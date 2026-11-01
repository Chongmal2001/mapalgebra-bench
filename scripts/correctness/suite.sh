#!/usr/bin/env bash
# Correctness cases (scripts/correctness/cases.txt, benchmark-design.md §3), one correctness/case.sh per run, then a
# summary. Stores are built on demand (scripts/data/stores.sh).
#   scripts/correctness/suite.sh [ID...]          default: $CASES (bench.env)
#   FILTER="op=mean radius=1,4" scripts/correctness/suite.sh K1     only the matching runs of K1
# A run counts as done when its compare report exists (FORCE=1 reruns it), so a suite can be stopped, restarted or
# extended by further IDs into the same result set. EXPECT=fail / error in a case line: a FAIL / a NES error is the
# expected outcome. CASES_FILE: other definition file. NES dumps and imported outputs are deleted after each compare,
# a reference after the last run that needs it (KEEP_REFS=1 keeps them).
# Outcomes: $RESULTS/correctness/outcomes.csv; reports: $RESULTS/correctness/<case>/<store>/<run>.json.
set -euo pipefail
source "$(dirname "$0")/../lib/common.sh"
cd "$BENCH_REPO"
FILE="${CASES_FILE:-$BENCH_REPO/scripts/correctness/cases.txt}"
IDS="${*:-$CASES}"
export NES_THREADS KEEP_DUMP=0 KEEP_OUTPUT=0
STORES="$BENCH_REPO/scripts/data/stores.sh"
OUTC="$RESULTS/correctness/outcomes.csv"
mkdir -p "$RESULTS/correctness"
[[ -f "$OUTC" ]] || echo "time_utc,case,store,workload,settings,expect,outcome,as_expected" > "$OUTC"

mapfile -t PLAN < <(python "$BENCH_REPO/scripts/lib/plan.py" "$FILE" "$IDS" "${FILTER:-}")
[[ ${#PLAN[@]} -gt 0 ]] || { echo "no runs for '$IDS' ${FILTER:+(filter $FILTER)}" >&2; exit 2; }
declare -A LAST_USE
for i in "${!PLAN[@]}"; do
  eval "set -- ${PLAN[$i]}"
  LAST_USE["$2|$3"]=$i
done

status=0
for i in "${!PLAN[@]}"; do
  eval "set -- ${PLAN[$i]}"
  id="$1" store="$2" wl="$3" st="$4"; shift 4
  settings="$*"
  if [[ "$st" != run ]]; then echo "########## $id $store $wl $settings: skipped (${st#skip:} not in FEATURES)"; continue; fi
  (
    expect=pass
    for kv in "$@"; do
      k="${kv%%=*}" v="${kv#*=}"
      case "$k" in
        EXPECT) expect="$v" ;;
        NES_INPUT) "$STORES" "$v" > /dev/null; NES_INPUT="$("$STORES" --path "$v")"; export NES_INPUT ;;
        *) export "$k=$v" ;;
      esac
    done
    export CASE_ID="$id"
    "$STORES" "$store" > /dev/null
    path="$("$STORES" --path "$store")"
    w="${wl#focal:}"; w="${w//:/-}"
    s="${NES_FOCAL_SUMMATION:-exact}"; [[ "$s" == exact ]] || w="$w-$s"
    fpw="${WINDOW_FRAMES:-1}" slide="${WINDOW_SLIDE:-${WINDOW_FRAMES:-1}}"
    if [[ "$fpw" != 1 || "$slide" != "$fpw" ]]; then w="$w-w$fpw"; fi
    if [[ "$slide" != "$fpw" ]]; then w="${w}s$slide"; fi
    if [[ -n "${CASE_TAG:-}" ]]; then w="$w-$CASE_TAG"; fi
    report="$RESULTS/correctness/$id/$(basename "$path")/$w.json"
    if [[ -e "$report" && "${FORCE:-0}" != 1 ]]; then echo "########## $id $store $wl $settings: done"; exit 0; fi
    echo "########## $id $store $wl $settings"
    rc=0
    "$BENCH_REPO/scripts/correctness/case.sh" "$path" "$wl" || rc=$?
    if [[ -e "$report" ]]; then
      outcome="$(python -c 'import json, sys; print("PASS" if json.load(open(sys.argv[1]))["passed"] else "FAIL")' "$report")"
    elif [[ $rc != 0 ]]; then outcome=ERROR; else outcome=MISSING; fi
    case "$expect:$outcome" in pass:PASS|fail:FAIL|error:ERROR) ok=yes ;; *) ok=no ;; esac
    echo "$(date -u +%FT%TZ),$id,$store,$wl,\"$settings\",$expect,$outcome,$ok" >> "$OUTC"
    echo "== $outcome (expected $expect)"
    [[ $ok == yes ]]
  ) || status=1
  if [[ "${KEEP_REFS:-0}" == 0 && "${LAST_USE[$store|$wl]}" == "$i" ]]; then
    w="${wl#focal:}"
    rm -rf "$DATA/refs/geotrellis/$(basename "$("$STORES" --path "$store")")/${w//:/-}"
  fi
done

python - "$OUTC" "$RESULTS/correctness" $IDS <<'PY'
import csv, json, os, sys
outc, root, ids = sys.argv[1], sys.argv[2], set(sys.argv[3:])
last = {}
for r in csv.DictReader(open(outc)):
    if r["case"] in ids:
        last[(r["case"], r["store"], r["workload"], r["settings"])] = r
print(f"{'case':<6}{'store':<12}{'workload':<24}{'settings':<44}{'outcome':>8}{'expected':>9}")
for k, r in sorted(last.items()):
    print(f"{r['case']:<6}{r['store']:<12}{r['workload']:<24}{r['settings'][:43]:<44}{r['outcome']:>8}{r['expect']:>9}"
          + ("" if r["as_expected"] == "yes" else "   <-- unexpected"))
bad = sum(r["as_expected"] != "yes" for r in last.values())
print(f"{len(last) - bad}/{len(last)} as expected")
PY
exit $status

#!/usr/bin/env bash
# Resource sampler for one process: every MS milliseconds one CSV row t_us (wall clock, µs since epoch), rss_kb, cpu_ticks
# (utime + stime), until the process ends or the sampler is killed. Bash builtins only, so sampling forks nothing.
#   scripts/lib/sample.sh PID OUT.csv [MS]       MS = 0: append one sample to an existing file and exit
pid="$1"; out="$2"; ms="${3:-100}"
printf -v secs '%d.%03d' $((ms / 1000)) $((ms % 1000))
if [[ "$ms" != 0 ]]; then
  exec {pause}<> <(:)
  echo "t_us,rss_kb,cpu_ticks" > "$out"
fi
while [[ -r /proc/$pid/stat ]]; do
  rss=
  while read -r k v _; do
    if [[ "$k" == VmRSS: ]]; then rss="$v"; break; fi
  done < "/proc/$pid/status" 2>/dev/null
  read -r -a st < "/proc/$pid/stat" 2>/dev/null || break
  [[ -n "$rss" ]] || break
  t="${EPOCHREALTIME/[.,]/}"
  echo "$t,$rss,$(( st[13] + st[14] ))" >> "$out"
  if [[ "$ms" == 0 ]]; then break; fi
  read -t "$secs" -u "$pause" _ || true
done

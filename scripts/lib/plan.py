"""Expands a case / grid definition file into one run per line, for scripts/correctness/suite.sh and scripts/perf/grid.sh.

Definition line:  ID  STORES  OPS:SHAPES:RADII  [KEY=V1,V2,...]...
  STORES          store names of scripts/data/stores.txt, comma-separated
  OPS:SHAPES:RADII  comma-separated lists, e.g. sum,mean:square,circle:1,4,16
  KEY=V1,V2       environment for the run; a comma list is one more axis (Cartesian product); quote values with spaces
Special keys: SUMMATION (exact, running, kahan or all; non-exact only for square sum / mean; default: NES' default,
kahan for square sum / mean, exact otherwise), REQUIRES (features that
must be listed in $FEATURES, else the line is skipped), EXPECT (pass | fail | error, correctness only).

Output, one shell-quoted line per run:  ID STORE WORKLOAD STATUS KEY=VALUE...   (STATUS: run | skip:<feature>)
  python plan.py FILE "K1 K3" [FILTER]     FILTER: space-separated key=v1,v2 conditions on id, store, op, shape, radius
                                           or any KEY, e.g. "op=mean radius=1,4"
"""
import itertools
import os
import shlex
import sys


def lines(path):
    for n, raw in enumerate(open(path), 1):
        text = raw.split("#", 1)[0] if not raw.lstrip().startswith("#") else ""
        words = shlex.split(text, comments=False)
        if words:
            if len(words) < 3 or words[2].count(":") != 2:
                sys.exit(f"{path}:{n}: expected ID STORES OPS:SHAPES:RADII [KEY=VALUE...]")
            yield n, words


def expand(words):
    rid, stores, wl, *kv = words
    ops, shapes, radii = (p.split(",") for p in wl.split(":"))
    axes, fixed = [], {}
    for item in kv:
        k, sep, v = item.partition("=")
        if not sep:
            sys.exit(f"bad setting {item!r} in line of {rid}")
        if k in ("REQUIRES",):
            fixed[k] = v
        else:
            vals = v.split(",") if "," in v and k not in ("GT_JAVA_OPTS",) else [v]
            if k == "SUMMATION" and vals == ["all"]:
                vals = ["exact", "running", "kahan"]
            axes.append((k, vals))
    for store, op, shape, r in itertools.product(stores.split(","), ops, shapes, radii):
        for combo in itertools.product(*(v for _, v in axes)):
            env = dict(zip((k for k, _ in axes), combo))
            dense = shape == "square" and op in ("sum", "mean")
            s = env.pop("SUMMATION", "kahan" if dense else "exact")
            if s != "exact" and not (shape == "square" and op in ("sum", "mean")):
                continue
            env["NES_FOCAL_SUMMATION"] = s
            yield rid, store, f"focal:{op}:{shape}:{r}", fixed.get("REQUIRES", ""), env


def main():
    path, ids = sys.argv[1], sys.argv[2].split()
    flt = {}
    for cond in (sys.argv[3].split() if len(sys.argv) > 3 else []):
        k, _, v = cond.partition("=")
        flt[k] = set(v.split(","))
    features = set(os.environ.get("FEATURES", "").split())
    known = {w[0] for _, w in lines(path)}
    for i in ids:
        if i not in known:
            sys.exit(f"{path}: no definition for {i} (defined: {' '.join(sorted(known))})")
    seen = set()
    for i in ids:                                    # in the order asked for
        for _, words in lines(path):
            if words[0] != i:
                continue
            for rid, store, wl, req, env in expand(words):
                _, op, shape, r = wl.split(":")
                fields = {"id": rid, "store": store, "op": op, "shape": shape, "radius": r, **env}
                if any(fields.get(k) not in v for k, v in flt.items()):
                    continue
                missing = [f for f in req.split(",") if f and f not in features]
                status = f"skip:{','.join(missing)}" if missing else "run"
                key = (rid, store, wl, tuple(sorted(env.items())))
                if key in seen:
                    continue
                seen.add(key)
                print(" ".join(shlex.quote(x) for x in (rid, store, wl, status, *(f"{k}={v}" for k, v in env.items()))), flush=True)


if __name__ == "__main__":
    main()

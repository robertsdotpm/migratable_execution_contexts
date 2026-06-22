"""Allocation-fast-path A/B microbenchmark for the alloc-home redirect.

Measures the cost of the feature-on-unused redirect: one extra `_alloc_home`
load + branch on every object/raw allocation, with the home always NULL (no
borrow active). The allocation-bound churn below is the case most able to show
that overhead.

Two modes:

  # 1. workload mode -- run the churn, print best-of-INNER wall time (seconds).
  #    Invoked once per interpreter by the driver; you don't call this directly.
  python perf_alloc_microbench.py --workload [INNER] [SCALE]

  # 2. A/B driver -- run the workload on two interpreters, alternating, and
  #    report the median ON/OFF ratio against the run-to-run noise floor.
  python perf_alloc_microbench.py --ab /path/to/off/python /path/to/on/python \
                                  [--reps 21] [--inner 5] [--scale 600000] [--cpu 3]

The two interpreters must be FREE-THREADED RELEASE builds from the SAME source,
configured identically, differing ONLY by -DPy_TSTATE_ALLOC_HOME (the "on" one).
See patches/VALIDATION.md for the exact build recipe. Run on an otherwise-idle,
frequency-pinned box; both arms run PYTHON_GIL=0.

Result (this machine, 2 passes): median ON/OFF +0.03% / +0.73% against a 4-6%
noise floor; the more-stable minimum statistic bounced both sides of zero
-> the feature-on-unused redirect is within measurement noise.
"""
import statistics as st
import subprocess
import sys
import time


def workload(inner, scale):
    import gc
    gc.disable()                      # measure the allocator, not GC scheduling
    best = float("inf")
    for _ in range(inner):
        t0 = time.perf_counter()
        acc = 0
        for i in range(scale):
            t = (i, i + 1, i + 2)     # tuple fast path
            l = [i, i, i, i]          # list
            d = {"a": i, "b": i + 1}  # dict (keytable raw alloc)
            o = object()              # bare object
            acc += t[0] + l[0] + d["a"] + (1 if o is not None else 0)
        dt = time.perf_counter() - t0
        best = min(best, dt)
    sys.stdout.write("%.6f\n" % best)


def run_one(py, cpu, inner, scale):
    cmd = []
    if cpu is not None:
        cmd += ["taskset", "-c", str(cpu)]
    cmd += ["env", "PYTHON_GIL=0", py, __file__, "--workload", str(inner), str(scale)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout.strip()
    return float(out)


def ab(py_off, py_on, reps, inner, scale, cpu):
    off, on = [], []
    for _ in range(reps):
        off.append(run_one(py_off, cpu, inner, scale))
        on.append(run_one(py_on, cpu, inner, scale))
    mo, mn = st.median(off), st.median(on)
    lo, ln = min(off), min(on)
    print("OFF (flag off): median=%.2fms min=%.2fms sd=%.3f n=%d"
          % (mo * 1e3, lo * 1e3, st.pstdev(off) * 1e3, len(off)))
    print("ON  (flag on) : median=%.2fms min=%.2fms sd=%.3f n=%d"
          % (mn * 1e3, ln * 1e3, st.pstdev(on) * 1e3, len(on)))
    print("median ON/OFF = %.4f (%+.2f%%)" % (mn / mo, (mn / mo - 1) * 100))
    print("min    ON/OFF = %.4f (%+.2f%%)" % (ln / lo, (ln / lo - 1) * 100))
    print("noise floor (OFF sd/median) = %.2f%%" % (st.pstdev(off) / mo * 100))


def main(argv):
    if "--workload" in argv:
        i = argv.index("--workload")
        rest = argv[i + 1:]
        inner = int(rest[0]) if len(rest) > 0 else 5
        scale = int(rest[1]) if len(rest) > 1 else 600000
        workload(inner, scale)
        return 0
    if "--ab" in argv:
        i = argv.index("--ab")
        py_off, py_on = argv[i + 1], argv[i + 2]

        def opt(name, default, cast=int):
            return cast(argv[argv.index(name) + 1]) if name in argv else default
        ab(py_off, py_on,
           reps=opt("--reps", 21), inner=opt("--inner", 5),
           scale=opt("--scale", 600000),
           cpu=opt("--cpu", 3) if "--cpu" in argv or True else None)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))

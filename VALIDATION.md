# Validation — how to reproduce the claims

Build the interpreters with `scripts/build_interpreters.sh` (see its header); it
applies the patch and produces `build-{off,on,pydebug,tsan}/python`. The patch is
~50 lines across three files and is meant to be audited by reading; the soundness
argument is in the PEP's *Specification* and *Reference Implementation →
Validation* sections.

There are two tiers of evidence.

## Self-contained (reproducible here, today)

Pure CPython — no third-party runtime. These cover the *cost* of the hook and its
*non-impact when off*.

| Claim | How to reproduce | Build |
|---|---|---|
| **OFF build is byte-identical to stock** (593 stdlib tests pass, no regression) | `./build-off/python -m test -j0 test_dict test_list test_gc test_weakref test_threading` | `off` |
| **Feature-on-unused redirect is within perf noise** (median ON/OFF ~+0.0–0.7% vs a 4–6% noise floor) | `python validation/perf_alloc_microbench.py --ab build-off/python build-on/python` | `off` + `on` |
| **mimalloc cross-thread free is race-clean under TSan** (the control that distinguishes a real borrow race from an M:N runtime's fiber-sync noise) | run `validation/tsan_xthread_free_control.py` under `build-tsan/python` (see script header) → **0 reports** | `tsan` |

The two perf interpreters are built from one source, configured identically,
differing **only** by `-DPy_TSTATE_ALLOC_HOME` — so the microbench isolates
exactly the redirect's load+branch.

## Soundness under real migration (demonstrated by a reference runtime)

Demonstrating correctness under *real* cross-thread migration inherently needs a
runtime that moves execution across OS-thread workers. A reference M:N
stackful-coroutine runtime (published separately) provides that, and was used to
produce the results the PEP cites:

- **An 8-way adversarial source audit** of every piece of per-thread state the
  redirect leaves un-redirected (free lists, `current_object_heap`, GC
  `alloc_count` + the stop-the-world scan, QSBR / page-list reclamation, biased
  reference counting, raw `PyMem_*`, redirect-set completeness, same-interpreter),
  each verdict independently refuted: all sound.
- **A pydebug A/B** under a `--with-pydebug --disable-gil` interpreter built with
  the flag (so `brc.c:122`, `mimalloc/alloc.c:120`, and the QSBR asserts are
  live): a 10-round migration stress with GC churn, stop-the-world collections,
  and cross-thread decref of borrow-allocated objects → **0 assertions**; the
  borrow-disabled baseline aborts immediately at `mimalloc/alloc.c:120`, proving
  the stress reaches the hazard.
- **The fixed crash:** a channel-churn stress that crashed in `_mi_page_retire`
  without the borrow passes **24/24** with it (8/8 abort without).
- **A borrow-path TSan run:** no races in the borrow logic. The remaining reports
  are the runtime's own fiber-sync instrumentation gap (proven by the
  self-contained control above being race-clean), not the borrow.

These become fully reproducible here once that runtime is public; the
self-contained tier above and the patch itself are reproducible by anyone now.

## Honest status

Run and passing: the perf A/B (in the noise), the TSan control (clean), the
pydebug migration A/B (0 asserts), the source audit, the channel-churn fix, and a
borrow-path TSan run (no borrow-logic races). **Not yet done:** the full
`pyperformance` suite (the gate for making the redirect unconditional rather than
build-gated), a ≥100k-task-scale TSan run, and a fully clean TSan run of the
borrow path (which needs the consuming runtime's fiber-sync primitives annotated
for TSan). And there is **one** consumer so far — a second independent one is the
thing this most needs (see the PEP's *Open Issues* and the discussion post).

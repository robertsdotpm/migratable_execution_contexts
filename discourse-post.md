# Migratable execution contexts for free-threading: a tiny "allocation-home" hook (pre-PEP, seeking feedback + a sponsor)

*Suggested category: **Free-threading** (or **C API**). This is a pre-submission
draft — I'd like feedback on the idea, and per PEP 1 I'm looking for a
core-developer sponsor before it becomes a numbered PEP.*

## TL;DR

Free-threaded CPython welds a thread state's **allocation** to the OS thread it
runs on, which blocks the whole category of userspace M:N schedulers / green
threads / stackful coroutines: a lightweight task's execution can't be moved to
another OS-thread worker, because its mimalloc heap would move with it and
corrupt. I have a ~50-line, off-by-default patch that decouples *where allocation
is served* from *which thread state is executing* — one borrow pointer — and a
working M:N runtime that uses it to migrate parked fibers across workers. I'd like
to know (1) whether there's appetite to have such a hook upstream and a core dev
willing to sponsor a PEP, and (2) whether other green-thread/runtime authors hit
the same wall and would use it.

## The problem (one coupling)

A single `_PyThreadState_GET()` serves **both** execution state (frames,
exceptions, recursion, GC state) **and** allocation (mimalloc heap selection on
`PyObject_Malloc`'s fast path). These have *different* affinity requirements:
execution is per-task and movable; allocation must be per-OS-thread (the mimalloc
invariant — cross-thread *free* is supported via `xthread_free`, cross-thread
*alloc* is not). So a per-fiber thread state attached on a different worker and
then allocating lands on a heap owned by another OS thread → corruption (the
`_mi_page_retire` crash; the `mimalloc/alloc.c:120` `heap->thread_id == tid`
assert in a debug build). The existing escapes are all unsatisfactory: one shared
thread state per worker (no per-fiber frames across a yield), mimalloc
abandon/adopt per migration (designed for thread *death*, far too heavy), or
simply don't migrate (forfeits work-stealing and rescuing fibers stuck behind a
blocked worker).

## The mechanism

Add one optional, build-gated pointer to the free-threaded thread state and let
its allocation be redirected to a **home** — the thread state bound to the OS
thread currently executing it — while its execution state stays its own:

```c
#define _PyThreadStateImpl_AllocHome(tstate) \
    ((tstate)->_alloc_home ? (tstate)->_alloc_home : (tstate))
```

A migratable fiber runs under an execution-only thread state whose `_alloc_home`
is re-pointed to the running worker on each resume. It owns no live heap of its
own, so there's nothing to migrate and nothing to corrupt — and nothing extra for
GC to walk. The redirect covers the object-alloc heap selection, the raw
`PyMem_*` heaps, and the mimalloc page list; QSBR and biased refcounting
deliberately stay with the executing thread state (they key on the OS thread id
and self-correct). Off by default it's byte-identical to stock CPython.

## Status — there's a working patch, and I tried hard to break it

- **Patch:** ~50 lines against 3.13t across `obmalloc.c` +
  `pycore_object_alloc.h` + `pycore_tstate.h`. Off → byte-identical; the off
  build passes 593 stdlib tests.
- **Real consumer:** a Go-style M:N stackful-coroutine runtime migrates parked
  fibers across workers with it; a channel-churn stress that crashed in
  `_mi_page_retire` without the borrow passes **24/24** with it (8/8 abort
  without).
- **Soundness audit:** I audited every piece of per-thread state the redirect
  leaves alone (free lists, `current_object_heap`, GC alloc-count + the STW scan,
  QSBR/page-list reclamation, biased refcounting, raw `PyMem_*`, redirect-set
  completeness, same-interpreter) against the 3.13t source. They hold; in
  particular brc self-corrects because `ob_tid` is the *running* OS thread, so
  cross-thread decrefs route to the running worker's bucket.
- **pydebug oracle:** built a `--with-pydebug --disable-gil` interpreter *with*
  the flag, so the `brc.c` / `mimalloc/alloc.c:120` / QSBR asserts are live. A
  10-round migration stress (cross-worker migration + cyclic-GC churn + STW +
  cross-thread decref of borrow-allocated objects) runs clean — **0 assertions**.
  The borrow-off baseline aborts immediately at `alloc.c:120`, confirming the
  stress actually reaches the hazard.
- **Perf:** an allocation-bound microbench (two release builds differing only by
  the flag) puts the feature-on-unused redirect **within run-to-run noise**.
- **TSan:** under a fully TSan-instrumented interpreter the migration stress is
  race-clean *in the borrow logic*; the remaining reports are a separate
  instrumentation gap in my runtime's fiber primitives, not the borrow (a
  real-threads control is race-clean).

## What I'm *not* claiming

The full `pyperformance` suite isn't run yet (the gate for making the redirect
unconditional rather than build-gated); there's only **one** consumer so far
(mine); and the draft proposes the public API as `PyUnstable_*` while the
reference patch still exposes the mechanism via a private inline. I'd rather say
this plainly than oversell it.

## What I'm asking

1. **Is this the right layer, and is there appetite for it upstream?** The honest
   long-term design is splitting the thread state into a movable *execution
   context* and a detachable *allocator context*; the borrow pointer is the
   small increment that unblocks the hot path now and doesn't foreclose that
   split. Is a scoped hook like this something the free-threading effort would
   consider — and would anyone be willing to **sponsor** a PEP?
2. **Do other runtimes hit this?** If you maintain or are building a green-thread
   / coroutine / actor runtime on free-threaded Python (greenlet/gevent/eventlet,
   a Stackless-style design, trio/anyio-adjacent executors, …): does the
   allocation-vs-execution coupling block you too, and would this hook (or the
   detachable-context version) help? A second independent "yes, I'd use this" is
   the thing this most needs.

Full draft PEP, the patch, and reproducible validation (build recipes + an
allocation A/B microbenchmark + a TSan control):
<https://github.com/robertsdotpm/migratable_execution_contexts>. Happy to walk
through any part of the soundness argument or the validation in detail.

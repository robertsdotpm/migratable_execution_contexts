# Migratable execution contexts for free-threaded CPython

A proposed, opt-in CPython hook — a borrowable per-thread-state **allocation
home** — that lets a thread state's *allocation* be served from another
(same-OS-thread) thread state's heap while its *execution* state stays its own.
This is the primitive a userspace M:N scheduler needs to migrate a lightweight
task's execution between OS-thread workers without corrupting the allocator.

This repo is the **pre-submission bundle**: the draft PEP, the reference patch,
the discussion post, and reproducible validation. It is circulated to gather
feedback and, per [PEP 1](https://peps.python.org/pep-0001/), to find a
core-developer **sponsor** before it becomes a numbered PEP.

## The problem, in one paragraph

In free-threaded CPython a single `_PyThreadState_GET()` selects both the
executing thread state *and* its mimalloc heap, and mimalloc heaps are
OS-thread-affine (cross-thread *free* is supported, cross-thread *alloc* is not).
So a per-task thread state attached on a different worker and then allocating
lands on a heap owned by another OS thread → corruption. This blocks the whole
category of userspace M:N schedulers / green threads / stackful coroutines. The
patch adds one optional, build-gated `_alloc_home` pointer that redirects
allocation to the running worker's thread state; **off by default it is
byte-identical to stock**.

## Contents

| Path | What |
|---|---|
| [`pep-borrowable-alloc-home.rst`](pep-borrowable-alloc-home.rst) | the draft PEP (reStructuredText) |
| [`discourse-post.md`](discourse-post.md) | the discussion-forum post (problem → patch → evidence → asks) |
| [`patch/cpython313t-tstate-alloc-home.patch`](patch/cpython313t-tstate-alloc-home.patch) | the reference patch (~50 lines, 3 files, off by default) |
| [`VALIDATION.md`](VALIDATION.md) | every claim → how to reproduce it |
| [`scripts/build_interpreters.sh`](scripts/build_interpreters.sh) | build the off / on / pydebug / tsan interpreters |
| [`validation/`](validation/) | self-contained perf A/B + the TSan control |

## Status

The patch design has been audited against the CPython 3.13.13 free-threaded
source and validated under a `--with-pydebug` interpreter built with the flag
(assertions live), with the feature-on-unused redirect measured within
perf noise. Soundness under real cross-thread migration is demonstrated by a
reference M:N runtime that is published separately. See
[`VALIDATION.md`](VALIDATION.md) for exactly what is and is not done, and the
PEP's *Open Issues* for the honest remaining work (chiefly: the full
`pyperformance` suite, and a second independent consumer).

## What this is asking

1. Is a scoped hook like this something the free-threading effort would consider
   upstream, and would a core developer **sponsor** a PEP?
2. If you build a green-thread / coroutine / actor runtime on free-threaded
   Python: does the allocation-vs-execution coupling block you too, and would
   this hook help? A second independent consumer is what this most needs.

Discussion thread: <https://discuss.python.org/t/migratable-execution-contexts-for-free-threading-a-tiny-allocation-home-hook-pre-pep-seeking-feedback-a-sponsor/107860>

## License

The PEP text is CC0-1.0 / public domain (standard for PEPs). The patch is offered
for inclusion in CPython under the PSF license. The validation scripts are under
this repo's `LICENSE`.

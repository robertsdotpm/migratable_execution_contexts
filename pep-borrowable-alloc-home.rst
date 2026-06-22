PEP: 9999
Title: Borrowable per-thread-state allocation home for free-threaded CPython
Author: Matthew Roberts <matthew@roberts.pm>
Sponsor: (to be recruited — see note)
Status: Draft
Type: Standards Track
Content-Type: text/x-rst
Created: 22-Jun-2026
Python-Version: 3.15
Post-History: (pre-submission — see note)
Discussions-To: (pre-submission — see note)

.. note::

   This is a **pre-submission draft**, circulated for discussion on
   discuss.python.org — it is *not* (yet) a submitted PEP. Per :pep:`1`, a
   non-core-developer author recruits a core-developer **sponsor** (ideally a
   free-threading / :pep:`703` contributor) *before* a number is assigned;
   finding one is a goal of that discussion. The ``PEP: 9999`` number,
   ``Sponsor``, ``Discussions-To`` and ``Post-History`` fields are therefore
   placeholders for now, and ``Resolution`` is left for the Steering Council or
   its delegate. The draft is otherwise complete; what it primarily seeks is a
   sponsor and a second prospective consumer (see `Open Issues`_).


Abstract
========

In free-threaded CPython (:pep:`703`) every ``PyThreadState`` owns a
per-OS-thread mimalloc heap. mimalloc permits cross-thread *free* (via
``xthread_free``) but not cross-thread *allocation*: a heap may only be
allocated from on its owning OS thread. Because a single thread-local read
(``_PyThreadState_GET()``) selects both the executing thread state *and* the
allocator heap, a thread state's execution cannot be moved to a different OS
thread without its allocator moving with it, which corrupts the heap.

This PEP specifies a minimal, opt-in mechanism — an *allocation-home* pointer on
the free-threaded thread-state struct — that lets one thread state's object and
raw allocations be served as if they originated on another (same-OS-thread)
thread state's heap, while the borrowing thread state keeps its own execution
state (frames, exception state, recursion counters). This decouples *where
allocation is served* from *which thread state is executing*, which is the
mechanism a userspace scheduler needs to move a lightweight task's execution
between OS-thread workers.

The mechanism is deliberately scoped to the hot migration path and is presented
as a stepping-stone toward, not a substitute for, a future detachable
allocator-context design (see `Why thread state, not a new execution-context
type`_).


Motivation
==========

The model behind goroutines, Erlang processes, and M:N / green-thread runtimes
is to multiplex many lightweight tasks across a small pool of OS threads. Such a
runtime runs ``N`` OS-thread *workers* that multiplex ``M`` lightweight
*fibers*. For load balancing, and to rescue work stranded behind a worker
blocked in a long non-cooperative C call, a fiber that parked on one worker must
be able to resume on another.

Each fiber needs its own ``PyThreadState`` so its Python frames, exception
state, recursion counters and current-frame chain survive a park/resume. In a
free-threaded build a ``PyThreadState`` also owns a mimalloc heap
(``_PyThreadStateImpl.mimalloc``), which is OS-thread-affine. When a per-fiber
thread state is attached on a *different* worker and allocates, the allocation
lands on a heap owned by another OS thread. The observed failure is a crash in
``_mi_page_retire`` during teardown; the structural detection point is the
mimalloc assertion ``heap->thread_id == 0 || heap->thread_id == tid``
(``mimalloc/alloc.c:120``), which fires in a debug mimalloc when a heap is
allocated from off-thread. This is the one failure that has been *confirmed* to
block naive fiber migration in the reference runtime, and it is exactly the
failure this PEP's mechanism removes (see `Reference Implementation`_).

The existing options are unsatisfactory:

1. **One thread state per worker, shared by all fibers on it.** Fibers then have
   no independent execution state, so per-fiber frames and exception state
   cannot be preserved across a yield. This is the only option that works on
   stock CPython today, and it cannot provide the capability M:N runtimes need.

2. **mimalloc abandon/adopt on every migration.** mimalloc supports handing a
   heap from a dying thread to another (``_mi_heap_abandon`` / adopt), but this
   is designed for *thread death*: it walks and reassigns the heap's pages.
   Fiber migration happens at every park/resume, so abandon/adopt per migration
   is far too expensive (see `Rejected Ideas`_ for the cost framing this PEP
   commits to quantifying).

3. **Do not migrate.** Pin every fiber to its origin worker. This forfeits work
   stealing and the ability to rescue fibers stranded behind a blocked worker —
   the problems M:N scheduling exists to solve.

This is a missing primitive, not a missing feature: the runtime needs a way to
say "serve this thread state's allocations from that thread state's heap for the
duration of this run."

This PEP does **not** claim free-threaded CPython "owes" the M:N ecosystem a
hook. It claims that a single, well-scoped indirection removes a specific
correctness hazard, and it accepts that adding the hook constrains future
allocator internals (see `Why thread state, not a new execution-context
type`_ and `Open Issues`_). The trade-off — a one-pointer indirection on the
allocator fast path versus a class of runtime that cannot otherwise be built —
is argued, not asserted.

The most likely Steering Council counter is "do this in a fork / out-of-tree
patch; the reference implementation already is one." `Rationale`_ answers this
directly under `Why upstream and not a fork`_.


Rationale
=========

The root cause is a coupling. A single ``_PyThreadState_GET()`` serves both
execution state (``PyErr_*``, recursion limits, GC state, the frame chain) and
allocation (mimalloc heap selection on ``PyObject_Malloc``'s fast path). These
have different affinity requirements:

* **Execution state** is logically per-task and movable.
* **Allocation state** must be per-OS-thread (the mimalloc invariant).

The minimal change is to let a thread state's allocation be redirected to a
*home* thread state — the one bound to the OS thread currently executing it —
while its execution state stays its own. A migratable fiber then runs under an
execution-only thread state whose ``_alloc_home`` points at the running worker's
thread state. Allocations are served from the worker's correctly thread-local
heap; objects allocated under a previous home are reclaimed via mimalloc's
supported cross-thread *free*. The per-fiber thread state never owns a live heap
of its own, so there is nothing to migrate and nothing to corrupt, and there is
no per-fiber heap to walk during garbage collection.

The indirection is one pointer load; re-pointing ``_alloc_home`` on each resume
is O(1).

A central question for the soundness of this design is which companion
per-thread state must *also* follow the home, and which can safely stay with the
executing (per-fiber) thread state. That question has now been worked through
against the free-threaded CPython 3.13.13 source and validated under a
free-threaded ``--with-pydebug`` interpreter; the answer is recorded in
`Companion per-thread state and soundness`_ and is **narrower than the previous
draft of this PEP assumed**: only the mimalloc heap selection and the
deferred-page list need to follow the home. The biased reference counting,
QSBR, deferred-free queue and per-type object free lists are correct to leave on
the executing thread state, because they all key on the *running OS thread's*
identity rather than on the thread state, and the redirect homes every
allocation on the heap of the OS thread that is executing the fiber.


Why thread state, not a new execution-context type
---------------------------------------------------

The honest framing: the proper long-term design splits ``_PyThreadStateImpl``
into a movable *execution context* and a cheaply (de)tachable *allocator
context*, which would also compose cleanly with sub-interpreters and survive a
change of the free-threaded allocator. That is a large refactor (order thousands
of lines touching the thread-state lifecycle).

The borrow pointer is the small increment (order tens of lines) that unblocks
the hot migration path now. This PEP is therefore framed as a deliberately
scoped stepping-stone. ``_alloc_home`` does **not** foreclose the later split: a
detachable allocator-context design can subsume the borrow by making the
"home" a first-class allocator context rather than a second thread state, and
the contract below is stated in allocator-agnostic terms ("this thread state's
allocations are served as if they originated on the home's OS thread, whatever
the free-threaded allocator is") so that the public contract does not weld
itself to mimalloc's current per-OS-thread heap model.

Because the contract is intentionally allocator-agnostic at the API surface but
the *implementation* is mimalloc-specific, the symbol is proposed as
``PyUnstable_``-prefixed (explicitly unstable) and the build is gated off by
default until the free-threaded allocator model is stable enough to commit to.


Why upstream and not a fork
---------------------------

An out-of-tree allocator patch must be re-rebased against every change to
``Objects/obmalloc.c`` and ``Include/internal/pycore_object_alloc.h``, both of
which are actively refactored as the free-threaded allocator matures. A
recompile-only patch cannot be relied on by an ecosystem: no PyPI wheel and no
distribution interpreter can ship it, and any consumer must build CPython
themselves. The value of upstreaming is precisely that the hook tracks the
allocator across refactors and becomes available to every free-threaded build.
This argument is only valid if the mechanism is eventually unconditional; the
permanently-gated outcome is therefore explicitly rejected (see `Rejected
Ideas`_).


Specification
=============

The proposal adds one optional, build-gated field, one allocator indirection,
one C-API surface with a getter, a normative contract, and a set of debug
assertions. All are scoped to the free-threaded build.


The ``_alloc_home`` field
--------------------------

A new field is added to the free-threaded thread-state struct
(``_PyThreadStateImpl``, in ``Include/internal/pycore_tstate.h``), gated on the
feature macro and placed **last** in the struct::

    #if defined(Py_GIL_DISABLED) && defined(Py_TSTATE_ALLOC_HOME)
        struct _PyThreadStateImpl *_alloc_home;  /* NULL => use own allocator */
    #endif

``NULL`` (the default for every thread state) means the thread state uses its
own allocator, identical to current behavior. When non-``NULL``, allocation is
served from ``_alloc_home``.

Two properties of this layout are load-bearing and are what the reference patch
implements (it supersedes the previous draft's *unconditional-field* design):

* **Gated on the feature macro.** With ``Py_TSTATE_ALLOC_HOME`` undefined the
  field is absent and the accessor and every redirect macro-expand back to the
  original ``tstate``, so the build is byte-for-byte identical to stock CPython
  (the field, the accessor, and the cost all vanish). The default build pays
  *nothing* — not even a ``NULL`` pointer per thread state.

* **Placed last in the struct.** With the macro on, only a trailing pointer is
  appended; **no existing field's offset shifts**. An extension compiled against
  ``pycore_tstate.h`` *without* the macro therefore reads ``qsbr``,
  ``mimalloc`` and every other member at correct offsets — it is simply unaware
  of a harmless trailing field it does not touch. The field relies on the thread
  state being zero-initialized, which ``alloc_threadstate`` guarantees via
  ``PyMem_RawCalloc``, so the appended position is safe.

Together these mean the design has *no two-ABI hazard for field access* (no
existing offset moves between a macro-on interpreter and a macro-off extension)
*and* preserves the byte-identical-when-off property. See `Backwards
Compatibility`_.


The redirect
------------

The free-threaded object allocator selects its heap and per-size-class page list
from the allocation-home thread state rather than the executing one:

.. code-block:: c

   #define _PyThreadStateImpl_AllocHome(tstate) \
       ((tstate)->_alloc_home ? (tstate)->_alloc_home : (tstate))

(under ``Py_TSTATE_ALLOC_HOME``; with the macro off it expands to ``(tstate)``).

The complete set of allocation entry points that consult ``tstate->mimalloc``
and are redirected through ``_PyThreadStateImpl_AllocHome(tstate)`` is
enumerated below. Completeness of this set is the crux of the soundness
argument; any path that reads ``tstate->mimalloc`` (or a per-thread allocator
cache) to *decide where a byte is carved* without going through the home
reintroduces the cross-thread-allocation hazard.

Redirected entry points (from the reference patch):

* the two object-allocation inline fast paths in
  ``Include/internal/pycore_object_alloc.h``
  (``_PyObject_MallocWithType`` / ``_PyObject_ReallocWithType``);
* ``_PyObject_GetAllocationHeap``;
* the three ``current_object_heap`` accesses (see `The current_object_heap
  scratch slot`_);
* the three raw-memory allocators ``_PyMem_MiMalloc`` / ``_PyMem_MiCalloc`` /
  ``_PyMem_MiRealloc`` (see `Raw memory allocation`_);
* the two ``mimalloc.page_list`` insertion sites in ``Objects/obmalloc.c``;
* the page-retire walk in ``Objects/obmalloc.c``.

This is the **complete** set of allocation-deciding reads of per-thread
allocator state. It was established by enumerating every per-thread mimalloc
read in CPython 3.13.13 and confirming that every read *not* in this set is
either statistics, the GC heap-walk (which safely tolerates an execution-only
thread state's empty heaps — see `Garbage collection`_), or bind / abandon /
teardown — none of which decide where a byte is carved. The only ``mi_heap_*``
allocation callers in the whole tree are the six ``obmalloc.c`` functions above;
GC allocations route through the redirected ``_PyObject_MallocWithType``. (One
debug-only read, ``fill_mem_debug``, is discussed under `Open Issues`_; it is a
``--with-pydebug`` fill detail, not an allocation-deciding site.)

**Not** redirected, and *verified sound* to leave on the executing thread state
(see `Companion per-thread state and soundness`_):

* per-thread object free lists (``_Py_object_freelists``);
* QSBR (``tstate->qsbr``) and the deferred-free queue
  (``mem_free_queue``);
* biased reference counting (``tstate->brc``);
* the GC thread state, including ``alloc_count``.

The previous draft treated the first of these — free lists — as a "correctness
hole" and a blocker. That framing is **withdrawn**: a free-list push/pop is pure
pointer reuse of a block mimalloc still considers in-use, touches no mimalloc
per-thread or per-page metadata, and is therefore not an allocation at all
(see `Companion per-thread state and soundness`_). The completeness of the
redirect set rests on the current ``obmalloc`` / ``pycore_object_alloc.h``
layout and must be re-audited on any allocator refactor; that is a real ongoing
cost (and part of why "permanently gated" is rejected), tracked in `Open
Issues`_.


The current_object_heap scratch slot
-------------------------------------

``current_object_heap`` is **not** durable allocator state; it is a per-call
scratch slot that ``_PyObject_MallocWithType`` writes
(``m->current_object_heap = GetAllocationHeap(...)``) and ``PyObject_Malloc``
re-reads. The reference patch redirects it to the home, and this is **correct**.
(The previous draft proposed keeping it on the executor and amending the patch;
that requirement is withdrawn.)

The concern was a preemption between the write and the read letting a second
fiber overwrite a shared home's ``current_object_heap``, mis-binning a GC object.
That cannot happen, for two independent structural reasons verified against the
runtime:

* **No interleaving point exists between the write and the read.** The set and
  the read both lie within one non-yielding pure-C call chain
  (``_PyObject_MallocWithType`` → ``PyObject_Malloc`` → ``_PyObject_MiMalloc``
  → ``mi_heap_malloc``). That chain contains no eval-breaker poll, no Python
  frame entry, and no other interpreter re-entry. An M:N scheduler's preemption is
  delivered only at Python frame entry (the eval-frame wrapper) and at the
  eval-breaker on a backward jump — both strictly outside that C call. A fiber
  therefore always reads the ``current_object_heap`` value it itself just wrote.

* **A home has a single writer.** A worker is one OS thread, and at most one fiber
  executes on it at any instant; the home is the running worker's own per-OS-thread
  thread state, so ``current_object_heap`` on that home is written only by that
  one OS thread.

Contract item (3) below — at most one executing thread state borrows a given
home at any instant — follows from the same-OS-thread requirement and is what
keeps the durable ``heaps[]`` / ``page_list`` selection (which *is* shared
mutable state) sound; it is stated explicitly for that reason.


Raw memory allocation
----------------------

The raw allocators ``_PyMem_MiMalloc`` / ``Calloc`` / ``Realloc`` are also
redirected to the home heap. Raw ``PyMem_*`` blocks have no ``ob_tid`` and no
biased-refcount path, so the object-level "reclaimed via cross-thread free"
argument is made for them separately and on a simpler basis: a raw block
allocated on the home's heap may later be freed on any thread, and mimalloc's
``mi_free`` decides local-vs-cross-thread purely by comparing the calling OS
thread id (``_mi_prim_thread_id()``) against the block's segment owner
(``segment->thread_id``) — it never consults the thread state or
``_alloc_home``. A free on the owning worker takes the thread-local path (the
owning worker's OS thread is the unique local-freer of its own segments); a free on
any other thread takes the supported lock-free cross-thread path
(``_mi_free_block_mt``, an atomic push onto ``page->xthread_free``), exactly as
for object pages. The raw MEM heap additionally sets ``page_use_qsbr = false``,
so raw page retirement never touches the (non-redirected) per-fiber QSBR slot or
page list.

This was confirmed under ``--with-pydebug`` (see `Reference Implementation`_); a
TSan run of the raw path is still outstanding (see `Performance`_ and `Open
Issues`_).


Companion per-thread state and soundness
----------------------------------------

This subsection records which companion per-thread state must follow the home
and which is sound to leave on the executing thread state. It is a correctness
question at **any** fiber count, not a tuning item at high fiber counts. Each
path below was independently verified against the CPython 3.13.13 free-threaded
source by an 8-way adversarial review (each verdict separately refuted), and the
end-to-end behavior was exercised under a free-threaded ``--with-pydebug``
interpreter built with the feature macro, so the relevant CPython asserts
(``brc.c:122``, ``mimalloc/alloc.c:120``, the QSBR poll assert) are live. The
relevant invariant is the **biased-refcount owner contract**: the biased-refcount
owner must be the thread that runs the thread state. The resolution below is how
that contract is honored under migration.

The organizing fact is that the un-redirected companion state all keys on the
**running OS thread's identity**, not on the thread state. ``_Py_ThreadId()``
(``Include/object.h``) reads the hardware thread-pointer register of the OS
thread currently executing, ``ob_tid`` is stamped from it at allocation, biased
refcounting and ``mi_free`` route on it, and the redirect homes every allocation
on the heap of the OS thread executing the fiber. So the allocating thread, the
segment owner, and ``ob_tid`` always coincide on whichever worker the fiber is
running, and any later cross-worker release takes the standard shared-refcount path
back to the owning worker.

Walking the four not-redirected items:

* **Free lists — sound, not a hole.** A per-type object free list lives on the
  executing (per-fiber) thread state (``_Py_object_freelists_GET`` reads the
  *running* thread state's ``freelists``), so it travels with the fiber exactly
  like its frames. A push or pop rewrites only the freed block's own first word
  plus the plain C arrays in ``tstate->freelists``; it performs **no** mimalloc
  operation and touches **no** mimalloc per-page or per-thread metadata, and it
  does not decrement ``page->used`` (mimalloc still considers the block in use).
  Re-handing a block out on a different worker than it was carved on is therefore
  pure pointer reuse of a block the fiber already owns — *not* an allocation,
  and *not* the foreign-heap ``mi_heap_malloc`` hazard the patch exists to
  prevent. The only mimalloc interaction, the eventual ``mi_free`` when a free
  list overflows or is cleared, keys on the segment's OS-thread owner (not the
  thread state or ``_alloc_home``) and takes the supported cross-thread-free
  path. Leaving the free lists un-redirected is correct.

* **QSBR / deferred-free queue — sound.** ``tstate->qsbr`` and ``mem_free_queue``
  stay with the executing fiber. ``_Py_qsbr_poll`` asserts the qsbr passed is
  the *running* thread state's, which holds because the fiber's own qsbr is
  passed; and a deferred page minted in the fiber's QSBR domain is reclaimed in
  the home's ``page_list`` no earlier than globally quiescent because the QSBR
  shared sequence domain is per-interpreter (see `QSBR and page-list
  cross-domain reclamation`_).

* **Biased reference counting (brc) — sound, resolved.** This is the path the
  previous draft left as an unresolved (a)/(b) choice ("does ``ob_tid`` name the
  executor or the home?"). The answer: ``ob_tid`` is set to the **running OS
  thread** (``_Py_ThreadId()``), not the fiber's spawn thread and not the
  fiber's thread state. brc stays with the executing thread state (option (b)),
  and it self-corrects: a cross-thread decref of a borrow-allocated object
  routes via ``_Py_brc_queue_object`` to the bucket keyed by ``ob_tid``, i.e. to
  the **running worker** that allocated it, whose brc table is drained on that same
  worker where ``brc->tid == _Py_ThreadId()`` (``brc.c:122``) holds. The per-fiber
  thread state's *own* brc table is therefore essentially never the merge sink
  for the fiber's objects. This is exactly the "self-correct via ``ob_tid``"
  the patch claims, and it was exercised under ``--with-pydebug`` without firing
  ``brc.c:122`` (see `Reference Implementation`_).

  **Residual (medium confidence).** This safety currently rests on brc bucket
  *insertion order*: a per-fiber thread state can share a brc bucket and tid with
  the long-lived worker it was spawned on, and ``find_thread_state`` returns the
  long-lived owner (registered first) ahead of the per-fiber duplicate, so the
  duplicate is shadowed and never selected as the drainer. That is an *emergent*
  property of CPython 3.13.13's registration order and head-first bucket walk,
  **not a documented invariant**. The confidence here is **medium**, not high:
  the ``brc.c:122`` assert is theoretically reachable if a future CPython
  changes brc bucket insertion order, and the empirical evidence rests partly on
  non-reproduction of a rare interleaving with a TSan run still outstanding.
  This PEP therefore recommends (a) a regression assertion pinning the
  bucket-order property, and (b) keeping migration gated behind the alloc-home
  heap fix. Tracked in `Open Issues`_.

* **GC thread state / ``alloc_count`` — cadence-only, not a correctness issue.**
  See `Garbage collection`_.

Because each of these is resolved rather than open, this PEP **does** now assert
that redirecting the heap selection and page list (and keeping the companion
state as above) is sufficient for soundness *under the validated conditions*
(single interpreter, ``--with-pydebug``). It does not claim TSan or performance
validation, which remain open (see `Open Issues`_).


QSBR and page-list cross-domain reclamation
--------------------------------------------

In ``Objects/obmalloc.c`` a deferred page is stamped
``page->qsbr_goal = _Py_qsbr_shared_next(tstate->qsbr->shared)`` using the
*executing* (fiber) QSBR, then linked into the *home*'s
``mimalloc.page_list``. The page's reclaim goal is sequenced in the fiber's QSBR
domain, but the page lives on and is processed by the home's ``page_list`` under
the home's polling in ``_PyMem_ProcessDelayed``.

The correctness of this pairing depends on the goal minted in the fiber's QSBR
domain being correctly observed-as-reached when polled on the home's thread. The
preferred precondition — a *shared* QSBR domain — **holds structurally** and
need not be established by the consumer:

* ``_qsbr_shared`` is **per-interpreter**, not per-thread: it is the
  ``struct _qsbr_shared`` embedded directly in ``PyInterpreterState``
  (confirmed in ``pycore_qsbr.h`` / ``qsbr.c``), and every thread state's
  ``qsbr->shared`` is initialized to ``&interp->qsbr`` at registration.
* A migrating runtime that uses a single interpreter therefore has
  ``home->qsbr->shared == exec->qsbr->shared`` as literally the same object. A
  goal minted by any worker is a value read out of ``shared->wr_seq`` and is
  sequenced in exactly the domain the home polls against (``shared->rd_seq``
  plus a scan of ``shared->array``), so reclamation is no earlier than globally
  quiescent. The per-thread ``qsbr`` *pointer* only selects the assertion target
  in ``_Py_qsbr_poll`` (which holds because the running thread state's own qsbr
  is passed); it does not change the observed sequence numbers.

The patch also redirects the page-list insert *and* the page-retire/process
walk to the same home, so a deferred page's heap and its reclaim list are always
the same worker — never divergent. Together with the per-interpreter shared domain,
this makes premature reclaim and leak both unreachable under the single-
interpreter invariant.

This argument **breaks** the instant a worker or per-fiber thread state is created
in a *different* interpreter (a sub-interpreter), since then the shared domains
diverge. The single-interpreter requirement is therefore a normative Contract
MUST (item 2). The only residual is optional: whether to *add* a debug assert
enforcing ``home->qsbr->shared == exec->qsbr->shared`` (see `Open Issues`_); the
precondition itself is satisfied structurally, not assumed.


Garbage collection
-------------------

GC objects are allocated from the home worker's mimalloc GC heaps and threaded
onto the home's ``page_list``, but ``_gc_thread_state.alloc_count`` (the
per-thread young-generation collection trigger) is incremented on the
*executing* (per-fiber) thread state. Both halves of this are now resolved:

* **Stop-the-world reachability — verified sound.** The free-threaded GC's
  stop-the-world scan walks objects via ``gc_visit_heaps`` over each *registered*
  thread state's own ``mimalloc`` heaps (iterating ``interp->threads.head``), not
  via the alloc-home indirection. Every worker is a normal registered, bound thread
  state whose heaps hold every fiber's borrow-allocated objects, so those heaps
  are always walked and **no live borrow-allocated object is missed**. An
  execution-only fiber thread state is also registered and bound, but its heaps
  stay empty, so walking them is a harmless no-op; it does not strand the
  stop-the-world handshake (a parked fiber's thread state is left detached and
  participates in the STW park/unpark protocol like any idle thread). The
  free-threaded GC has no thread-stack root scan — reachability is the
  refcount-difference model, which the borrow does not perturb — so a fiber's
  frames keep their referents alive through true refcounts regardless of where
  the memory physically sits.

* **alloc_count is a cadence concern, not a correctness one.** The authoritative
  young-generation count (``generations[0].count``) is **per-interpreter**; only
  the ``±512`` buffer ``alloc_count`` is per-thread-state. That buffer travels
  with the migrating fiber and is flushed to the per-interpreter count on every
  threshold crossing and unconditionally at thread-state teardown
  (``PyThreadState_Clear``). Charging it to the executing fiber therefore never
  loses a collection and never misses an object; it only shifts *when* the
  threshold flush happens. Redirecting ``alloc_count`` to the home (so a fiber
  allocating heavily charges the trigger to the thread state whose heap is
  actually growing) is an **optional cadence refinement**, not a required
  correctness fix. The previous draft's "``alloc_count`` MUST follow home /
  patch must be amended" requirement is withdrawn; this is reframed as optional
  tuning in `Open Issues`_.

One honest operational residual remains (not a correctness defect): every
empty-but-bound per-fiber thread state is still iterated by every full-thread-
list walk (the STW scan, the brc merge, the block count), so the STW pause cost
scales with the number of *live* fibers even though their heaps are empty. This
is a cadence/scaling cost, consistent with the separately-tracked high-fiber-
count STW wall, and is to be measured under `Performance`_.


C-API
-----

The public surface is a pair of ``PyUnstable_`` functions. The ``PyUnstable_``
prefix is CPython's convention for an API that is supported but may change between
feature releases without a deprecation period — which is exactly the right
stability tier for an advanced-embedding hook tied to the still-evolving
free-threaded allocator:

.. code-block:: c

   /* Set exec's allocation home to home. home == exec (or NULL) clears the borrow. */
   PyAPI_FUNC(void) PyUnstable_ThreadState_SetAllocHome(PyThreadState *exec,
                                                        PyThreadState *home);

   /* Returns exec's current allocation home, or NULL if none is set. */
   PyAPI_FUNC(PyThreadState *) PyUnstable_ThreadState_GetAllocHome(PyThreadState *exec);

A getter is part of the API, not optional: the misuse mode is silent heap
corruption, so debug tooling and external auditors must be able to read back the
current home.

The reference patch currently demonstrates the *mechanism* through a private,
header-only ``static inline _PyThreadState_SetAllocHome`` (no getter) in
``pycore_tstate.h``. Promoting it to the two exported ``PyUnstable_`` symbols
above (and adding the getter) is a mechanical change with no design content; it is
listed as an implementation follow-up in `Open Issues`_, not a design question.


Contract
--------

The following are normative MUSTs on the caller. Each is checked by a
``Py_DEBUG`` assertion (see `Security Implications`_):

1. **Same OS thread.** ``home`` MUST be attached to the same OS thread that is
   executing ``exec`` at the time ``exec`` allocates. This half of the contract
   is enforced in a debug build by mimalloc's own ``alloc.c:120``
   ``heap->thread_id == tid`` assertion, which is empirically the first thing to
   fire on misuse (see `Reference Implementation`_).

2. **Same interpreter.** ``home->interp == exec->interp``. mimalloc heaps, the
   abandoned pool (``interp->mimalloc.abandoned_pool``), ``mem_free_queue``, the
   brc table (``interp->brc.table``) and the QSBR shared sequence domain are all
   per-interpreter in the free-threaded build; a cross-interpreter borrow routes
   frees and merges into the wrong interpreter's queues, indexes ``ob_tid`` into
   the wrong brc table, and lets one interpreter's objects be walked by another's
   GC. This is enforced by a ``Py_DEBUG``-gated assertion in the setter
   (implemented). For a single-interpreter runtime it is also structurally
   guaranteed.

3. **At most one borrower per home at a time.** A given ``home`` MUST be borrowed
   by at most one executing thread state at any instant. This follows from
   item (1) but is load-bearing because durable heap selection is shared mutable
   state.

4. **Teardown drain thread.** A borrowing thread state's per-fiber brc table and
   ``mem_free_queue`` MUST be drained at teardown on a thread with a live,
   compatible QSBR.

A migrating runtime re-sets the home on every resume, before the fiber runs on
its new worker:

.. code-block:: c

   /* resuming fiber F on worker W (W's own thread state is W_ts): */
   PyUnstable_ThreadState_SetAllocHome(F->tstate, W_ts);
   PyEval_RestoreThread(F->tstate);  /* attach + run F on W; allocs hit W's heap */

Setting the home before attaching is load-bearing: on a cross-worker resume the
fiber's previous home is stale, so the borrow must be re-pointed before any
allocation under the fiber's thread state. To remove the stale-home footgun, a
scoped form is also specified (see `Security Implications`_).


Build gating
------------

The whole mechanism — the field, the accessor, and the redirect — is compiled in
only under a ``Py_TSTATE_ALLOC_HOME`` build macro (off by default). With the
macro off, the field is absent, ``_PyThreadStateImpl_AllocHome(tstate)`` expands
to ``(tstate)``, and the allocator fast path is byte-for-byte identical to stock
CPython (see `The _alloc_home field`_). The default build pays nothing.

This gating is presented as a transitional de-risking step, not the end state:
it permits an identical-source A/B comparison (macro-on vs macro-off) and buys
time to gather the perf data in `Performance`_. The intended trajectory is to
make the redirect unconditional once that data clears the threshold below; the
permanently-gated outcome is rejected (see `Rejected Ideas`_).


Backwards Compatibility
=======================

With the feature macro off (the default), there is no change of any kind: the
field is absent, the allocator fast path is unchanged, and no behavior changes
for any code, pure-Python or C. The build is byte-identical to stock CPython —
empirically corroborated by an OFF-build passing 593 standard-library tests with
zero regressions (which attests *only* to OFF-path identity; see `Reference
Implementation`_).

With the feature macro on, the field is appended **last** in
``_PyThreadStateImpl``, so no existing field's offset shifts. There is therefore
**no two-ABI hazard for field access**: an extension built against
``pycore_tstate.h`` without the macro reads ``qsbr`` and every other member at
the correct offset and is simply unaware of the harmless trailing field. The
only effect of the macro-on layout is that ``_PyThreadStateImpl`` grows by one
trailing pointer relative to a build that predates the field; this affects only
the internal, non-stable struct and only tools (debuggers, profilers) that
compute offsets into the *very end* of it. No stable public API or ABI is
altered.

This trailing-field design supersedes an earlier sketch that inserted the field
mid-struct (before ``asyncio_running_loop`` and ``qsbr``). That earlier sketch
*would* have created a real two-ABI hazard for the free-threading/advanced-
embedding audience this PEP targets — an interpreter built with the macro and an
extension built without it would silently disagree on every field offset after
the insertion point, with no diagnostic. Placing the field last closes that
hazard while keeping the byte-identical-when-off property.


Security Implications
=====================

There is **no new attack surface for Python code**: the mechanism is an
unstable, C-level embedder API with no pure-Python entry point.

There **is** a new C-level failure class: **silent cross-thread heap
corruption.** Violating the Contract (a home not attached to the executing OS
thread, or a cross-interpreter borrow) routes ``mi_heap_malloc`` to a heap owned
by a different OS thread. In a release build this is undetected metadata
corruption whose typical signature is the ``_mi_page_retire`` crash at teardown.

mimalloc already has the exact check (``mimalloc/alloc.c:120``:
``heap->thread_id == 0 || heap->thread_id == tid``), but it only trips in a debug
mimalloc, which release builds do not ship. This PEP therefore specifies CPython
``Py_DEBUG``-gated assertions, **gated on Py_DEBUG and not on the feature
macro**, in the setter and at the allocator fast path:

* in the setter: assert ``exec->interp == home->interp`` (Contract item 2) —
  **implemented** in the reference patch. The thread-affinity half of the
  contract (Contract item 1) is enforced by mimalloc's own ``alloc.c:120``
  ``heap->thread_id == tid`` assert, which empirically is the first thing to
  fire on a thread-affinity misuse; CPython may additionally assert at the
  setter that, when the home is non-``NULL``, the home's mimalloc heap
  ``thread_id`` equals ``_Py_ThreadId()``.

* on the allocator fast path: assert the selected home heap's ``thread_id``
  matches ``_Py_ThreadId()``.

On violation, ``Py_FatalError`` with a message naming the contract breached.
These are compiled out in release builds — the standard CPython assertion
pattern — with the mimalloc ``thread_id`` assert cited as precedent.

To eliminate the stale-home footgun (a parked fiber's home is stale by default,
and any allocation between attach and the next ``SetAllocHome`` — teardown,
exception unwind on the old worker, a finalizer, a signal callback, a preempt
landing the thread state between attach and the re-point — corrupts), the API
also specifies a scoped form: either an ``AttachWithAllocHome`` that sets the
home and attaches atomically, or auto-clear of ``_alloc_home`` on detach so a
re-attached thread state cannot allocate against a stale home before its home is
re-pointed. The scoped form is the recommended consumer entry point; the bare
setter is retained for runtimes that manage attach/detach themselves.


How to Teach This
=================

This is not an application-author feature. For authors of C extensions and
alternative runtimes (schedulers, actor systems, green-thread libraries) the
teaching is: a thread state may borrow another (same-OS-thread, same-interpreter)
thread state's allocator, so its execution may migrate between OS threads while
its allocations stay thread-local — provided the Contract is honored, which a
debug build checks. It belongs in the free-threading C-API / advanced-embedding
documentation alongside the thread-state lifecycle APIs.


Reference Implementation
========================

A patch against CPython 3.13t (``patches/cpython313t-tstate-alloc-home.patch``)
implements the build-gated, trailing ``_alloc_home`` field, the
``_PyThreadStateImpl_AllocHome`` accessor, the redirect at all the sites
enumerated in `The redirect`_, and the ``Py_DEBUG``-gated same-interpreter
assertion in the setter. The patch's *design* matches the Specification; the only
difference is mechanical — it demonstrates the mechanism through a private inline
``_PyThreadState_SetAllocHome`` rather than the exported ``PyUnstable_`` pair the
Specification proposes (see `C-API`_). Promoting the two symbols and adding the
getter is an implementation follow-up tracked in `Open Issues`_, not a design
question. The heap / page-list / free-list / ``current_object_heap`` /
``alloc_count`` questions the previous draft listed as required patch amendments
are now **resolved** (the patch's design is correct as written).

Validation
----------

State precisely what has and has not been validated. "Validated under pydebug"
is **not** "validated under TSan or perf"; those gaps are kept explicit below.

* **Companion-state soundness audit (against source).** An 8-way adversarial
  review verified all eight not-redirected / redirected companion-state paths
  (free lists, ``current_object_heap``, GC ``alloc_count`` + STW reachability,
  QSBR / page-list cross-domain, brc / the biased-refcount owner contract, raw
  ``PyMem_*``, redirect-set completeness, same-interpreter) against the 3.13.13 free-threaded
  source. All **8/8 resolved SOUND**, each verdict independently refuted.

* **End-to-end A/B under a live-assert pydebug interpreter (feature-ON).** On a
  CPython built ``Py_DEBUG=1`` + ``Py_GIL_DISABLED=1`` + ``Py_TSTATE_ALLOC_HOME=1``
  (so the ``brc.c:122``, ``mimalloc/alloc.c:120`` and QSBR asserts are *live*),
  a migration stress — 10 rounds of cross-worker fiber migration with interleaved
  cyclic-GC churn, stop-the-world collections, raw-buffer allocation, and
  **cross-thread decref of borrow-allocated tuples / dicts / lists** —
  **completes clean with 0 assertions** (borrow ON). The *same* stress with the
  borrow disabled (borrow OFF) aborts immediately at ``mimalloc/alloc.c:120``
  (``heap->thread_id == tid``). The OFF-path abort proves the stress genuinely
  drives the cross-thread-allocation hazard the borrow removes — i.e. the clean
  ON run is meaningful, not a stress that never reaches the dangerous path.

* **brc specifically.** Three targeted pydebug repros — bulk main-spawn, heavy
  cross-worker merge churn, and nested spawn (the exact bucket-collision topology
  the residual concerns) — fired ``brc.c:122`` **zero times in ~20 runs**.

* **Corroboration.** The earlier channel-churn repro passes **24/24** with the
  borrow versus **8/8 abort** without it; a gc-churn soak records a clean
  **12/12** under ``--with-pydebug --disable-gil`` for the per-worker borrow; and
  the OFF build passes **593 standard-library tests** (which
  attests only to OFF-path identity).

* **Migration probe (restated metric).** A direct probe runs 60 fibers across
  workers: **all 60 complete, 50 demonstrably resume on a different worker than
  they parked on, 10 resume on their origin worker** (``t0 == t1``: expected
  scheduling — the scheduler happened to pick the origin worker — **not** failures),
  **0 crashes.** The previous draft's "50/60 fibers migrated" wording wrongly
  read as a 17% failure rate.

**Honest framing (preserved).** This is *feasibility plus soundness-under-pydebug
from one in-house demonstrator* — an M:N stackful-coroutine runtime. An
allocation-bound perf microbenchmark has since been run (feature-on-unused within
noise) and the borrow path has been run under a fully TSan-instrumented
interpreter (no races in the borrow logic), but the **full ``pyperformance``
suite is still outstanding**, the TSan run leaves a localized consumer-side
instrumentation gap (not a borrow defect), and there is **not** yet a second
independent consumer (see `Performance`_ and `Open Issues`_). Historically the one
*confirmed* migration blocker was the mimalloc heap (``alloc.c:120``) — exactly
what alloc-home fixes — while brc was a feared-but-refuted concern.


Performance
===========

The default build (feature macro off) has zero cost: the field, the accessor and
the redirect branch are all absent and the build is byte-identical to stock.

The cost added to ``PyObject_Malloc``'s fast path is a dependent load of
``_alloc_home`` from ``_PyThreadStateImpl`` feeding the heap-pointer computation,
repeated at each redirect site, plus a branch. A correctly-predicted branch is
not free: it still issues the load and lengthens the allocation critical-path
dependency chain. An **allocation-bound microbenchmark** (the most fast-path-
sensitive case: tight tuple/list/dict/``object()`` churn, GC disabled) was run on
two free-threaded release CPythons built from one source, configured identically,
differing **only** by ``-DPy_TSTATE_ALLOC_HOME``, pinned to one core, medians over
two alternating passes of 21–25 runs each. The feature-on-unused redirect came in
**within run-to-run noise**: the median ON/OFF ratio was ``+0.03%`` and ``+0.73%``
across the two passes against a ``4–6%`` noise floor, and the more-stable
*minimum* statistic bounced both sides of zero (``+0.98%`` / ``−0.17%``) — i.e. no
measurable, consistent overhead on the case most able to show it. This is one
microbenchmark on one machine, not the full suite; the broader plan stands below.

This PEP commits to the following measurement plan as a precondition for making
the redirect unconditional. Until these numbers exist, the feature remains
build-gated.

A/B perf (feature-on-unused vs stock, same revision):

* ``pyperformance`` full suite.
* an allocation-bound microbenchmark: tight list/dict/tuple churn and
  ``object()`` allocation loops.
* GC-heavy cases: ``gc.collect()`` churn and cyclic-garbage creation.
* Method: frequency-pinned isolated box, ``PYTHON_GIL=0``, median of ≥20 runs,
  reported with confidence intervals.
* **Acceptance threshold:** ≤1% geometric-mean regression on ``pyperformance``
  and no single benchmark regressing >2%. If the unused path cannot meet this,
  the redirect stays build-gated and "unconditional" is abandoned.

**Status:** the allocation-bound microbenchmark above is **done** (within noise,
see opening paragraph). The full ``pyperformance`` suite and the GC-heavy A/B are
**not yet collected**; they remain the gating input for the
unconditional-vs-gated decision. The microbenchmark result is encouraging but is
not a substitute for the suite.

Borrow-path soundness (feature-on, used):

* **Done under pydebug.** The borrow path (``home != NULL``) under a migrating
  workload with ``gc.collect()`` churn, stop-the-world collections, raw-buffer
  allocation and cross-thread decref of borrow-allocated objects has been run
  under ``--with-pydebug --disable-gil`` with the feature macro on, so the
  ``mimalloc/alloc.c:120`` and ``brc.c:122`` assertions are live — **0
  assertions** (see `Reference Implementation`_).

* **Run under TSan — no borrow-path races; remaining reports localized to a
  consumer-side instrumentation gap.** The borrow path was run under a **fully
  TSan-instrumented** free-threaded CPython built with the flag (so CPython
  internals, not just the extension, are instrumented). The migration stress
  completed correctly — no crash, no assertion, correct results — and TSan
  surfaced **no races in the alloc-home borrow logic** (the field, the redirect,
  the setter, the per-resume re-point). It did surface ~128 reports in
  **mimalloc / CPython-object alloc-free internals on objects handed between
  fibers** (e.g. ``_mi_page_malloc`` vs ``mi_free``, ``Py_TYPE`` vs
  ``Py_SET_TYPE``). A control — the *same* cross-thread-free pattern with real OS
  threads (no fibers, no borrow) under the *same* instrumented interpreter — was
  **race-clean (0 reports)**, which localizes the reports to the runtime's fiber
  model rather than to mimalloc or the borrow: TSan cannot observe the
  happens-before that the runtime's channel / park / wake primitives establish
  *between fibers* (those primitives are not annotated with
  ``__tsan_acquire`` / ``__tsan_release``), so a fiber that allocates an object
  and a different fiber that frees it appear to race even though the program
  serialises them. The stack-swap annotations (``__tsan_switch_to_fiber``) are
  present and active but only bridge happens-before at switch points, not through
  channel handoffs. **This is a pre-existing consumer-side TSan-instrumentation
  gap, not a borrow defect.** A fully race-clean TSan run of the borrow path therefore
  requires first annotating the runtime's fiber synchronisation primitives — a
  separate, now-scoped effort — and a larger ≥100k-fiber scale run.

Steady-state migration cost and fragmentation:

* migration converts intra-fiber frees of pre-migration allocations into
  mimalloc cross-thread (atomic ``xthread_free``) frees; over a long-lived fiber
  that hops workers, its live objects are smeared across every worker heap it has
  visited, with page retirement only via cross-thread free plus QSBR on each
  owning worker. The plan reports: what wakes ``_mi_page_retire`` on a worker the fiber
  has left, worst-case resident pages on an otherwise-idle worker, and whether a
  fiber that owns a page's QSBR goal but has migrated away can stall a home worker's
  page reclamation. **Not yet measured.**

GC trigger cadence:

* the GC-trigger cadence under sustained migration vs the non-migrating
  baseline, to evaluate the optional ``alloc_count``-follows-home refinement (see
  `Garbage collection`_). **Not yet measured.**


Rejected Ideas
==============

* **One thread state per OS thread (no per-fiber execution state).** Cannot
  preserve per-fiber frames/exceptions across a yield, so no transparent
  stackful fibers. This is the gap, not a solution.

* **mimalloc abandon/adopt on each migration.** Designed for thread *death*;
  walks and reassigns heap pages. The PEP commits to quantifying the ratio of
  ``_mi_heap_abandon`` + adopt per migration against one ``_alloc_home`` store at
  representative per-fiber heap sizes (see `Performance`_); if adopt is two to
  three orders of magnitude more expensive, as expected, that figure is the
  single strongest argument for borrow and belongs here once measured.

* **Snapshot/copy the fiber's execution state into the worker's own thread
  state.** Unsound: the eval loop bakes the executing ``tstate`` pointer into the
  suspended frame's register / eval-frame spill slots (disassembly-confirmed),
  so it cannot be re-rooted onto a different thread state after the fact.

* **Make the allocator thread-agnostic (a shared global heap).** Reintroduces a
  global allocator lock (defeating free-threading) or demands a write-barriered
  concurrent allocator (research-grade). The borrow keeps mimalloc's lock-free
  per-thread fast path intact.

* **Permanently build-gated.** Rejected. A recompile-only feature gets zero
  buildbot coverage, bit-rots across every ``obmalloc`` refactor, and cannot
  serve the ecosystem the Motivation invokes (no wheel, no distribution
  interpreter ships it). The build gate is accepted only as a transitional
  de-risking step; the end state is unconditional, contingent on the
  `Performance`_ threshold.


Open Issues
===========

These are genuinely unresolved; none is hidden by downgrading it to prose. The
soundness questions the previous draft tracked here as blockers (free lists,
``current_object_heap``, ``alloc_count``, the brc (a)/(b) choice, the QSBR
shared-domain precondition) are **resolved** and have moved into the
Specification with their evidence; what remains below are the genuinely-open
items.

* **Export the ``PyUnstable_`` symbols (implementation follow-up).** The
  Specification commits to the public ``PyUnstable_ThreadState_SetAllocHome`` /
  ``GetAllocHome`` pair (see `C-API`_); the reference patch still demonstrates the
  mechanism via the private inline ``_PyThreadState_SetAllocHome`` and ships no
  getter. Promoting the two symbols and adding the getter is mechanical (no design
  content). This is the only remaining patch/spec difference; the
  ``current_object_heap`` / free-list / ``alloc_count`` questions the previous
  draft tracked here are resolved.

* **Performance A/B data (partial; full suite outstanding).** The
  allocation-bound microbenchmark is **done** — the feature-on-unused redirect is
  within run-to-run noise (see `Performance`_). The full ``pyperformance`` suite
  and the GC-heavy A/B are **not yet collected**, and remain the gating input for
  the unconditional-vs-gated decision.

* **TSan run of the borrow path (run; one gap localized).** A fully
  TSan-instrumented free-threaded CPython built with the flag ran the migration
  stress correctly with **no races in the borrow logic** (see `Performance`_).
  The remaining ~128 reports were localized — by a race-clean real-threads
  control — to the runtime's fiber synchronisation primitives lacking TSan
  happens-before annotations, **not** to the borrow. The remaining work is
  therefore (a) annotate the runtime's channel/park/wake primitives so a fully
  race-clean borrow-path TSan run is possible, and (b) a ≥100k-fiber-scale run.

* **brc bucket-order regression test (in place).** The brc safety (`Companion
  per-thread state and soundness`_) rests on an emergent, undocumented bucket
  *insertion-order* property, rated **medium confidence**. A regression *test*
  driving the exact bucket-collision topologies under ``--with-pydebug`` now
  exists (a regression test driving those collision topologies) and passes
  (``brc.c:122`` never fires); it converts the residual into a loud, early CI
  signal if a future
  CPython changes brc registration order. A C-level assertion pinning the
  property upstream remains optional follow-up, and migration should stay gated
  behind the alloc-home heap fix.

* **Second independent consumer.** The only consumer today is the author's own
  reference runtime. The PEP should either cite two or three independent would-be consumers
  (greenlet/gevent/eventlet, trio/anyio, a Stackless-class runtime) confirming
  this exact hook unblocks them, or honestly state its scope as "one
  demonstrator, generalizable." This bears directly on the cost/benefit case.

* **Sufficiency of the redirect set (ongoing audit cost).** The completeness of
  the redirect set (`The redirect`_) is *currently verified* against the
  CPython 3.13.13 ``obmalloc`` / ``pycore_object_alloc.h`` layout, but it must be
  re-audited on any allocator refactor. This is a real ongoing cost of the hook
  and part of why "permanently gated" is rejected (the hook must track the
  allocator upstream).

* **Optional / minor refinements (not correctness gaps):**

  - ``alloc_count``-follows-home cadence refinement — optional GC-timing tuning,
    to be evaluated against the cadence measurement in `Performance`_.
  - a hardened non-debug ("checked") assert mode for embedders who cannot ship a
    full debug build, given the silent-corruption consequence.
  - an optional debug assert enforcing the shared QSBR domain
    (``home->qsbr->shared == exec->qsbr->shared``); the precondition itself is
    satisfied structurally by the single per-interpreter ``_qsbr_shared``, so
    this is enforcement-belt-and-braces, not a correctness fix.


Copyright
=========

This document is placed in the public domain or under the CC0-1.0-Universal
license, whichever is more permissive.

"""TSan control: pure cross-thread free in stock free-threaded CPython.

This is the CONTROL for a borrow-path data-race investigation. Real OS threads
(no fibers, no userspace scheduler, no borrow) allocate objects and free them on
a different thread -- the same cross-thread-free pattern a migrating M:N runtime
produces.

Run it under a fully ThreadSanitizer-instrumented free-threaded interpreter
(build it with scripts/build_interpreters.sh tsan), e.g.:

    setarch "$(uname -m)" -R env PYTHON_GIL=0 \
        TSAN_OPTIONS="suppressions=<cpython-ft-suppressions>:halt_on_error=0" \
        ./build-tsan/python validation/tsan_xthread_free_control.py

EXPECTED: 0 ThreadSanitizer reports. mimalloc's cross-thread free is atomic and
TSan sees the synchronisation, so plain real-thread cross-thread free is
race-clean. This is the reference point that lets you distinguish a *real* race
introduced by the borrow from the noise an M:N fiber runtime produces under TSan:
TSan tracks happens-before per OS thread, so a runtime that hands an object from
one fiber to another (alloc in fiber A, free in fiber B) will trip apparent races
unless its channel / park / wake primitives are annotated with __tsan_acquire /
__tsan_release -- even though the program serialises the accesses. A clean run of
this control means the mimalloc machinery those apparent races touch is correctly
synchronised, so the apparent races localise to the runtime's fiber-sync
instrumentation, not to the allocator or the borrow.
"""
import queue
import threading

NPROD, NCONS, N = 4, 4, 6000


def producer(q):
    for i in range(N):
        # tuple + list + dict (keytable) -- the freelisted / raw-alloc types
        q.put(([object() for _ in range(8)], [i] * 6, {"a": i, "b": i + 1}))


def consumer(q):
    while True:
        x = q.get()
        if x is None:
            break
        del x                       # free on a DIFFERENT OS thread than allocated


def main():
    q = queue.Queue(maxsize=128)
    prods = [threading.Thread(target=producer, args=(q,)) for _ in range(NPROD)]
    cons = [threading.Thread(target=consumer, args=(q,)) for _ in range(NCONS)]
    for t in cons:
        t.start()
    for t in prods:
        t.start()
    for t in prods:
        t.join()
    for _ in range(NCONS):
        q.put(None)
    for t in cons:
        t.join()
    print("control done (expect 0 TSan reports)")


if __name__ == "__main__":
    main()

#!/usr/bin/env bash
# Build the free-threaded CPython 3.13 interpreters used to validate the
# alloc-home patch. Each is a separate, self-contained recipe; run the ones you
# need. Requires a CLEAN CPython 3.13.x source checkout (no in-tree build).
#
#   usage:  CPYTHON_SRC=/path/to/clean/cpython-3.13  bash build_interpreters.sh <target>
#   targets:  off   release FT, flag OFF        (perf baseline / byte-identical check)
#             on    release FT, flag ON         (perf "feature-on-unused")
#             pydebug   FT + --with-pydebug + flag   (the assert oracle)
#             tsan      FT + ThreadSanitizer + flag   (data-race detector)
#
# The patch (../patch/cpython313t-tstate-alloc-home.patch) is OFF by default:
# the field + redirect only compile in when Py_TSTATE_ALLOC_HOME is defined, so
# the OFF build is byte-identical to stock. Apply it to the source ONCE; the
# flag is what each ON/pydebug/tsan recipe toggles.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
PATCH="$HERE/../patch/cpython313t-tstate-alloc-home.patch"
SRC="${CPYTHON_SRC:?set CPYTHON_SRC to a clean CPython 3.13.x checkout}"
TARGET="${1:?pick a target: off|on|pydebug|tsan}"
JOBS="$(nproc)"

apply_patch_once() {
  if ! grep -q "_alloc_home" "$SRC/Include/internal/pycore_tstate.h"; then
    ( cd "$SRC" && patch -p1 < "$PATCH" )
    echo "[build] applied alloc-home patch to $SRC"
  fi
}

# Out-of-tree build dir so the recipes don't collide. CPython refuses out-of-tree
# if the source has an in-tree build -- keep $SRC clean (git clean -dfx, or a
# fresh checkout).
build() {  # build <dirname> <configure-args...>
  local dir="$HERE/../build-$1"; shift
  rm -rf "$dir"; mkdir -p "$dir"
  ( cd "$dir" && "$SRC/configure" "$@" >configure.log 2>&1 && make -j"$JOBS" >build.log 2>&1 )
  echo "[build] $dir/python  ($([ -x "$dir/python" ] && echo OK || echo FAILED))"
}

apply_patch_once
case "$TARGET" in
  off)     build off     --disable-gil ;;
  on)      build on      --disable-gil CPPFLAGS="-DPy_TSTATE_ALLOC_HOME" ;;
  pydebug) build pydebug --disable-gil --with-pydebug CPPFLAGS="-DPy_TSTATE_ALLOC_HOME" ;;
  tsan)    build tsan    --disable-gil --with-thread-sanitizer CPPFLAGS="-DPy_TSTATE_ALLOC_HOME" ;;
  *) echo "unknown target: $TARGET"; exit 2 ;;
esac

cat <<'NOTE'

[next steps]
  perf A/B (needs off + on):
      python validation/perf_alloc_microbench.py --ab build-off/python build-on/python

  off-build byte-identical / stdlib check (needs off):
      ./build-off/python -m test -j0 test_dict test_list test_gc test_weakref test_threading

  TSan control (needs tsan):
      setarch "$(uname -m)" -R env PYTHON_GIL=0 \
        TSAN_OPTIONS="suppressions=$SRC/Tools/tsan/suppressions_free_threading.txt:halt_on_error=0" \
        ./build-tsan/python validation/tsan_xthread_free_control.py    # expect 0 reports

  Migration / assertion soundness needs a consuming M:N runtime that moves
  execution across OS-thread workers; it is demonstrated by a reference runtime
  (published separately). NB for any consumer extension: setuptools build_ext
  tracks .c mtimes, NOT CPython headers -- after a header-only patch change
  rebuild the extension with `build_ext --force`, or it relinks a stale .o at the
  old struct offset (a spurious cross-thread-alloc abort).
NOTE

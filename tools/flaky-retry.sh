#!/usr/bin/env bash
# Run one command; if it fails with the known flaky MLIR-pass crash
# (`isContiguousAccess` / `properlyDominates` in the compiler's stack dump),
# run it once more and say so on a line canary.py counts.
#
# For checks that compile many programs in one step: canary.py retries a
# failed step once, which is not enough when the step is thirty compiles and
# any one of them can hit the crash. Wrap each compile instead:
#
#   pixi run "$CANARY_TOOLS/flaky-retry.sh" mojo -I . tests/test_x.mojo
set -u
out=$(mktemp)
"$@" 2>&1 | tee "$out"
rc=${PIPESTATUS[0]}
if [ "$rc" -ne 0 ] && grep -qE 'isContiguousAccess|properlyDominates' "$out"; then
  echo "canary: flaky-retry: $*"
  "$@"
  rc=$?
fi
rm -f "$out"
exit "$rc"

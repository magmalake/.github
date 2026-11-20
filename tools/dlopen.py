#!/usr/bin/env python3
"""Load each shared library named on the command line; exit 1 if one fails.

The canary's shared-library checks link a Mojo library and then load it,
because a symbol the compiler referenced but never emitted fails at the link on
macOS and only at `dlopen` on Linux (parquet.mojo#74). Run it inside the tin's
environment (`pixi run python3 tools/dlopen.py build/lib*.so`) so the Mojo
runtime libraries in $CONDA_PREFIX/lib resolve the same way they do for the
library's real consumers.
"""

import ctypes
import os
import sys


def main(paths: list[str]) -> int:
    if not paths:
        print("dlopen.py: no library given (did the build produce one?)")
        return 1
    bad = 0
    for path in paths:
        try:
            ctypes.CDLL(path, mode=os.RTLD_NOW)
            print(f"loaded {path}")
        except OSError as e:
            print(f"error: dlopen {path}: {e}")
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

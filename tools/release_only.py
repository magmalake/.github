#!/usr/bin/env python3
"""Decide whether a pull request is a release PR, from its diff alone.

A release PR does one thing: it names a new version. Everything it changes was
already tested on the PR that wrote the code, so re-running the whole suite
(macOS, nightly, benchmarks, fuzzing, kind, the data shards) buys nothing but
a ten-minute wait per tin, one tin after another down a release chain. The
2026-11-05 parquet -> iceberg -> orca cascade spent most of its hour that way.

The catch is that "looks like a release" is easy to fool. A PR titled "Release
0.9.0" that also edits a source file is a feature PR, and it must get the full
suite. So the decision is made from the diff, line by line, and anything the
rules below do not recognise sends the PR to the full suite:

  * `shelf.toml` and `pixi.toml`: a `version = "..."` line at the left margin
    (the manifest's [workspace] or [package] version, or the tin's), and a
    dependency's `rev = "<hex>"`, provided that is the only thing the line's
    change touched.
  * `CHANGELOG.md`: anything.
  * `pixi.lock` (any directory): anything. It is generated.
  * `.github/workflows/*.yml`: a 40-hex commit replaced by another on a line
    that is otherwise unchanged (sibling checkouts pinned by commit), and
    comment lines.
  * recipe files (`recipe.yaml`, `meta.yaml`): a `version:` line.

Adding, deleting, renaming or chmod-ing any other file, a binary change, an
empty diff: full suite. A dependency line added or removed rather than
re-pinned: full suite.

Usage:
    python3 tools/release_only.py [DIFF_FILE]      # stdin when omitted

Prints `release-only` or `full suite: <why>` and exits 0 or 1 accordingly
(2 when it cannot read the diff, which a caller should treat as "full suite").
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from dataclasses import dataclass, field

# A manifest's own version, at the left margin. Dependency tables written as
# `[dependencies.foo]` with a `version =` line are indented or rare enough that
# the worst case is a missed full suite on a PR that also changed code, which
# the other rules reject anyway.
TOML_VERSION = re.compile(r'^version\s*=\s*"[^"\s]*"\s*(#.*)?$')
RECIPE_VERSION = re.compile(
    r'^\s*version:\s*"?[^\s"#]+"?\s*(#.*)?$'
    r'|^\s*\{%\s*set\s+version\s*=\s*"[^"]*"\s*%\}\s*$'
)
REV = re.compile(r'\brev\s*=\s*"[0-9a-f]{7,40}"')
HEX40 = re.compile(r'\b[0-9a-f]{40}\b')
COMMENT = re.compile(r'^\s*#')

TOML_FILES = {"shelf.toml", "pixi.toml"}
RECIPE_FILES = {"recipe.yaml", "recipe.yml", "meta.yaml"}


@dataclass
class FileDiff:
    old: str
    new: str
    special: list[str] = field(default_factory=list)  # new/deleted/rename/binary
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)


def parse(diff: str) -> list[FileDiff]:
    """Split a unified `git diff` into files with their changed lines."""
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    in_hunk = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(m.group(1), m.group(2)) if m else FileDiff("?", "?")
            if not m:
                cur.special.append("unparsable header")
            files.append(cur)
            in_hunk = False
        elif cur is None:
            continue
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith(("+", "-")):
            (cur.added if line[0] == "+" else cur.removed).append(line[1:])
        elif in_hunk:
            continue  # context, "\\ No newline at end of file", or a blank context line
        elif not in_hunk:
            # Header lines. `index`, `---` and `+++` carry nothing; the rest
            # (new file, deleted file, rename, mode, Binary files) mean the
            # file is not merely edited.
            if not line.startswith(("index ", "--- ", "+++ ", "similarity ")):
                cur.special.append(line)
    return files


def _paired(removed: list[str], added: list[str], pattern: re.Pattern[str]) -> bool:
    """True when `added` is `removed` with only `pattern` matches replaced.

    Every line must contain a match, so that a line that merely moved, or one
    whose other text changed, does not pass.
    """
    if not removed or not added:
        return False
    if not all(pattern.search(x) for x in removed + added):
        return False
    return Counter(pattern.sub("\0", x) for x in removed) == Counter(
        pattern.sub("\0", x) for x in added
    )


def why_not(f: FileDiff) -> str | None:
    """The reason this file's change needs the full suite, or None."""
    name = f.new.rsplit("/", 1)[-1]
    if name == "pixi.lock" and f.old == f.new:
        return None  # generated; any content
    if f.old != f.new:
        return f"{f.old} renamed to {f.new}"
    if f.special:
        return f"{f.new}: {f.special[0]}"
    if not f.removed and not f.added:
        return f"{f.new}: no line changes (mode or binary change)"
    if f.new == "CHANGELOG.md":
        return None

    if name in TOML_FILES:
        rest_removed = [x for x in f.removed if not TOML_VERSION.match(x)]
        rest_added = [x for x in f.added if not TOML_VERSION.match(x)]
        if not rest_removed and not rest_added:
            return None
        if _paired(rest_removed, rest_added, REV):
            return None
        return f"{f.new}: changes more than version and rev values"

    if name in RECIPE_FILES:
        if all(RECIPE_VERSION.match(x) for x in f.removed + f.added):
            return None
        return f"{f.new}: changes more than the version"

    if f.new.startswith(".github/workflows/") and f.new.endswith((".yml", ".yaml")):
        code_removed = [x for x in f.removed if not COMMENT.match(x)]
        code_added = [x for x in f.added if not COMMENT.match(x)]
        if not code_removed and not code_added:
            return None  # comments only
        if _paired(code_removed, code_added, HEX40):
            return None
        return f"{f.new}: changes more than pinned commits"

    return f"{f.new}: not a release file"


def classify(diff: str) -> tuple[bool, str]:
    """(release_only, explanation) for a unified diff."""
    files = parse(diff)
    if not files:
        return False, "empty diff"
    for f in files:
        reason = why_not(f)
        if reason:
            return False, reason
    return True, f"{len(files)} file(s), all version, changelog, pin or lock changes"


def main() -> int:
    args = sys.argv[1:]
    if len(args) > 1 or args[:1] in (["-h"], ["--help"]):
        print(__doc__)
        return 2
    try:
        text = open(args[0], encoding="utf-8", errors="replace").read() if args else sys.stdin.read()
    except OSError as e:
        print(f"full suite: cannot read the diff ({e})")
        return 2
    ok, why = classify(text)
    print("release-only" if ok else f"full suite: {why}")
    if ok:
        print(why, file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

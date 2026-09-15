#!/usr/bin/env python3
"""Find magmalake locks that have quietly frozen a sibling at an old commit.

A pixi dependency written without a rev --

    zstd-mojo = { git = "https://github.com/magmalake/zstd.mojo" }

-- means "track that repository". It does not behave that way: `pixi.lock`
records whatever it first resolved and never moves again unless somebody runs
`pixi update`. Nothing warns, and the include path does not save you, because a
pixi git-dependency package wins over `-I ../zstd.mojo/src`: the fixed source
sits on the include path being ignored while the build links the old package.

That is not hypothetical. On 2026-09-14 three repositories were still pinned at
zstd.mojo's *first* commit, which predates its process-wide `OwnedDLHandle` and
so dlopened the shim on every decompressed Parquet page. A 79.5M-row scan took
2687 ms instead of 262 ms. It had been that way for two weeks, and every
repository's own test suite passed the whole time, because the cost is per page
and their fixtures have few pages.

So this asks one question of every repository in the org: is a **rev-less**
dependency locked to something the sibling moved past, and has it been that way
long enough that nobody is simply mid-merge?

A dependency pinned with an explicit `rev` is deliberate and never reported --
taxibench.example pins the published tins it benchmarks against on purpose, and
a check that nagged about those would teach people to ignore it.

Usage:
    python3 tools/check_dep_drift.py [--days N] [--org NAME]

Exits non-zero when something has drifted, so a scheduled run goes red.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys

DECLARATION = re.compile(
    r'^\s*([A-Za-z0-9_-]+)\s*=\s*\{([^}\n]*'
    r'git\s*=\s*"https://github\.com/(?P<org>[A-Za-z0-9_.-]+)/'
    r'(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?"[^}\n]*)\}',
    re.M,
)


def gh(*args: str) -> str:
    """One `gh api` call, empty string when the endpoint says no."""
    out = subprocess.run(
        ["gh", "api", *args], capture_output=True, text=True, timeout=120
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def repo_file(org: str, repo: str, path: str) -> str:
    return gh(
        f"repos/{org}/{repo}/contents/{path}",
        "-H", "Accept: application/vnd.github.raw",
    )


def head_of(org: str, repo: str) -> tuple[str, dt.datetime] | tuple[None, None]:
    raw = gh(f"repos/{org}/{repo}/commits/HEAD")
    if not raw:
        return None, None
    data = json.loads(raw)
    when = data["commit"]["committer"]["date"]
    return data["sha"], dt.datetime.fromisoformat(when.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--days",
        type=int,
        default=7,
        help="Only report a pin whose upstream commit is older than this, so a"
        " repository that is merely mid-merge is not reported (default: 7).",
    )
    ap.add_argument("--org", default="magmalake")
    args = ap.parse_args()

    repos_raw = gh(
        f"orgs/{args.org}/repos?per_page=100", "--jq", ".[].name"
    )
    repos = [r for r in repos_raw.splitlines() if r]
    if not repos:
        print("could not list the org's repositories", file=sys.stderr)
        return 2

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=args.days)
    heads: dict[str, tuple] = {}
    findings: list[tuple[str, str, str, str, str, int]] = []
    checked = 0

    for repo in sorted(repos):
        toml = repo_file(args.org, repo, "pixi.toml")
        if not toml:
            continue
        lock = repo_file(args.org, repo, "pixi.lock")
        if not lock:
            continue
        checked += 1
        for m in DECLARATION.finditer(toml):
            name, decl, dep = m.group(1), m.group(2), m.group("repo")
            if m.group("org") != args.org or "rev" in decl:
                continue  # a deliberate pin, or somebody else's repository
            pinned = sorted(
                set(
                    re.findall(
                        r"git\+https://github\.com/"
                        + re.escape(args.org)
                        + "/"
                        + re.escape(dep)
                        + r"(?:\.git)?(?:\?[^#]*)?#([0-9a-f]{40})",
                        lock,
                    )
                )
            )
            if not pinned:
                continue
            if dep not in heads:
                heads[dep] = head_of(args.org, dep)
            sha, when = heads[dep]
            if not sha or when is None or when > cutoff:
                continue
            for locked in pinned:
                if locked != sha:
                    age = (dt.datetime.now(dt.timezone.utc) - when).days
                    findings.append(
                        (repo, name, dep, locked[:7], sha[:7], age)
                    )

    print(f"checked {checked} repositories in {args.org}\n")
    if not findings:
        print("no rev-less dependency is behind its sibling.")
        return 0

    width = max(len(f[0]) for f in findings)
    print(f"{'repository':<{width}}  {'dependency':<20} {'locked':<9} "
          f"{'upstream':<9} behind since")
    for repo, name, dep, locked, sha, age in sorted(set(findings)):
        print(f"{repo:<{width}}  {name:<20} {locked:<9} {sha:<9} {age}d")

    print(
        "\nThese say 'track the sibling' and do not. Refresh each one with\n"
        "  pixi update <dep> <dep>-shim\n"
        "in the repository that holds it, then commit the lock. Pin a rev\n"
        "explicitly if the old commit was the point."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

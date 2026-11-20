#!/usr/bin/env python3
"""Nightly canary: build and test every magmalake tin against one Mojo nightly.

Every tin pins one exact Mojo nightly (`mojo-compiler ==<version>`), so a
compiler or stdlib change that breaks a tin is only noticed when somebody
moves the pin, often weeks later and in the middle of something else. This
moves the pin for them, every day, in a throwaway work directory:

1. Resolve the target compiler: `--version`, else the newest `mojo-compiler`
   on the max-nightly channel (by upload time; the channel also carries old
   25.x builds that sort above 1.x by version).
2. Clone every enabled tin in tools/canary-tins.toml at its `ref` (main) into
   `<work>/<dir>`, as siblings, so `-I ../x.mojo/src` resolves as in CI.
3. Rewrite each checkout in place: every exact Mojo/MAX pin to the target
   (`mojo-compiler`, `mojo`, `mojo-python`, and `max`, `max-core`, `modular` to
   the MAX build of the same night), in every pixi.toml and recipe.yaml; and
   every git dependency on a sibling tin to a path dependency on the sibling's
   checkout. A git-pinned sibling would still carry the old compiler pin and
   the solve would fail; as path dependencies the whole graph is built from
   the rewritten sources, by the target compiler. The locks are kept as solver
   hints but never enforced (no --locked / --frozen).
4. Run each tin's checks in order, with a timeout, one log per tin. A tin whose
   dependency failed is skipped ("blocked by X"), not run. A failure whose log
   shows the known flaky MLIR-pass crash (`isContiguousAccess` /
   `properlyDominates`) is retried once and reported as flaky.
5. Write a Markdown summary (also to $GITHUB_STEP_SUMMARY), a JSON result file
   and the logs to `<out>`; exit 1 when anything failed or was blocked.

Usage:
    python3 tools/canary.py [--version 1.3.0.dev2026111905] [--work DIR]
                            [--out DIR] [--only a,b] [--rewrite-only]
    python3 tools/canary.py --resolve        # print the newest nightly

`--work` defaults to ./canary-work. An existing checkout there is reused as
it is (not reset), so a second run can retry one tin with `--only`; pass
`--fresh` to delete and re-clone. Do not point it at checkouts you work in:
their pixi.toml files are rewritten.

On macOS with an SDK the conda linker cannot read (`libSystem.tbd: unknown
architecture`), pass `--macos-sdk /Library/Developer/CommandLineTools/SDKs/
MacOSX26.5.sdk`: C-shim builds that fail that way get their SDKROOT
activation script pointed at it and are retried.

Adding a tin: one [[tin]] block in tools/canary-tins.toml, after everything it
consumes. Stdlib only, Python 3.11+.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
DEFAULT_TINS = HERE / "canary-tins.toml"
DEFAULT_CHANNEL = "https://conda.modular.com/max-nightly"

# Packages versioned with the Mojo compiler, and with MAX, respectively.
MOJO_PACKAGES = ("mojo-compiler", "mojo", "mojo-python")
MAX_PACKAGES = ("max-core", "max", "modular")

# The intermittent MLIR-pass crash (roughly 1 compile in 15 on the 1.3
# nightlies); it passes on retry.
FLAKY = re.compile(r"isContiguousAccess|properlyDominates")
# Xcode 27's SDK against conda's linker, macOS only; see --macos-sdk.
SDK_BREAK = re.compile(r"libSystem\.tbd.*unknown architecture")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
ERROR_LINE = re.compile(
    r"(\berror\b:|^error\b|\bFAIL(ED)?\b|\w+Error: |panicked|Segmentation fault|"
    r"Assertion|undefined symbol|Undefined symbols|failed to|\u00d7 |timed out)"
)
# Lines that look like errors and are not: test names, the task echo, the
# crash reporter's own chatter.
NOT_ERROR = re.compile(r"^\s*(PASS\b|ok:|\u2728 Pixi task|\$ |\[\d+:\d+:\d+)")
CRASH = re.compile(r"Please submit a bug report to https://github\.com/modular|^Stack dump:", re.M)
CRASH_ARGS = re.compile(r"^\d+\.\s+Program arguments: (?P<args>.*)$", re.M)


# ── pins ────────────────────────────────────────────────────────────────────

# An exact conda spec: `==V`, `=V` or a bare `V`, nothing else.
EXACT_SPEC = re.compile(r"^(?P<op>==|=)?\s*(?P<ver>[0-9][0-9A-Za-z._+-]*)$")


def repin(spec: str, target: str) -> str | None:
    """`spec` with its version replaced by `target`, or None if not exact."""
    m = EXACT_SPEC.match(spec.strip())
    if not m or "*" in spec:
        return None
    return f"{m.group('op') or ''}{target}"


# ── sibling URLs ────────────────────────────────────────────────────────────

GITHUB = re.compile(
    r"^(?:https?://|git\+https://|ssh://git@|git@)github\.com[/:]"
    r"(?P<org>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$"
)


def repo_key(url: str) -> str | None:
    """`org/repo`, lowercased, for a GitHub URL; None for anything else."""
    m = GITHUB.match(url.strip())
    return f"{m.group('org')}/{m.group('repo')}".lower() if m else None


# ── pixi.toml rewriting ─────────────────────────────────────────────────────

HEADER = re.compile(r"^\s*\[\[?\s*(?P<name>[^\]]+?)\s*\]\]?\s*(#.*)?$")
KEYVAL = re.compile(
    r"^(?P<indent>\s*)(?P<key>[A-Za-z0-9_-]+|\"[^\"]+\")(?P<eq>\s*=\s*)(?P<rest>.*)$"
)
GIT_KEYS = ("git", "rev", "branch", "tag", "subdirectory")


def _is_dep_table(header: str) -> bool:
    parts = [p.strip().strip('"') for p in header.split(".")]
    return any(
        p.endswith("dependencies") or p.endswith("constraints") for p in parts[-2:]
    )


def _scan_value(rest: str) -> tuple[str, str]:
    """Split `rest` into a single-line TOML value and the trailing text."""
    if rest.startswith('"'):
        i = 1
        while i < len(rest):
            if rest[i] == "\\":
                i += 2
                continue
            if rest[i] == '"':
                return rest[: i + 1], rest[i + 1 :]
            i += 1
        raise ValueError("unterminated string")
    if rest.startswith("{"):
        depth, i, in_str = 0, 0, None
        while i < len(rest):
            c = rest[i]
            if in_str:
                if c == "\\" and in_str == '"':
                    i += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c in "\"'":
                in_str = c
            elif c in "{[":
                depth += 1
            elif c in "}]":
                depth -= 1
                if depth == 0:
                    return rest[: i + 1], rest[i + 1 :]
            i += 1
        raise ValueError("inline table does not close on its line")
    m = re.match(r"^[^#\s]*", rest)
    return m.group(0), rest[m.end() :]


def _toml_str(s: str) -> str:
    return json.dumps(s)


def _inline(table: dict) -> str:
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, str):
            return _toml_str(v)
        if isinstance(v, (int, float)):
            return str(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        raise ValueError(f"cannot write {v!r} inline")

    return "{ " + ", ".join(f"{k} = {val(v)}" for k, v in table.items()) + " }"


@dataclasses.dataclass
class Rewrite:
    text: str
    changes: list[str]
    siblings: set[str]  # sibling dirs this file depends on
    old_pins: dict[str, set[str]]  # package -> versions it was pinned to


def rewrite_pixi_toml(
    text: str,
    pins: dict[str, str],
    siblings: dict[str, pathlib.Path],
    manifest_dir: pathlib.Path,
) -> Rewrite:
    """Re-pin a pixi.toml and turn sibling git dependencies into path ones.

    `pins` maps package name -> target version; `siblings` maps `org/repo`
    (lowercase) -> the sibling's checkout. Only dependency tables are touched,
    only single-line entries are rewritten, and the result is checked against
    the same transformation applied to the parsed document, so anything this
    cannot rewrite exactly is an error rather than a silently stale pin.
    """
    out, changes, used = [], [], set()
    old: dict[str, set[str]] = {}
    table = ""
    for lineno, line in enumerate(text.splitlines(keepends=True), 1):
        body = line.rstrip("\r\n")
        nl = line[len(body) :]
        h = HEADER.match(body)
        if h and not KEYVAL.match(body):
            table = h.group("name")
            out.append(line)
            continue
        m = KEYVAL.match(body)
        if not m or not _is_dep_table(table):
            out.append(line)
            continue
        key = m.group("key").strip('"')
        rest = m.group("rest")
        try:
            value, tail = _scan_value(rest)
        except ValueError:
            out.append(line)  # multi-line value; the parse check catches it
            continue
        new_value = value
        if key in pins and value.startswith('"'):
            spec = tomllib.loads(f"v = {value}")["v"]
            new = repin(spec, pins[key])
            if new is not None:
                old.setdefault(key, set()).add(EXACT_SPEC.match(spec.strip())["ver"])
                new_value = _toml_str(new)
        elif value.startswith("{"):
            tbl = tomllib.loads(f"v = {value}")["v"]
            sib = repo_key(tbl.get("git", "")) if "git" in tbl else None
            if sib in siblings:
                dest = siblings[sib]
                if "subdirectory" in tbl:
                    dest = dest / tbl["subdirectory"]
                rel = os.path.relpath(dest, manifest_dir).replace(os.sep, "/")
                kept = {k: v for k, v in tbl.items() if k not in GIT_KEYS}
                new_value = _inline({"path": rel, **kept})
                used.add(siblings[sib].name)
            elif key in pins and isinstance(tbl.get("version"), str):
                new = repin(tbl["version"], pins[key])
                if new is not None:
                    old.setdefault(key, set()).add(
                        EXACT_SPEC.match(tbl["version"].strip())["ver"]
                    )
                    tbl["version"] = new
                    new_value = _inline(tbl)
        if new_value != value:
            changes.append(f"{lineno}: [{table}] {key}: {value} -> {new_value}")
            line = f"{m.group('indent')}{m.group('key')}{m.group('eq')}{new_value}{tail}{nl}"
        out.append(line)
    result = "".join(out)
    _verify(text, result, pins, siblings, manifest_dir)
    return Rewrite(result, changes, used, old)


def _expected(doc, pins, siblings, manifest_dir, in_deps=False):
    """The parsed-document version of rewrite_pixi_toml, for _verify."""
    if not isinstance(doc, dict):
        return doc
    res = {}
    for k, v in doc.items():
        deps_here = k.endswith("dependencies") or k.endswith("constraints")
        if in_deps:
            if isinstance(v, str) and k in pins:
                v = repin(v, pins[k]) or v
            elif isinstance(v, dict):
                sib = repo_key(v.get("git", "")) if "git" in v else None
                if sib in siblings:
                    dest = siblings[sib]
                    if "subdirectory" in v:
                        dest = dest / v["subdirectory"]
                    rel = os.path.relpath(dest, manifest_dir).replace(os.sep, "/")
                    v = {"path": rel, **{a: b for a, b in v.items() if a not in GIT_KEYS}}
                elif k in pins and isinstance(v.get("version"), str):
                    v = {**v, "version": repin(v["version"], pins[k]) or v["version"]}
                else:
                    # A sub-table of a dependency table
                    # ([package.extra-dependencies.gpu]) is one too.
                    v = _expected(v, pins, siblings, manifest_dir, True)
            res[k] = v
        else:
            res[k] = _expected(v, pins, siblings, manifest_dir, deps_here)
    return res


def _verify(before, after, pins, siblings, manifest_dir):
    want = _expected(tomllib.loads(before), pins, siblings, manifest_dir)
    got = tomllib.loads(after)
    if want != got:
        raise ValueError(
            "pixi.toml rewrite did not come out as expected (a multi-line "
            "dependency entry or a [dependencies.x] table?); refusing to run "
            "with a partially rewritten manifest"
        )


# ── recipe.yaml rewriting ───────────────────────────────────────────────────

RECIPE_PIN = re.compile(
    r"^(?P<lead>\s*-\s*)(?P<name>[A-Za-z0-9_.-]+)(?P<sp>\s+)(?P<spec>(?:==|=)?\s*"
    r"[0-9][0-9A-Za-z._+-]*)(?P<tail>\s*(#.*)?)$"
)
RECIPE_GIT = re.compile(r"^(?P<lead>\s*-\s*)git:\s*(?P<url>\S+)\s*(#.*)?$")
RECIPE_REV = re.compile(r"^(?P<lead>\s*)rev:\s*(?P<rev>\S+)(?P<tail>\s*(#.*)?)$")


def rewrite_recipe(
    text: str, pins: dict[str, str], heads: dict[str, str]
) -> tuple[str, list[str], dict[str, set[str]]]:
    """Re-pin a rattler-build recipe.yaml and move sibling git sources.

    Requirements written `- mojo-compiler ==V` move to the target. A source
    `- git: <sibling>` with a `rev:` moves to the sibling checkout's HEAD
    (`heads` maps `org/repo` -> sha), so a precompiled .mojoc is built against
    the same tree the rest of the graph uses.
    """
    lines = text.splitlines(keepends=True)
    changes, old = [], {}
    git_item: tuple[int, str] | None = None  # (indent of "- ", sibling key)
    for i, line in enumerate(lines):
        body = line.rstrip("\r\n")
        nl = line[len(body) :]
        g = RECIPE_GIT.match(body)
        if g:
            key = repo_key(g.group("url"))
            git_item = (len(g.group("lead")), key) if key in heads else None
            continue
        if git_item and body.strip():
            indent = len(body) - len(body.lstrip())
            if body.lstrip().startswith("-") and indent <= git_item[0]:
                git_item = None
            elif indent < git_item[0] - 1:
                git_item = None
            else:
                r = RECIPE_REV.match(body)
                if r:
                    new = f"{r.group('lead')}rev: {heads[git_item[1]]}{r.group('tail')}"
                    if new != body:
                        changes.append(f"{i + 1}: source {git_item[1]} rev -> {heads[git_item[1]]}")
                        lines[i] = new + nl
                    git_item = None
                    continue
        p = RECIPE_PIN.match(body)
        if p and p.group("name") in pins:
            spec = p.group("spec")
            new = repin(spec, pins[p.group("name")])
            if new is not None and new != spec.replace(" ", ""):
                old.setdefault(p.group("name"), set()).add(EXACT_SPEC.match(spec)["ver"])
                changes.append(f"{i + 1}: {p.group('name')} {spec} -> {new}")
                lines[i] = f"{p.group('lead')}{p.group('name')}{p.group('sp')}{new}{p.group('tail')}{nl}"
    return "".join(lines), changes, old


# ── target version ──────────────────────────────────────────────────────────


def conda_subdir() -> str:
    machine = platform.machine().lower()
    if sys.platform == "darwin":
        return "osx-arm64" if machine in ("arm64", "aarch64") else "osx-64"
    return "linux-aarch64" if machine in ("arm64", "aarch64") else "linux-64"


def fetch_repodata(channel: str, subdir: str) -> dict:
    url = f"{channel.rstrip('/')}/{subdir}/repodata.json"
    req = urllib.request.Request(url, headers={"User-Agent": "magmalake-canary"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.load(r)
        except OSError:
            if attempt == 2:
                raise
            time.sleep(5)
    raise AssertionError


def _records(repodata: dict, name: str) -> list[dict]:
    recs = {**repodata.get("packages", {}), **repodata.get("packages.conda", {})}
    return [r for r in recs.values() if r.get("name") == name]


def newest_mojo(repodata: dict) -> str:
    """The `mojo-compiler` version uploaded last that also has a `mojo`.

    By upload time, not by version: the channel still carries 25.x builds from
    before Mojo's own version numbers, and those sort above 1.x.
    """
    mojo = {r["version"] for r in _records(repodata, "mojo")}
    recs = [r for r in _records(repodata, "mojo-compiler") if r["version"] in mojo]
    if not recs:
        raise SystemExit("no mojo-compiler with a matching mojo on the channel")
    return max(recs, key=lambda r: (r.get("timestamp", 0), r["version"]))["version"]


def max_for(repodata: dict, mojo_version: str) -> str | None:
    """The MAX version built with `mojo_version` (max-core depends on it)."""
    want = f"mojo-compiler =={mojo_version}"
    for r in _records(repodata, "max-core"):
        if any(d.replace(" ", "") == want.replace(" ", "") for d in r.get("depends", [])):
            return r["version"]
    return None


def target_pins(mojo_version: str, max_version: str | None) -> dict[str, str]:
    pins = {name: mojo_version for name in MOJO_PACKAGES}
    if max_version:
        pins.update({name: max_version for name in MAX_PACKAGES})
    return pins


# ── tins ────────────────────────────────────────────────────────────────────


@dataclasses.dataclass
class Check:
    name: str
    run: str


@dataclasses.dataclass
class Tin:
    dir: str
    repo: str
    url: str
    ref: str
    checks: list[Check]
    needs: list[str]
    dev_only: bool
    timeout: int
    enabled: bool
    note: str


def load_tins(path: pathlib.Path) -> list[Tin]:
    data = tomllib.loads(path.read_text())
    tins, seen = [], set()
    for t in data.get("tin", []):
        d = t["dir"]
        if d in seen:
            raise SystemExit(f"{path}: {d} listed twice")
        checks = []
        for c in t.get("checks", ["pixi run test"]):
            if isinstance(c, str):
                checks.append(Check(c, c))
            else:
                checks.append(Check(c.get("name", c["run"].strip()), c["run"]))
        tin = Tin(
            dir=d,
            repo=t.get("repo", f"magmalake/{d}"),
            url=t.get("url", ""),
            ref=t.get("ref", "main"),
            checks=checks,
            needs=list(t.get("needs", [])),
            dev_only=bool(t.get("dev_only", False)),
            timeout=int(t.get("timeout", 45)),
            enabled=bool(t.get("enabled", True)),
            note=t.get("note", ""),
        )
        for n in tin.needs:
            if n not in seen:
                raise SystemExit(f"{path}: {d} needs {n}, which is not listed before it")
        seen.add(d)
        tins.append(tin)
    return tins


# ── running ─────────────────────────────────────────────────────────────────


def git(*args, cwd=None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def clone(tin: Tin, work: pathlib.Path, log) -> str:
    dest = work / tin.dir
    if not (dest / ".git").exists():
        url = tin.url or f"https://github.com/{tin.repo}"
        log(f"clone {url} ({tin.ref}) -> {dest}")
        subprocess.run(
            ["git", "clone", "--quiet", "--depth", "1", "--branch", tin.ref, url, str(dest)],
            check=True,
        )
    else:
        log(f"reuse {dest} as it is")
    return git("rev-parse", "HEAD", cwd=dest)


def manifests(root: pathlib.Path, name: str):
    for p in sorted(root.rglob(name)):
        rel = p.relative_to(root).parts
        if any(part in (".pixi", ".git", "build", "target", "node_modules") for part in rel):
            continue
        yield p


def committed(f: pathlib.Path, root: pathlib.Path) -> str:
    """The committed text of `f`, so a reused checkout is rewritten afresh."""
    rel = f.relative_to(root).as_posix()
    p = subprocess.run(
        ["git", "show", f"HEAD:{rel}"], cwd=root, capture_output=True, text=True
    )
    return p.stdout if p.returncode == 0 else f.read_text()


def rewrite_checkout(
    tin: Tin,
    work: pathlib.Path,
    pins: dict[str, str],
    siblings: dict[str, pathlib.Path],
    heads: dict[str, str],
) -> tuple[list[str], set[str], dict[str, set[str]]]:
    root = work / tin.dir
    changes, used, old = [], set(), {}
    for f in manifests(root, "pixi.toml"):
        rw = rewrite_pixi_toml(committed(f, root), pins, siblings, f.parent)
        if rw.text != f.read_text():
            f.write_text(rw.text)
        changes += [f"{f.relative_to(work)}:{c}" for c in rw.changes]
        used |= rw.siblings
        for k, v in rw.old_pins.items():
            old.setdefault(k, set()).update(v)
    for f in manifests(root, "recipe.yaml"):
        text, ch, o = rewrite_recipe(committed(f, root), pins, heads)
        if text != f.read_text():
            f.write_text(text)
        changes += [f"{f.relative_to(work)}:{c}" for c in ch]
        for k, v in o.items():
            old.setdefault(k, set()).update(v)
    used.discard(tin.dir)
    return changes, used, old


def patch_sdk(root: pathlib.Path, sdk: str) -> int:
    n = 0
    body = f"export SDKROOT={sdk}\nexport CONDA_BUILD_SYSROOT={sdk}\n"
    for f in root.glob(".pixi/**/activate_aaaa_sdkroot_env_osx-arm64.sh"):
        if f.read_text() != body:
            f.write_text(body)
            n += 1
    return n


def run_step(cmd: str, cwd: pathlib.Path, env: dict, timeout_s: int, log_path: pathlib.Path):
    """Run one check; returns (exit code or None on timeout, its output)."""
    with open(log_path, "ab") as logf:
        start = logf.tell()
        p = subprocess.Popen(
            ["bash", "-e", "-o", "pipefail", "-c", cmd],
            cwd=cwd,
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            code = p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
            code = None
    with open(log_path, "rb") as logf:
        logf.seek(start)
        out = logf.read().decode("utf-8", "replace")
    return code, out


def first_errors(output: str, n: int = 6) -> list[str]:
    lines = [ANSI.sub("", ln).rstrip() for ln in output.splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    hits = [ln.strip() for ln in lines if ERROR_LINE.search(ln) and not NOT_ERROR.match(ln)]
    if CRASH.search(output):
        what = "compiler crash"
        m = CRASH_ARGS.search(ANSI.sub("", output))
        if m:
            args = m.group("args").split()
            what += ": mojo " + " ".join(a for a in args[1:] if not a.startswith("/"))[:160]
        if FLAKY.search(output):
            what += " (the flaky isContiguousAccess crash)"
        hits.insert(0, what)
    return (hits or lines[-n:])[:n]


@dataclasses.dataclass
class Result:
    tin: str
    sha: str = ""
    # pass | flaky-retried | fail | skipped (a need failed) | disabled | not-run
    result: str = "pending"
    seconds: float = 0.0
    failed_check: str = ""
    errors: list[str] = dataclasses.field(default_factory=list)
    retries: list[str] = dataclasses.field(default_factory=list)
    blocked_by: list[str] = dataclasses.field(default_factory=list)
    log: str = ""
    note: str = ""


def run_tin(tin, work, out, env, args, log) -> Result:
    res = Result(tin.dir)
    root = work / tin.dir
    log_path = out / "logs" / f"{tin.dir}.log"
    log_path.write_text("")
    res.log = str(log_path.relative_to(out))
    t0 = time.monotonic()
    for check in tin.checks:
        with open(log_path, "a") as f:
            f.write(f"\n=== {check.name}\n$ {check.run.strip()}\n\n")
        attempts, sdk_fixes = 0, 0
        while True:
            attempts += 1
            code, output = run_step(check.run, root, env, tin.timeout * 60, log_path)
            if code == 0:
                res.retries += [
                    f"{check.name}: {m}" for m in re.findall(r"^canary: flaky-retry: (.*)$", output, re.M)
                ]
                break
            if args.macos_sdk and SDK_BREAK.search(output) and sdk_fixes < 10:
                patched = patch_sdk(root, args.macos_sdk)
                if patched:
                    sdk_fixes += 1
                    log(f"  {tin.dir}: macOS SDK workaround on {patched} build prefix(es), again")
                    with open(log_path, "a") as f:
                        f.write(f"\n=== {check.name} (again, after the macOS SDK workaround)\n")
                    attempts -= 1
                    continue
            if code is not None and FLAKY.search(output) and attempts == 1:
                res.retries.append(check.name)
                log(f"  {tin.dir}: '{check.name}' hit the known flaky compiler crash, once more")
                with open(log_path, "a") as f:
                    f.write(f"\n=== {check.name} (retry: known flaky compiler crash)\n")
                continue
            break
        if code != 0:
            res.result = "fail"
            res.failed_check = check.name
            res.errors = (
                [f"timed out after {tin.timeout} min"] if code is None else first_errors(output)
            )
            break
    else:
        res.result = "flaky-retried" if res.retries else "pass"
    res.seconds = time.monotonic() - t0
    return res


def fmt_duration(s: float) -> str:
    s = int(round(s))
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def summary_md(meta: dict, results: list[Result]) -> str:
    pinned = ", ".join(meta["pinned"]) or "unknown"
    same = meta["target"] in meta["pinned"] and len(meta["pinned"]) == 1
    head = (
        f"**Mojo nightly canary: `mojo-compiler {meta['target']}`"
        f"{' (newest on the channel)' if meta['resolved'] else ''} "
        f"against the tins' pin `{pinned}`"
        f"{' (the same version)' if same else ''}.**"
    )
    counts = {}
    for r in results:
        counts[r.result] = counts.get(r.result, 0) + 1
    lines = [
        head,
        "",
        f"{meta['platform']}, MAX `{meta['max'] or 'n/a'}`, started {meta['started']}. "
        + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())),
        "",
        "| tin | result | duration | details |",
        "|---|---|---|---|",
    ]
    icon = {"pass": "pass", "flaky-retried": "pass (flaky, retried)", "fail": "**FAIL**",
            "skipped": "skipped", "disabled": "disabled"}
    for r in results:
        if r.result == "fail":
            errs = "<br>".join(_cell(e) for e in r.errors)
            detail = f"`{_cell(r.failed_check)}`: {errs}"
        elif r.result == "skipped":
            detail = "blocked by " + ", ".join(r.blocked_by)
        elif r.result == "flaky-retried":
            detail = "retried: " + ", ".join(f"`{_cell(c)}`" for c in r.retries)
        else:
            detail = _cell(r.note)
        sha = f" `{r.sha[:7]}`" if r.sha else ""
        dur = fmt_duration(r.seconds) if r.seconds else ""
        lines.append(f"| {r.tin}{sha} | {icon.get(r.result, r.result)} | {dur} | {detail} |")
    lines += ["", "Logs and canary-results.json are in the run's `canary` artifact."]
    return "\n".join(lines) + "\n"


def _cell(s: str) -> str:
    s = s.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= 220 else s[:217] + "..."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", help="mojo-compiler version (default: newest nightly)")
    ap.add_argument("--max-version", help="MAX version to pair with it (default: from the channel)")
    ap.add_argument("--channel", default=DEFAULT_CHANNEL)
    ap.add_argument("--tins", type=pathlib.Path, default=DEFAULT_TINS)
    ap.add_argument("--work", type=pathlib.Path, default=pathlib.Path("canary-work"))
    ap.add_argument("--out", type=pathlib.Path, help="default: <work>/canary-out")
    ap.add_argument("--only", help="comma-separated tins to run (the rest are still cloned and rewritten)")
    ap.add_argument("--fresh", action="store_true", help="delete the work dir first")
    ap.add_argument("--rewrite-only", action="store_true", help="clone and rewrite, run nothing")
    ap.add_argument("--resolve", action="store_true", help="print the target version and exit")
    ap.add_argument("--macos-sdk", help="SDK path for the macOS C-shim workaround (see above)")
    args = ap.parse_args(argv)

    def log(msg):
        print(msg, flush=True)

    subdir = conda_subdir()
    repodata = None
    if not (args.version and args.max_version):
        repodata = fetch_repodata(args.channel, subdir)
    target = args.version or newest_mojo(repodata)
    max_version = args.max_version or max_for(repodata, target)
    if args.resolve:
        print(target)
        return 0
    if not max_version:
        log(f"warning: no max-core built with mojo-compiler {target}; MAX pins are left alone")
    pins = target_pins(target, max_version)

    tins = load_tins(args.tins)
    work = args.work.resolve()
    if args.fresh and work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True, exist_ok=True)
    out = (args.out or work / "canary-out").resolve()
    (out / "logs").mkdir(parents=True, exist_ok=True)
    only = set(args.only.split(",")) if args.only else None
    if only and not only <= {t.dir for t in tins}:
        raise SystemExit(f"--only: unknown tins {sorted(only - {t.dir for t in tins})}")

    enabled = [t for t in tins if t.enabled]
    log(f"target: mojo-compiler {target}, MAX {max_version or 'n/a'} ({args.channel}, {subdir})")

    results = {t.dir: Result(t.dir, result="disabled", note=t.note) for t in tins}
    shas = {}
    for t in enabled:
        shas[t.dir] = clone(t, work, log)
        results[t.dir].sha = shas[t.dir]
    siblings = {t.repo.lower(): work / t.dir for t in enabled}
    heads = {t.repo.lower(): shas[t.dir] for t in enabled}

    old_pins: dict[str, set[str]] = {}
    needs: dict[str, set[str]] = {}
    dev_only = {t.dir for t in tins if t.dev_only}
    for t in enabled:
        changes, used, old = rewrite_checkout(t, work, pins, siblings, heads)
        (out / "logs" / f"{t.dir}.rewrite.txt").write_text("\n".join(changes) + "\n")
        log(f"rewrote {t.dir}: {len(changes)} change(s)")
        needs[t.dir] = set(t.needs) | used
        order = [x.dir for x in enabled]
        for n in sorted(used - dev_only):
            if order.index(n) > order.index(t.dir):
                log(f"warning: {t.dir} depends on {n}, which runs after it")
        for k, v in old.items():
            old_pins.setdefault(k, set()).update(v)
    pinned = sorted(old_pins.get("mojo-compiler", set()) | old_pins.get("mojo", set()))

    meta = {
        "target": target,
        "max": max_version,
        "resolved": not args.version,
        "pinned": pinned,
        "channel": args.channel,
        "platform": subdir,
        "started": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "needs": {k: sorted(v) for k, v in needs.items()},
    }
    if args.rewrite_only:
        print(json.dumps(meta, indent=2))
        return 0

    env = dict(os.environ)
    env["CANARY_WORK"] = str(work)
    env["CANARY_TOOLS"] = str(HERE)
    env["CANARY_CHANNEL"] = args.channel
    env["CANARY_MOJO_VERSION"] = target
    env.setdefault("PIXI_COLOR", "never")
    env.setdefault("NO_COLOR", "1")

    for t in enabled:
        r = results[t.dir]
        if only is not None and t.dir not in only:
            r.result, r.note = "not-run", "not selected (--only)"
            continue
        blockers = sorted(
            n for n in needs[t.dir]
            if n not in dev_only
            and results.get(n) is not None
            and results[n].result in ("fail", "skipped")
        )
        if blockers:
            r.result, r.blocked_by = "skipped", blockers
            log(f"skip {t.dir}: blocked by {', '.join(blockers)}")
            continue
        log(f"run {t.dir} ({len(t.checks)} check(s))")
        res = run_tin(t, work, out, env, args, log)
        res.sha = r.sha
        results[t.dir] = res
        log(f"  {t.dir}: {res.result} in {fmt_duration(res.seconds)}"
            + (f" ({res.failed_check})" if res.failed_check else ""))

    ordered = [results[t.dir] for t in tins]
    md = summary_md(meta, ordered)
    (out / "canary-summary.md").write_text(md)
    (out / "canary-results.json").write_text(
        json.dumps({**meta, "results": [dataclasses.asdict(r) for r in ordered]}, indent=2) + "\n"
    )
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(md)
    print()
    print(md)
    bad = [r for r in ordered if r.result == "fail" or (r.result == "skipped" and r.blocked_by)]
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

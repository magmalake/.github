"""Tests for tools/canary.py: run `python3 -m unittest discover tests`.

The manifests under fixtures/canary/ are real files from the tins' main
branches on 2026-11-20: iceberg.mojo (sibling git dependencies, a
`subdirectory`, comments everywhere), parquet.mojo/full, json_mlake (MAX pins
in run-constraints and an extra), encoder.mojo (max-core), mozz_mlake (a
non-sibling git dependency), maws (still on 1.2.0, with a range pin), and the
three rattler-build recipes.
"""

import contextlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import tomllib
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

import canary  # noqa: E402

FIX = HERE / "fixtures" / "canary"
OLD = "1.3.0.dev2026111805"
NEW = "1.3.0.dev2026111905"
OLD_MAX = "26.8.0.dev2026111805"
NEW_MAX = "26.8.0.dev2026111905"
PINS = canary.target_pins(NEW, NEW_MAX)
WORK = pathlib.Path("/w")
TINS = [
    "arrow-mlake.mojo", "avro.mojo", "bench.mojo", "brotli.mojo",
    "deflate.mojo", "flare_mlake", "hashes.mojo", "json_mlake", "lint.mojo",
    "lz4.mojo", "memory-region.mojo", "mozz_mlake", "objectstore.mojo",
    "parquet.mojo", "postgres.mojo", "roaring.mojo", "snappy.mojo",
    "sqlite.mojo", "threads.mojo", "thrift.mojo", "zstd.mojo",
]
SIBLINGS = {f"magmalake/{t}".lower(): WORK / t for t in TINS}


def fixture(name: str) -> str:
    return (FIX / name).read_text()


def rewrite(name: str, tin: str, pins=PINS, sub: str = ""):
    return canary.rewrite_pixi_toml(fixture(name), pins, SIBLINGS, WORK / tin / sub)


def changed_lines(before: str, after: str):
    b, a = before.splitlines(), after.splitlines()
    assert len(b) == len(a), "the rewrite must not add or drop lines"
    return [(x, y) for x, y in zip(b, a) if x != y]


def all_deps(doc):
    """Every (table path, name, spec) in every dependency table."""
    out = []

    def walk(d, path):
        for k, v in d.items():
            if isinstance(v, dict):
                if k.endswith("dependencies") or k.endswith("constraints"):
                    for name, spec in v.items():
                        out.append((path + (k,), name, spec))
                walk(v, path + (k,))

    walk(doc, ())
    return out


class PixiToml(unittest.TestCase):
    def test_iceberg_every_sibling_becomes_a_path(self):
        before = fixture("iceberg.pixi.toml")
        rw = rewrite("iceberg.pixi.toml", "iceberg.mojo")
        doc = tomllib.loads(rw.text)
        for path, name, spec in all_deps(doc):
            if isinstance(spec, dict) and "git" in spec:
                self.fail(f"{path} {name} still a git dependency: {spec}")
        hc = doc["package"]["host-dependencies"]
        self.assertEqual(hc["parquet-full-mojo"], {"path": "../parquet.mojo/full"})
        self.assertEqual(hc["threads-mojo"], {"path": "../threads.mojo"})
        self.assertEqual(doc["dependencies"]["lint-mojo"], {"path": "../lint.mojo"})
        self.assertEqual(doc["dependencies"]["mojo"], f"=={NEW}")
        self.assertEqual(hc["mojo-compiler"], f"=={NEW}")
        self.assertEqual(doc["package"]["run-dependencies"]["mojo-compiler"], f"=={NEW}")
        self.assertEqual(rw.old_pins, {"mojo": {OLD}, "mojo-compiler": {OLD}})
        self.assertIn("objectstore.mojo", rw.siblings)
        # Everything else is byte-for-byte the same: comments, order, tasks.
        for old, new in changed_lines(before, rw.text):
            self.assertTrue(
                "git = " in old or OLD in old, f"unexpected change: {old!r} -> {new!r}"
            )
        self.assertEqual(len(rw.changes), len(changed_lines(before, rw.text)))

    def test_same_version_only_moves_siblings(self):
        rw = rewrite("iceberg.pixi.toml", "iceberg.mojo", canary.target_pins(OLD, OLD_MAX))
        for old, new in changed_lines(fixture("iceberg.pixi.toml"), rw.text):
            self.assertIn("git = ", old)
            self.assertIn("path = ", new)

    def test_subpackage_paths_are_relative_to_its_manifest(self):
        rw = rewrite("parquet-full.pixi.toml", "parquet.mojo", sub="full")
        doc = tomllib.loads(rw.text)
        host = doc["package"]["host-dependencies"]
        self.assertEqual(host["parquet-mojo"], {"path": ".."})  # untouched
        self.assertEqual(host["snappy-mojo"], {"path": "../../snappy.mojo"})
        self.assertEqual(host["mojo-compiler"], f"=={NEW}")

    def test_max_pins_follow_the_max_build(self):
        rw = rewrite("json_mlake.pixi.toml", "json_mlake")
        doc = tomllib.loads(rw.text)
        self.assertEqual(doc["package"]["run-constraints"]["max-core"], f"=={NEW_MAX}")
        self.assertEqual(
            doc["package"]["extra-dependencies"]["gpu"]["max-core"], f"=={NEW_MAX}"
        )
        self.assertEqual(doc["feature"]["gpu"]["dependencies"]["max-core"], f"=={NEW_MAX}")
        self.assertEqual(doc["dependencies"]["mojo"], f"=={NEW}")
        self.assertEqual(
            doc["feature"]["fuzz"]["dependencies"]["mozz_mlake"], {"path": "../mozz_mlake"}
        )
        # A package that happens to be called `git` is a conda spec, not a source.
        self.assertEqual(doc["feature"]["dev"]["dependencies"]["git"], ">=2")
        # mozz is only in the fuzz feature, so it does not block json_mlake.
        self.assertNotIn("mozz_mlake", rw.siblings)

    def test_encoder_max_core(self):
        doc = tomllib.loads(rewrite("encoder.pixi.toml", "encoder.mojo").text)
        self.assertEqual(doc["dependencies"]["max-core"], f"=={NEW_MAX}")
        self.assertEqual(doc["package"]["host-dependencies"]["max-core"], f"=={NEW_MAX}")
        self.assertEqual(doc["package"]["run-dependencies"]["mojo-compiler"], f"=={NEW}")

    def test_non_sibling_git_and_self_path_are_left_alone(self):
        before = fixture("mozz_mlake.pixi.toml")
        rw = rewrite("mozz_mlake.pixi.toml", "mozz_mlake")
        doc = tomllib.loads(rw.text)
        self.assertEqual(doc["dependencies"]["mozz_mlake"], {"path": "."})
        self.assertEqual(
            doc["feature"]["dev"]["dependencies"]["mojodoc"],
            tomllib.loads(before)["feature"]["dev"]["dependencies"]["mojodoc"],
        )

    def test_ranges_are_not_pins(self):
        before = fixture("maws.pixi.toml")
        rw = rewrite("maws.pixi.toml", "maws")
        doc = tomllib.loads(rw.text)
        self.assertEqual(
            doc["feature"]["nightly"]["dependencies"]["modular"], ">=26.8.0.dev2026102305,<28"
        )
        # The exact 1.2.0 pin is exact, so it moves too.
        self.assertEqual(doc["feature"]["stable"]["dependencies"]["mojo-compiler"], f"=={NEW}")
        self.assertEqual(rw.old_pins, {"mojo-compiler": {"1.2.0"}})
        self.assertEqual(len(changed_lines(before, rw.text)), 1)

    def test_inline_table_pin_and_trailing_comment(self):
        text = (
            "[dependencies]\n"
            f'mojo = {{ version = "=={OLD}", channel = "https://conda.modular.com/max-nightly" }}  # keep\n'
            f'threads-mojo = {{ git = "https://github.com/magmalake/threads.mojo.git", branch = "main" }} # x\n'
            f'mojo-compiler = "{OLD}"\n'
            'other = "==1.0"\n'
        )
        rw = canary.rewrite_pixi_toml(text, PINS, SIBLINGS, WORK / "x")
        lines = rw.text.splitlines()
        self.assertTrue(lines[1].endswith("}  # keep"))
        self.assertIn(f'version = "=={NEW}"', lines[1])
        self.assertEqual(lines[2], 'threads-mojo = { path = "../threads.mojo" } # x')
        self.assertEqual(lines[3], f'mojo-compiler = "{NEW}"')
        self.assertEqual(lines[4], 'other = "==1.0"')

    def test_tasks_and_other_tables_are_not_dependencies(self):
        text = '[tasks]\nmojo = "==1.0"\n[feature.x.tasks]\nmojo-compiler = "1.0"\n'
        self.assertEqual(canary.rewrite_pixi_toml(text, PINS, SIBLINGS, WORK).text, text)

    def test_what_it_cannot_rewrite_is_an_error(self):
        for text in [
            # A dependency written as its own table.
            '[dependencies.threads-mojo]\ngit = "https://github.com/magmalake/threads.mojo"\nrev = "abc"\n',
            # A pin written as its own table.
            f'[dependencies]\nmojo = {{ version = "=={OLD}" }}\n[package.host-dependencies.mojo-compiler]\nversion = "=={OLD}"\n',
        ]:
            with self.subTest(text), self.assertRaises(ValueError):
                canary.rewrite_pixi_toml(text, PINS, SIBLINGS, WORK)

    def test_rewrite_is_idempotent(self):
        once = rewrite("iceberg.pixi.toml", "iceberg.mojo").text
        twice = canary.rewrite_pixi_toml(once, PINS, SIBLINGS, WORK / "iceberg.mojo")
        self.assertEqual(once, twice.text)
        self.assertEqual(twice.changes, [])


HEADS = {"magmalake/json_mlake": "1" * 40, "magmalake/threads.mojo": "2" * 40}


class Recipe(unittest.TestCase):
    def test_flare(self):
        before = fixture("flare_mlake.recipe.yaml")
        text, changes, old = canary.rewrite_recipe(before, PINS, HEADS)
        self.assertIn(f"    rev: {'1' * 40}\n    target_directory: _deps/json_mlake", text)
        self.assertIn(f"    rev: {'2' * 40}\n    target_directory: _deps/threads", text)
        self.assertEqual(text.count(f"- mojo-compiler =={NEW}"), 2)
        self.assertIn("- threads-mojo >=0.6.1,<0.7", text)  # a range stays
        self.assertNotIn(OLD, text)
        self.assertEqual(old, {"mojo-compiler": {OLD}})
        self.assertEqual(len(changes), 4)
        self.assertEqual(len(changed_lines(before, text)), 4)

    def test_json_max_core_and_mozz_mojo(self):
        text, _, _ = canary.rewrite_recipe(fixture("json_mlake.recipe.yaml"), PINS, HEADS)
        self.assertIn(f"- max-core =={NEW_MAX}", text)
        self.assertIn(f"- mojo-compiler =={NEW}", text)
        text, _, old = canary.rewrite_recipe(fixture("mozz_mlake.recipe.yaml"), PINS, HEADS)
        self.assertEqual(text.count(f"- mojo =={NEW}"), 2)
        self.assertEqual(old, {"mojo": {OLD}})

    def test_unknown_git_source_keeps_its_rev(self):
        text = "source:\n  - git: https://github.com/someone/else\n    rev: abc\n"
        self.assertEqual(canary.rewrite_recipe(text, PINS, HEADS)[0], text)


def repodata(*recs):
    return {"packages.conda": {f"{i}.conda": r for i, r in enumerate(recs)}}


class Channel(unittest.TestCase):
    def test_newest_by_upload_time_not_version(self):
        data = repodata(
            {"name": "mojo-compiler", "version": "25.6.0.dev2025090305", "timestamp": 1},
            {"name": "mojo", "version": "25.6.0.dev2025090305", "timestamp": 1},
            {"name": "mojo-compiler", "version": NEW, "timestamp": 3},
            {"name": "mojo", "version": NEW, "timestamp": 3},
            {"name": "mojo-compiler", "version": OLD, "timestamp": 2},
            {"name": "mojo", "version": OLD, "timestamp": 2},
            # Uploaded last, but its `mojo` is not out yet.
            {"name": "mojo-compiler", "version": "1.3.0.dev2026112005", "timestamp": 4},
            {"name": "max-core", "version": NEW_MAX, "depends": [f"mojo-compiler =={NEW}"]},
            {"name": "max-core", "version": OLD_MAX, "depends": [f"mojo-compiler =={OLD}"]},
        )
        self.assertEqual(canary.newest_mojo(data), NEW)
        self.assertEqual(canary.max_for(data, NEW), NEW_MAX)
        self.assertEqual(canary.max_for(data, OLD), OLD_MAX)
        self.assertIsNone(canary.max_for(data, "9.9"))

    def test_repin(self):
        self.assertEqual(canary.repin(f"=={OLD}", NEW), f"=={NEW}")
        self.assertEqual(canary.repin(OLD, NEW), NEW)
        self.assertIsNone(canary.repin(">=1.2.0,<2", NEW))
        self.assertIsNone(canary.repin("1.3.*", NEW))

    def test_repo_key(self):
        for url in [
            "https://github.com/magmalake/lint.mojo.git",
            "https://github.com/magmalake/lint.mojo",
            "git@github.com:magmalake/lint.mojo.git",
            "https://github.com/Magmalake/Lint.mojo/",
        ]:
            self.assertEqual(canary.repo_key(url), "magmalake/lint.mojo")
        self.assertIsNone(canary.repo_key("https://gitlab.com/magmalake/lint.mojo"))


class TinsFile(unittest.TestCase):
    def test_the_real_list_loads(self):
        tins = canary.load_tins(HERE.parent / "tools" / "canary-tins.toml")
        dirs = [t.dir for t in tins]
        self.assertEqual(len(dirs), len(set(dirs)))
        enabled = {t.dir for t in tins if t.enabled}
        for t in ["bench.mojo", "lint.mojo", "parquet.mojo", "iceberg.mojo", "flight.mojo"]:
            self.assertIn(t, enabled)
        iceberg = next(t for t in tins if t.dir == "iceberg.mojo")
        self.assertTrue(any("carrow-scan-lib" in c.run for c in iceberg.checks))

    def test_a_need_must_come_first(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "t.toml"
            p.write_text('[[tin]]\ndir = "a"\nneeds = ["b"]\n[[tin]]\ndir = "b"\n')
            with self.assertRaises(SystemExit):
                canary.load_tins(p)


class Output(unittest.TestCase):
    def test_first_errors(self):
        out = "compiling\n\x1b[31merror:\x1b[0m use of unknown declaration 'x'\nmore\n"
        self.assertEqual(canary.first_errors(out), ["error: use of unknown declaration 'x'"])
        self.assertEqual(canary.first_errors("a\nb\n"), ["a", "b"])

    def test_summary_first_line_names_both_versions(self):
        meta = {"target": NEW, "max": NEW_MAX, "resolved": True, "pinned": [OLD],
                "platform": "linux-64", "started": "now"}
        rs = [canary.Result("a", result="fail", failed_check="pixi run test",
                            errors=["error: x | y"]),
              canary.Result("b", result="skipped", blocked_by=["a"])]
        md = canary.summary_md(meta, rs)
        first = md.splitlines()[0]
        self.assertIn(NEW, first)
        self.assertIn(OLD, first)
        self.assertIn("newest", first)
        self.assertIn("error: x \\| y", md)
        self.assertIn("blocked by a", md)


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


class EndToEnd(unittest.TestCase):
    """Three local repositories: one fails, one depends on it, one is flaky."""

    CHECKS = {
        "a": ("echo 'error: broke'; exit 1", ""),
        "b": ("true", 'needs = ["a"]'),
        "c": ("if [ ! -e once ]; then touch once; echo 'isContiguousAccess crash'; exit 1; fi", ""),
        # A crash that repeats is retried once and then fails.
        "d": ("echo 'Please submit a bug report to https://github.com/modular/modular/issues'; exit 139", ""),
    }

    def test_run(self):
        with tempfile.TemporaryDirectory() as d:
            d = pathlib.Path(d)
            tins = ""
            for name, (check, extra) in self.CHECKS.items():
                repo = d / "src" / name
                repo.mkdir(parents=True)
                (repo / "pixi.toml").write_text(
                    f'[workspace]\nname = "{name}"\n\n[dependencies]\n'
                    f'# the pin\nmojo-compiler = "=={OLD}"\n'
                )
                git("init", "-q", "-b", "main", cwd=repo)
                git("add", ".", cwd=repo)
                git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x", cwd=repo)
                tins += (
                    f'[[tin]]\ndir = "{name}"\nurl = "{repo.as_uri()}"\n'
                    f"checks = [{json.dumps(check)}]\n{extra}\n"
                )
            (d / "tins.toml").write_text(tins)
            argv = ["--version", NEW, "--max-version", NEW_MAX,
                    "--tins", str(d / "tins.toml"), "--work", str(d / "work")]
            env = {k: v for k, v in os.environ.items() if k != "GITHUB_STEP_SUMMARY"}
            with mock.patch.dict(os.environ, env, clear=True), \
                    contextlib.redirect_stdout(io.StringIO()):
                code = canary.main(argv)
            self.assertEqual(code, 1)
            res = json.loads((d / "work" / "canary-out" / "canary-results.json").read_text())
            by = {r["tin"]: r for r in res["results"]}
            self.assertEqual(by["a"]["result"], "fail")
            self.assertEqual(by["a"]["errors"], ["error: broke"])
            self.assertEqual(by["b"]["result"], "skipped")
            self.assertEqual(by["b"]["blocked_by"], ["a"])
            self.assertEqual(by["c"]["result"], "flaky-retried")
            self.assertEqual(by["d"]["result"], "fail")
            self.assertEqual(len(by["d"]["retries"]), 1)
            self.assertEqual(by["d"]["errors"][0], "compiler crash")
            self.assertEqual(res["pinned"], [OLD])
            pixi = (d / "work" / "c" / "pixi.toml").read_text()
            self.assertIn(f'mojo-compiler = "=={NEW}"', pixi)
            self.assertIn("# the pin", pixi)
            md = (d / "work" / "canary-out" / "canary-summary.md").read_text()
            self.assertIn(NEW, md.splitlines()[0])


if __name__ == "__main__":
    unittest.main()

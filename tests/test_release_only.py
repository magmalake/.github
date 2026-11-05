"""Tests for tools/release_only.py: run `python3 -m unittest discover tests`.

The fixtures are real pull-request diffs: three release PRs of the 2026-11-05
cascade (parquet.mojo #60, iceberg.mojo #73, orca.mojo #116), a later release
(iceberg.mojo #74), and a feature PR (iceberg.mojo #46).
"""

import pathlib
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "tools"))

from release_only import classify  # noqa: E402


def fixture(name: str) -> str:
    return (HERE / "fixtures" / name).read_text()


def file_diff(path: str, removed=(), added=(), header: str = "") -> str:
    body = "".join(f"-{x}\n" for x in removed) + "".join(f"+{x}\n" for x in added)
    return (
        f"diff --git a/{path} b/{path}\n{header}index 1111111..2222222 100644\n"
        f"--- a/{path}\n+++ b/{path}\n@@ -1,3 +1,3 @@\n{body}"
    )


REV_A = "a" * 40
REV_B = "b" * 40
DEP = 'dista-mojo = { git = "https://github.com/magmalake/dista.mojo", rev = "%s" }'


class RealPullRequests(unittest.TestCase):
    def test_release_prs_are_release_only(self):
        for name in [
            "parquet-60-release.diff",
            "iceberg-73-release.diff",
            "iceberg-74-release.diff",
            "orca-116-release.diff",
        ]:
            with self.subTest(name):
                ok, why = classify(fixture(name))
                self.assertTrue(ok, why)

    def test_feature_pr_is_not(self):
        ok, why = classify(fixture("iceberg-46-feature.diff"))
        self.assertFalse(ok)
        self.assertIn("shm_consume.mojo", why)

    def test_release_plus_one_source_edit_is_not(self):
        diff = fixture("parquet-60-release.diff") + file_diff(
            "src/parquet/writer.mojo", ["var a = 1"], ["var a = 2"]
        )
        self.assertFalse(classify(diff)[0])


class Manifests(unittest.TestCase):
    def test_version_bump(self):
        d = file_diff("shelf.toml", ['version = "1.0.0"'], ['version = "1.0.1"'])
        self.assertTrue(classify(d)[0])

    def test_rev_repin_only(self):
        d = file_diff("pixi.toml", [DEP % REV_A], [DEP % REV_B])
        self.assertTrue(classify(d)[0])

    def test_rev_repin_with_other_change_on_the_line(self):
        d = file_diff(
            "pixi.toml",
            [DEP % REV_A],
            [(DEP % REV_B).replace("dista.mojo", "dista2.mojo")],
        )
        self.assertFalse(classify(d)[0])

    def test_dependency_added_or_removed(self):
        self.assertFalse(classify(file_diff("pixi.toml", [], [DEP % REV_A]))[0])
        self.assertFalse(classify(file_diff("pixi.toml", [DEP % REV_A], []))[0])

    def test_unpinned_dependency_pinned(self):
        bare = 'dista-mojo = { git = "https://github.com/magmalake/dista.mojo" }'
        self.assertFalse(classify(file_diff("pixi.toml", [bare], [DEP % REV_A]))[0])

    def test_indented_version_is_a_dependency(self):
        d = file_diff("pixi.toml", ['  version = "1"'], ['  version = "2"'])
        self.assertFalse(classify(d)[0])

    def test_new_task_is_not(self):
        d = file_diff("pixi.toml", [], ['test2 = "mojo run t.mojo"'])
        self.assertFalse(classify(d)[0])

    def test_subdirectory_manifest(self):
        d = file_diff("full/shelf.toml", ['version = "0.1.8"'], ['version = "0.1.9"'])
        self.assertTrue(classify(d)[0])

    def test_recipe_version(self):
        d = file_diff("recipe.yaml", ['  version: "1.0.0"'], ['  version: "1.0.1"'])
        self.assertTrue(classify(d)[0])
        d = file_diff("recipe.yaml", ['  sha256: aa'], ['  sha256: bb'])
        self.assertFalse(classify(d)[0])


class Workflows(unittest.TestCase):
    CHECKOUT = "        with: {repository: magmalake/x.mojo, ref: %s, path: x.mojo}"

    def test_commit_replaced(self):
        d = file_diff(
            ".github/workflows/ci.yml", [self.CHECKOUT % REV_A], [self.CHECKOUT % REV_B]
        )
        self.assertTrue(classify(d)[0])

    def test_comment_only(self):
        d = file_diff(".github/workflows/ci.yml", ["  # 0.8.7"], ["  # 0.9.0"])
        self.assertTrue(classify(d)[0])

    def test_step_added(self):
        d = file_diff(".github/workflows/ci.yml", [], ["      - run: make"])
        self.assertFalse(classify(d)[0])

    def test_commit_replaced_with_other_edit(self):
        d = file_diff(
            ".github/workflows/ci.yml",
            [self.CHECKOUT % REV_A],
            [(self.CHECKOUT % REV_B).replace("path: x", "path: y")],
        )
        self.assertFalse(classify(d)[0])

    def test_other_yaml_is_not_workflow(self):
        d = file_diff("config/ci.yml", [self.CHECKOUT % REV_A], [self.CHECKOUT % REV_B])
        self.assertFalse(classify(d)[0])


class OtherFiles(unittest.TestCase):
    def test_changelog_and_lock(self):
        d = file_diff("CHANGELOG.md", ["a"], ["b"]) + file_diff(
            "sub/pixi.lock", ["a"], ["b"]
        )
        self.assertTrue(classify(d)[0])

    def test_readme_is_not_release_file(self):
        self.assertFalse(classify(file_diff("README.md", ["a"], ["b"]))[0])

    def test_new_file(self):
        d = file_diff("CHANGELOG.md", [], ["x"], header="new file mode 100644\n")
        self.assertFalse(classify(d)[0])

    def test_rename_of_a_lock_into_source(self):
        d = (
            "diff --git a/pixi.lock b/src/x.mojo\nsimilarity index 100%\n"
            "rename from pixi.lock\nrename to src/x.mojo\n"
        )
        self.assertFalse(classify(d)[0])

    def test_mode_change(self):
        d = "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n"
        self.assertFalse(classify(d)[0])

    def test_binary(self):
        d = "diff --git a/x.png b/x.png\nindex 1..2 100644\nBinary files a/x.png and b/x.png differ\n"
        self.assertFalse(classify(d)[0])

    def test_empty(self):
        self.assertFalse(classify("")[0])


if __name__ == "__main__":
    unittest.main()

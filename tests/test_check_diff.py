import os
import shutil
import subprocess
import tempfile
import unittest

from agent_vault.core.storage import VaultStorage


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class CheckDiffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(os.path.realpath(self.tmp.name), "repo")
        os.makedirs(os.path.join(self.repo, "src"))
        self.file = os.path.join(self.repo, "src", "app.py")
        self.write(self.file, "x = 1\n")
        self.git("init", "-q")
        self.git("add", ".")
        # Don't let the developer's global git config (signing, hooks) affect the test commit.
        no_hooks = os.path.join(self.tmp.name, "no-hooks")
        self.git("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false",
                 "-c", f"core.hooksPath={no_hooks}", "commit", "-q", "-m", "init")
        self.storages = []

    def tearDown(self):
        for s in self.storages:
            s.close()
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def write(self, path, text):
        with open(path, "w") as f:
            f.write(text)

    def open_storage(self, project_root=None):
        s = VaultStorage(os.path.join(os.path.realpath(self.tmp.name), "vault.db"), project_root=project_root)
        self.storages.append(s)
        return s

    def cache(self, s, path, summary):
        s.cache_file(s.normalize_path(path), summary)

    def test_clean_repo_has_no_entries(self):
        self.assertEqual(self.open_storage().diff_entries(self.repo), [])

    def test_summary_cached_before_the_edit_matches_head(self):
        s = self.open_storage()  # global mode: absolute cache keys
        self.cache(s, self.file, "before")
        self.write(self.file, "x = 2\n")
        self.assertEqual(s.diff_entries(self.repo), [
            {"path": "src/app.py", "untracked": False, "summary": "before", "state": "head"},
        ])

    def test_summary_recached_after_the_edit_matches_the_working_file(self):
        s = self.open_storage()
        self.write(self.file, "x = 2\n")
        self.cache(s, self.file, "after")
        self.assertEqual(s.diff_entries(self.repo)[0]["state"], "working")

    def test_summary_from_an_older_version(self):
        s = self.open_storage()
        self.write(self.file, "x = 0\n")
        self.cache(s, self.file, "old")
        self.write(self.file, "x = 2\n")
        self.assertEqual(s.diff_entries(self.repo)[0]["state"], "older")

    def test_untracked_and_uncached_files_are_listed(self):
        s = self.open_storage()
        self.write(os.path.join(self.repo, "new.py"), "y = 1\n")
        self.assertEqual(s.diff_entries(self.repo), [
            {"path": "new.py", "untracked": True, "summary": None, "state": None},
        ])

    def test_portable_mode_and_running_from_a_subdirectory(self):
        s = self.open_storage(project_root=self.repo)  # relative cache keys
        self.cache(s, self.file, "before")
        self.write(self.file, "x = 2\n")
        entries = s.diff_entries(os.path.join(self.repo, "src"))
        self.assertEqual([(e["path"], e["state"]) for e in entries], [("src/app.py", "head")])


if __name__ == "__main__":
    unittest.main()

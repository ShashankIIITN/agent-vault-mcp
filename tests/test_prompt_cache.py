import os
import shutil
import tempfile
import unittest

from agent_vault.core.storage import VaultStorage


class PromptCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.dep = os.path.join(self.root, "dep.py")
        with open(self.dep, "w") as f:
            f.write("print('hello')\n")
        self.storages = []

    def tearDown(self):
        for s in self.storages:
            s.close()
        self.tmp.cleanup()

    def open_storage(self, db_path=None, project_root=None):
        s = VaultStorage(db_path or os.path.join(self.root, "vault.db"), project_root=project_root)
        self.storages.append(s)
        return s

    def test_hit_then_invalidated_when_a_dependency_changes(self):
        s = self.open_storage()
        self.assertEqual(s.cache_answer("How does it work?", "It prints hello.", [self.dep]), [])
        self.assertEqual(s.search_answer("How does it work?")["response"], "It prints hello.")
        with open(self.dep, "a") as f:
            f.write("print('world')\n")
        self.assertIsNone(s.search_answer("How does it work?"))

    def test_missing_dependency_is_reported(self):
        s = self.open_storage()
        missing = os.path.join(self.root, "typo.py")
        self.assertEqual(s.cache_answer("q", "a", [self.dep, missing]), [missing])

    def test_external_dependencies_are_handed_back_for_checking(self):
        s = self.open_storage()
        ignored = s.cache_answer("q", "a", [
            self.dep,
            {"notion://page/1": "2026-10-01T00:00:00Z"},
            {"uri": "github://org/repo/issues/2", "version_hash": "etag-abc"},
        ])
        self.assertEqual(ignored, [])
        self.assertEqual(s.search_answer("q")["unvalidated_dependencies"], {
            "notion://page/1": "2026-10-01T00:00:00Z",
            "github://org/repo/issues/2": "etag-abc",
        })

    def test_external_uri_without_a_version_is_reported(self):
        s = self.open_storage()
        self.assertEqual(s.cache_answer("q", "a", ["notion://page/1", {"notion://page/2": ""}]),
                         ["notion://page/1", "notion://page/2"])

    def test_portable_answers_survive_a_checkout_at_another_path(self):
        repo = os.path.join(self.root, "alice", "repo")
        os.makedirs(repo)
        shutil.copy(self.dep, repo)
        db = os.path.join(repo, ".agent_vault.db")
        alice = self.open_storage(db, project_root=repo)
        alice.cache_answer("q", "a", [os.path.join(repo, "dep.py")])
        alice.close()

        clone = os.path.join(self.root, "bob", "repo")
        shutil.copytree(repo, clone)  # what a teammate gets from git
        bob = self.open_storage(os.path.join(clone, ".agent_vault.db"), project_root=clone)
        self.assertEqual(bob.search_answer("q")["response"], "a")

    def test_portable_db_is_complete_without_its_wal_file(self):
        db = os.path.join(self.root, ".agent_vault.db")
        s = self.open_storage(db, project_root=self.root)
        s.cache_answer("q", "a", [self.dep])
        # Copy while the server still has the DB open, as `git add` would.
        committed = os.path.join(self.root, "committed", ".agent_vault.db")
        os.makedirs(os.path.dirname(committed))
        shutil.copy(db, committed)
        copy = self.open_storage(committed, project_root=self.root)
        self.assertEqual(copy.search_answer("q")["response"], "a")

    def test_portable_rows_cached_with_an_absolute_root_still_match(self):
        s = self.open_storage(project_root=self.root)
        s.cache_answer("q", "a", [self.dep])
        s.conn.execute("UPDATE prompt_cache SET context_dir = ?", (self.root,))
        s.conn.commit()
        self.assertEqual(s.search_answer("q")["response"], "a")


if __name__ == "__main__":
    unittest.main()

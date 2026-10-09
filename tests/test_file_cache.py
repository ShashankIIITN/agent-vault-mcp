import os
import tempfile
import threading
import unittest
from unittest import mock

from agent_vault.core.storage import VaultStorage


class FileCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.db_path = os.path.join(self.root, "vault.db")
        self.file = os.path.join(self.root, "module.py")
        with open(self.file, "w") as f:
            f.write("x = 1\n")
        self.case_insensitive = os.path.exists(os.path.join(self.root, "MODULE.PY"))
        self.storages = []

    def tearDown(self):
        for s in self.storages:
            s.close()
        self.tmp.cleanup()

    def open_storage(self, project_root=None):
        s = VaultStorage(self.db_path, project_root=project_root)
        self.storages.append(s)
        return s

    def test_hit_after_cache(self):
        s = self.open_storage()
        self.assertTrue(s.cache_file(self.file, "summary"))
        result = s.check_file(self.file)
        self.assertTrue(result["cached"])
        self.assertEqual(result["summary"], "summary")

    def test_cache_is_visible_to_other_processes_sharing_the_db(self):
        # Each agent session runs its own server process against the same DB.
        already_running = self.open_storage()
        writer = self.open_storage()
        writer.cache_file(self.file, "summary")
        self.assertTrue(already_running.check_file(self.file)["cached"])

    def test_miss_does_not_hash_the_file(self):
        s = self.open_storage()
        with mock.patch("agent_vault.core.storage.get_file_digest") as digest:
            result = s.check_file(self.file)
        self.assertEqual(result, {"cached": False, "reason": "Not in database"})
        digest.assert_not_called()

    def test_modified_file_is_a_miss(self):
        s = self.open_storage()
        s.cache_file(self.file, "summary")
        with open(self.file, "a") as f:
            f.write("y = 2\n")
        self.assertEqual(s.check_file(self.file)["reason"], "Digest mismatch")

    def test_deleted_file_is_a_miss(self):
        s = self.open_storage()
        s.cache_file(self.file, "summary")
        os.remove(self.file)
        self.assertEqual(s.check_file(self.file)["reason"], "File not found or unreadable")

    def test_paths_differing_only_in_case_share_one_key(self):
        if not self.case_insensitive:
            self.skipTest("filesystem is case-sensitive")
        os.mkdir(os.path.join(self.root, "Pkg"))
        on_disk = os.path.join(self.root, "Pkg", "Mod.py")
        with open(on_disk, "w") as f:
            f.write("x = 1\n")
        s = self.open_storage()
        typed = os.path.join(self.root, "pkg", "mod.py")
        self.assertEqual(s.normalize_path(typed), on_disk)
        s.cache_file(s.normalize_path(on_disk), "summary")
        self.assertTrue(s.check_file(s.normalize_path(typed))["cached"])

    def test_project_root_typed_in_other_case_still_gives_relative_keys(self):
        if not self.case_insensitive:
            self.skipTest("filesystem is case-sensitive")
        s = self.open_storage(project_root=self.root.swapcase())
        self.assertEqual(s.normalize_path(self.file), "module.py")

    def test_missing_path_is_left_as_typed(self):
        s = self.open_storage()
        missing = os.path.join(self.root, "Not", "There.py")
        self.assertEqual(s.normalize_path(missing), missing)

    def test_parallel_calls_from_threads(self):
        # The MCP SDK runs sync tools on worker threads, so one session's parallel tool calls share a connection.
        s = self.open_storage()
        s.cache_file(self.file, "summary")
        errors = []

        def worker(i):
            try:
                for _ in range(200):
                    s.check_file(self.file)
                    s.cache_answer(f"q{i}", "a", [self.file], "t")
                    s.search_answer(f"q{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()

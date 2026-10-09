import os
import shutil
import subprocess
import tempfile
import unittest

from agent_vault.core.storage import VaultStorage


@unittest.skipUnless(shutil.which("git"), "git is not installed")
class ProjectScopeTest(unittest.TestCase):
    """Global mode: answers belong to the git repos of their files, not to the folder they were cached from."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = os.path.realpath(self.tmp.name)
        self.workspace = os.path.join(root, "Dev")
        self.api = os.path.join(self.workspace, "api")
        self.web = os.path.join(self.workspace, "web")
        self.notes = os.path.join(root, "notes")  # not a git repo
        for repo in (self.api, self.web):
            os.makedirs(os.path.join(repo, "src"))
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        os.makedirs(self.notes)
        self.auth = self.write(os.path.join(self.api, "src", "auth.py"))
        self.login = self.write(os.path.join(self.web, "src", "login.py"))
        self.old_cwd = os.getcwd()
        self.vault = VaultStorage(os.path.join(root, "vault.db"))

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.vault.close()
        self.tmp.cleanup()

    def write(self, path, text="x = 1\n"):
        with open(path, "w") as f:
            f.write(text)
        return path

    def session(self, folder):
        os.chdir(folder)

    def answer(self, prompt="q", project=None):
        return self.vault.search_answer(prompt, project=project)

    def test_answer_is_filed_under_the_repo_of_its_files(self):
        self.session(self.web)
        self.vault.cache_answer("q", "about api", [self.auth])
        self.assertEqual(self.answer(), {"status": "elsewhere", "projects": [self.api]})
        self.session(self.api)
        self.assertEqual(self.answer()["response"], "about api")

    def test_another_project_is_fetched_by_naming_it(self):
        self.session(self.api)
        self.vault.cache_answer("q", "about api", [self.auth])
        self.session(self.web)
        self.assertEqual(self.answer(project=self.api)["response"], "about api")
        self.assertEqual(self.answer(project="../api")["response"], "about api")  # relative to the session

    def test_parent_folder_session_sees_child_repos(self):
        self.session(self.api)
        self.vault.cache_answer("q", "about api", [self.auth])
        self.session(self.workspace)
        self.assertEqual(self.answer()["status"], "hit")

    def test_subfolder_session_sees_its_repo(self):
        self.session(self.api)
        self.vault.cache_answer("q", "about api", [self.auth])
        self.session(os.path.join(self.api, "src"))
        self.assertEqual(self.answer()["status"], "hit")

    def test_cross_repo_answer_is_local_to_each_repo(self):
        self.session(self.workspace)
        self.vault.cache_answer("q", "login across services", [self.auth, self.login])
        for repo in (self.api, self.web):
            self.session(repo)
            self.assertEqual(self.answer()["projects"], [self.api, self.web])

    def test_same_question_in_two_repos_keeps_both_answers(self):
        self.session(self.api)
        self.vault.cache_answer("How does auth work?", "api's auth", [self.auth])
        self.session(self.web)
        self.vault.cache_answer("How does auth work?", "web's auth", [self.login])

        self.assertEqual(self.answer("How does auth work?")["response"], "web's auth")
        self.session(self.api)
        self.assertEqual(self.answer("How does auth work?")["response"], "api's auth")
        self.session(self.workspace)
        self.assertEqual(self.answer("How does auth work?"),
                         {"status": "choose", "projects": [self.web, self.api]})
        self.assertEqual(self.answer("How does auth work?", project=self.web)["response"],
                         "web's auth")

    def test_recaching_replaces_the_answer_for_the_same_code(self):
        self.session(self.api)
        self.vault.cache_answer("q about auth", "v1", [self.auth], "auth")
        self.vault.cache_answer("q about auth", "v2", [self.auth], "auth")
        self.assertEqual(self.answer("q about auth")["response"], "v2")
        self.assertIn("Found 1 matching", self.vault.search_questions("auth"))

    def test_search_lists_other_projects_separately(self):
        self.session(self.api)
        self.vault.cache_answer("q about auth", "a", [self.auth], "auth")
        self.session(self.web)
        out = self.vault.search_questions("auth")
        self.assertNotIn("From this project:", out)
        self.assertIn("From other projects:", out)
        self.assertIn(f"Project: {self.api}", out)
        self.assertIn('project="<its Project path>"', out)

    def test_evicted_answer_leaves_no_search_entry(self):
        self.session(self.api)
        self.vault.cache_answer("q about auth", "a", [self.auth], "auth")
        self.write(self.auth, "x = 2\n")
        self.assertIsNone(self.answer("q about auth"))
        self.assertEqual(self.vault.search_questions("auth"), "No matching questions found in the cache.")

    def test_startup_repairs_duplicate_and_orphaned_search_rows(self):
        self.session(self.api)
        self.vault.cache_answer("q about auth", "a", [self.auth], "auth")
        # What older versions left behind: a second row for the same question, and one with no answer.
        query_hash = self.vault.conn.execute("SELECT query_hash FROM prompt_cache").fetchone()[0]
        self.vault.conn.execute("INSERT INTO prompt_search (prompt, tags, query_hash) VALUES ('q about auth', 'auth', ?)",
                                (query_hash,))
        self.vault.conn.execute("INSERT INTO prompt_search (prompt, tags, query_hash) VALUES ('gone auth', 'auth', 'x')")
        self.vault.conn.commit()
        self.assertIn("Found 1 matching", self.vault.search_questions("auth"))

        self.vault.close()
        self.vault = VaultStorage(self.vault.db_path)
        self.assertEqual(self.vault.conn.execute("SELECT count(*) FROM prompt_search").fetchone()[0], 1)

    def test_answers_cached_before_projects_existed(self):
        self.session(self.web)
        self.vault.cache_answer("q", "about api", [self.auth])
        # Older versions stored only the launch folder.
        self.vault.conn.execute("UPDATE prompt_cache SET projects = NULL, context_dir = ?", (self.web,))
        self.vault.conn.commit()
        self.session(self.api)
        self.assertEqual(self.answer()["response"], "about api")
        self.assertIsNotNone(self.vault.conn.execute("SELECT projects FROM prompt_cache").fetchone()[0])

    def test_file_outside_any_repo_stands_for_its_folder(self):
        note = self.write(os.path.join(self.notes, "n.md"), "notes\n")
        self.session(self.api)
        self.vault.cache_answer("q", "from notes", [note])
        self.assertEqual(self.answer(), {"status": "elsewhere", "projects": [self.notes]})
        self.session(self.notes)
        self.assertEqual(self.answer()["response"], "from notes")

    def test_answer_with_only_external_dependencies_uses_the_session_folder(self):
        self.session(self.api)
        self.vault.cache_answer("q", "from notion", [{"notion://page/1": "v1"}])
        self.assertEqual(self.answer()["status"], "hit")
        self.session(self.web)
        self.assertEqual(self.answer(), {"status": "elsewhere", "projects": [self.api]})


if __name__ == "__main__":
    unittest.main()

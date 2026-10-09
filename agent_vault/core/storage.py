import sqlite3
import os
import json
import functools
import threading
from .dsa import get_file_digest, TokenBoundedMinHeap

def _locked(method):
    # The MCP SDK runs sync tools on worker threads, so parallel tool calls share
    # self.conn. sqlite3 connections (and their transactions) are not safe to use
    # concurrently, so every public method holds the lock for its whole duration.
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper

def _on_disk_case(path):
    """Return absolute `path` with each component spelled as it is on disk.

    Case-insensitive filesystems (macOS, Windows) accept any casing and realpath
    keeps whatever was typed, so one file could otherwise be cached under several keys.
    Paths that don't exist as typed are returned unchanged.
    """
    if not os.path.exists(path):
        return path
    return _match_case(path)

def _match_case(path):
    head, tail = os.path.split(path)
    if not tail:
        return path
    parent = _match_case(head)
    try:
        names = os.listdir(parent)
    except OSError:
        return os.path.join(parent, tail)
    if tail not in names:
        # The path exists, so this level is case-insensitive and the match is unique.
        folded = tail.casefold()
        tail = next((n for n in names if n.casefold() == folded), tail)
    return os.path.join(parent, tail)

def _related(a, b):
    """True if paths a and b are the same, or one contains the other."""
    try:
        common = os.path.commonpath([a, b])
    except ValueError:  # different drives, or a relative path
        return False
    return common in (a, b)

class VaultStorage:
    def __init__(self, db_path="vault.db", project_root=None):
        self.db_path = db_path
        # Canonicalise the root the same way as file paths, or relpath() between them breaks.
        self.project_root = _on_disk_case(os.path.realpath(project_root)) if project_root else None
        self._lock = threading.RLock()
        self._repo_roots = {}  # directory -> git repo root, see _repo_root
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        if self.project_root:
            # A portable DB is meant to be committed to git, so every write must land in the main file:
            # in WAL mode it sits in a -wal side file until checkpointed, and a committed copy can be empty.
            # Leaving WAL fails while another session has the DB open; a later start retries.
            try:
                self.conn.execute("PRAGMA journal_mode=DELETE;")
            except sqlite3.OperationalError:
                pass
        else:
            self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self._init_db()

    
    def _to_os_path(self, db_path):
        import os
        if self.project_root and not os.path.isabs(db_path):
            return os.path.join(self.project_root, db_path)
        return db_path
        
    def normalize_path(self, filepath):
        """Canonical cache key for a file: relative to project_root if set, else absolute."""
        import os
        abs_path = _on_disk_case(os.path.abspath(os.path.realpath(filepath)))
        if self.project_root:
            try:
                return os.path.relpath(abs_path, start=self.project_root).replace(os.sep, '/')
            except ValueError:
                pass
        return abs_path

    # Cached answers belong to projects: the git repos of the files they depend on. In global mode a session
    # sees an answer as local when one of its projects is the session's folder, inside it, or contains it,
    # so a session opened on a parent folder of several repos sees all of them. A portable DB holds a single
    # project, so there every answer is local.

    def _session_scope(self):
        """The folder this server's session was launched in."""
        return _on_disk_case(os.path.realpath(os.getcwd()))

    def _repo_root(self, directory):
        """Git repo root containing directory; a directory outside any repo stands for itself."""
        import subprocess
        if directory not in self._repo_roots:
            root = directory
            try:
                result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=directory,
                                        capture_output=True, text=True)
                if result.returncode == 0 and result.stdout.strip():
                    root = _on_disk_case(os.path.realpath(result.stdout.strip()))
            except OSError:  # directory gone, or git not installed
                pass
            self._repo_roots[directory] = root
        return self._repo_roots[directory]

    def _projects_for(self, deps_data, context_dir):
        """Projects for an answer: the repos of its local dependency files, else the folder it was cached from."""
        projects = sorted({self._repo_root(os.path.dirname(p)) for p in deps_data if "://" not in p})
        if projects:
            return projects
        return [context_dir] if context_dir and context_dir != "." else []

    def _row_projects(self, row_id, projects_json, deps_json, context_dir):
        """Projects of a prompt_cache row; computed and saved for rows cached before projects existed."""
        if projects_json is not None:
            return json.loads(projects_json)
        projects = self._projects_for(json.loads(deps_json), context_dir)
        with self.conn:
            self.conn.execute("UPDATE prompt_cache SET projects = ? WHERE id = ?", (json.dumps(projects), row_id))
        return projects

    def _in_scope(self, projects, scope):
        # Old rows with neither files nor a folder to place them were visible everywhere; they still are.
        return not projects or any(_related(p, scope) for p in projects)

    def _delete_answer(self, row_id):
        # The caller holds the transaction. Search rows share their answer's id.
        self.conn.execute("DELETE FROM prompt_cache WHERE id = ?", (row_id,))
        self.conn.execute("DELETE FROM prompt_search WHERE rowid = ?", (row_id,))

    def _init_db(self):
        with self.conn:
            # FTS5 table for memory snippets
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS memories USING fts5(
                    key, content, tags, tokens
                )
            ''')
            # Table for file cache
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS file_cache (
                    filepath TEXT PRIMARY KEY,
                    digest TEXT,
                    summary TEXT,
                    tokens_saved_per_hit INTEGER DEFAULT 0
                )
            ''')
            # Handle schema migration for file_cache safely
            cursor = self.conn.execute("PRAGMA table_info(file_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'tokens_saved_per_hit' not in columns:
                self.conn.execute('ALTER TABLE file_cache ADD COLUMN tokens_saved_per_hit INTEGER DEFAULT 0')
            
            # Metrics table
            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    filepath TEXT,
                    is_hit BOOLEAN,
                    tokens_saved INTEGER,
                    reason TEXT
                )
            ''')
            # Handle schema migration for metrics safely
            cursor = self.conn.execute("PRAGMA table_info(metrics)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'reason' not in columns:
                self.conn.execute('ALTER TABLE metrics ADD COLUMN reason TEXT')

            # Prompt cache table
            

            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS prompt_cache (
                    id INTEGER PRIMARY KEY,
                    query_hash TEXT,
                    context_dir TEXT,
                    prompt TEXT,
                    response TEXT,
                    dependency_files TEXT,
                    tags TEXT,
                    tokens_saved_per_hit INTEGER DEFAULT 0
                )
            ''')
            # Handle migration
            cursor = self.conn.execute("PRAGMA table_info(prompt_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'context_dir' not in columns:
                # We need to drop the UNIQUE constraint on query_hash, which requires table recreation in SQLite.
                # Since it's just a cache, we can safely drop and recreate.
                self.conn.execute('DROP TABLE prompt_cache')
                self.conn.execute('DROP TABLE IF EXISTS prompt_search')
                self.conn.execute('''
                    CREATE TABLE prompt_cache (
                        id INTEGER PRIMARY KEY,
                        query_hash TEXT,
                        context_dir TEXT,
                        prompt TEXT,
                        response TEXT,
                        dependency_files TEXT,
                        tags TEXT
                    )
                ''')
                self.conn.execute('''
                    CREATE VIRTUAL TABLE prompt_search USING fts5(
                        prompt, tags, query_hash UNINDEXED
                    )
                ''')
            elif 'prompt' not in columns:
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN prompt TEXT')
            elif 'tags' not in columns:
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN tags TEXT')
            if 'tokens_saved_per_hit' not in columns:
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN tokens_saved_per_hit INTEGER DEFAULT 0')
            if 'projects' not in columns:
                # JSON list of project paths; NULL until computed, see _row_projects
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN projects TEXT')

            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS prompt_search USING fts5(
                    prompt, tags, query_hash UNINDEXED
                )
            ''')
            # Keep exactly one search row per cached answer, sharing its id. Older versions added a row on
            # every re-cache and never removed any, so drop duplicates and orphans and add missing rows.
            # Runs on every start because servers on an older version may still be writing to this DB.
            self.conn.execute('''
                DELETE FROM prompt_search WHERE rowid NOT IN (
                    SELECT s.rowid FROM prompt_search s
                    JOIN prompt_cache c ON c.id = s.rowid AND c.query_hash = s.query_hash
                )
            ''')
            self.conn.execute('''
                INSERT INTO prompt_search (rowid, prompt, tags, query_hash)
                SELECT id, prompt, tags, query_hash FROM prompt_cache
                WHERE id NOT IN (SELECT rowid FROM prompt_search)
            ''')

    @_locked
    def store_memory(self, key, content, tags, tokens=None):
        if tokens is None:
            # simple token estimation (1 token ~= 4 chars)
            tokens = len(content) // 4 + 1
            
        with self.conn:
            # Delete if exists to update
            self.conn.execute("DELETE FROM memories WHERE key = ?", (key,))
            self.conn.execute(
                "INSERT INTO memories (key, content, tags, tokens) VALUES (?, ?, ?, ?)",
                (key, content, tags, tokens)
            )

    @_locked
    def search_memory(self, query, max_tokens=2000):
        # We use BM25 which is built into FTS5 via ORDER BY rank
        # Standard FTS5 match query
        
        # Sanitize query to avoid FTS syntax errors on special chars
        safe_query = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in query).strip()
        if not safe_query:
            return []
            
        
        # Split into words and create an OR query with wildcards for better natural language matching
        words = [w for w in safe_query.split() if len(w) > 2]
        if not words:
            return []
            
        fts_query = ' OR '.join(f'"{w}"*' for w in words)
        try:
            cursor = self.conn.execute('''
                SELECT key, content, tags, tokens, rank 
                FROM memories 
                WHERE memories MATCH ? 
                ORDER BY rank
            ''', (fts_query,))
        except sqlite3.OperationalError:
            # fallback if query is still invalid
            return []
        
        # Rank in FTS5 is more negative for better matches. 
        # Let's negate it so higher is better for our max-heap logic.
        heap = TokenBoundedMinHeap(max_tokens=max_tokens)
        
        for row in cursor:
            key, content, tags, tokens, rank = row
            # FTS5 rank is typically a negative value, where lower (more negative) means better.
            # So a score of -rank means a higher positive value is better.
            score = -rank 
            item = {
                "key": key,
                "content": content,
                "tags": tags,
                "tokens": tokens
            }
            # We assume tokens is an integer
            try:
                tokens_int = int(tokens)
            except (ValueError, TypeError):
                tokens_int = len(content) // 4 + 1
                
            heap.add(item, score, tokens_int)
            
        return heap.get_items()

    @_locked
    def close(self):
        if self.conn:
            self.conn.close()

    @_locked
    def delete_memory(self, key):
        with self.conn:
            self.conn.execute("DELETE FROM memories WHERE key = ?", (key,))

    @_locked
    def evict_file(self, filepath):
        with self.conn:
            self.conn.execute("DELETE FROM file_cache WHERE filepath = ?", (filepath,))

    def _is_ignored(self, filepath):
        import subprocess
        import os
        try:
            os_path = self._to_os_path(filepath)
            cwd = self.project_root or os.path.dirname(os_path) or os.getcwd()
            result = subprocess.run(
                ['git', 'check-ignore', '-q', os_path],
                cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            return result.returncode == 0
        except Exception:
            return False


    @_locked
    def cache_resource(self, uri, version_hash, summary):
        with self.conn:
            self.conn.execute('''
                INSERT INTO file_cache (filepath, digest, summary, tokens_saved_per_hit)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    digest=excluded.digest,
                    summary=excluded.summary,
                    tokens_saved_per_hit=excluded.tokens_saved_per_hit
            ''', (uri, version_hash, summary, 500))
        return True

    @_locked
    def check_resource(self, uri, version_hash):
        cursor = self.conn.execute("SELECT digest, summary, tokens_saved_per_hit FROM file_cache WHERE filepath = ?", (uri,))
        row = cursor.fetchone()
        
        if not row:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (uri, "resource_not_in_db"))
            return {"cached": False, "reason": "not_in_db"}
            
        cached_digest, summary, tokens_saved = row
        if version_hash == cached_digest:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 1, ?, ?)", (uri, tokens_saved, "resource_hit"))
            return {"cached": True, "summary": summary}
        else:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (uri, "resource_mismatch"))
            return {"cached": False, "reason": "version_hash mismatch"}

    @_locked
    def cache_file(self, filepath, summary):
        if self._is_ignored(filepath):
            return False
        os_path = self._to_os_path(filepath)
        digest, raw_file_size = get_file_digest(os_path)
        if not digest:
            return False
            
        raw_tokens = raw_file_size // 4
        summary_tokens = len(summary) // 4
        tokens_saved_per_hit = max(0, raw_tokens - summary_tokens)
            
        with self.conn:
            self.conn.execute('''
                INSERT INTO file_cache (filepath, digest, summary, tokens_saved_per_hit)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(filepath) DO UPDATE SET
                    digest=excluded.digest,
                    summary=excluded.summary,
                    tokens_saved_per_hit=excluded.tokens_saved_per_hit
            ''', (filepath, digest, summary, tokens_saved_per_hit))

        return True

    @_locked
    def check_file(self, filepath):
        # 1. Fast rejection: indexed primary-key lookup, before paying to hash the file.
        # Reading the DB (not per-process state) keeps every server process sharing it consistent.
        cursor = self.conn.execute("SELECT digest, summary, tokens_saved_per_hit FROM file_cache WHERE filepath = ?", (filepath,))
        row = cursor.fetchone()

        if not row:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "not_in_db"))
            return {"cached": False, "reason": "Not in database"}

        # 2. Check digest
        os_path = self._to_os_path(filepath)
        current_digest, _ = get_file_digest(os_path)
        if not current_digest:
             with self.conn:
                 self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "not_found"))
             return {"cached": False, "reason": "File not found or unreadable"}

        cached_digest, summary, tokens_saved_per_hit = row
        if current_digest == cached_digest:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 1, ?, ?)", (filepath, tokens_saved_per_hit, "hit"))
            return {"cached": True, "summary": summary, "tokens_saved": tokens_saved_per_hit}
        else:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "digest_mismatch"))
            return {"cached": False, "reason": "Digest mismatch"}

    @_locked
    def get_file_entry(self, filepath):
        """Return (digest, summary) cached for filepath, or None."""
        cursor = self.conn.execute("SELECT digest, summary FROM file_cache WHERE filepath = ?", (filepath,))
        return cursor.fetchone()

    def diff_entries(self, cwd):
        """Cached summaries for the uncommitted changes (including untracked files) of the git repo at cwd.

        Returns a list of dicts: path (relative to the repo root), untracked, summary (None if not
        cached), and state: which version the summary was cached from -- "head" (before the changes),
        "working" (the current file) or "older". Raises if git fails.
        """
        import subprocess
        import hashlib

        def git(where, *args):
            return os.fsdecode(subprocess.run(['git', *args], cwd=where, capture_output=True, check=True).stdout)

        top = git(cwd, 'rev-parse', '--show-toplevel').strip()
        # Run from the repo root so all paths are relative to it. -z: NUL-separated, never quoted,
        # so non-ASCII file names come through intact.
        changed = [p for p in git(top, 'diff', '--name-only', '-z', 'HEAD').split('\0') if p]
        untracked = [p for p in git(top, 'ls-files', '-z', '--others', '--exclude-standard').split('\0') if p]

        entries = []
        for rel in changed + untracked:
            abs_path = os.path.join(top, rel)
            entry = {"path": rel, "untracked": rel not in changed, "summary": None, "state": None}
            row = self.get_file_entry(self.normalize_path(abs_path))
            if row:
                cached_digest, entry["summary"] = row
                head = subprocess.run(['git', 'show', f'HEAD:{rel}'], cwd=top, capture_output=True)
                head_digest = hashlib.sha256(head.stdout).hexdigest() if head.returncode == 0 else None
                if cached_digest == get_file_digest(abs_path)[0]:
                    entry["state"] = "working"
                elif cached_digest == head_digest:
                    entry["state"] = "head"
                else:
                    entry["state"] = "older"
            entries.append(entry)
        return entries

    @_locked
    def get_metrics_dashboard(self):
        cursor = self.conn.execute("SELECT COUNT(*), SUM(is_hit), SUM(tokens_saved) FROM metrics")
        row = cursor.fetchone()
        
        total_checks = row[0] if row else 0
        total_hits = row[1] if row and row[1] is not None else 0
        total_tokens_saved = row[2] if row and row[2] is not None else 0
        
        hit_ratio = (total_hits / total_checks) * 100 if total_checks > 0 else 0
        time_saved_seconds = total_tokens_saved / 50.0  # Assuming 50 tokens/sec
        
        if time_saved_seconds > 3600:
            time_saved_str = f"{time_saved_seconds / 3600:.2f} hours"
        elif time_saved_seconds > 60:
            time_saved_str = f"{time_saved_seconds / 60:.2f} minutes"
        else:
            time_saved_str = f"{time_saved_seconds:.2f} seconds"
            
        return (
            f"=== Vault ROI Dashboard ===\n"
            f"Total File Checks: {total_checks}\n"
            f"Cache Hits: {total_hits} ({hit_ratio:.1f}% hit ratio)\n"
            f"Total Tokens Saved: {total_tokens_saved:,}\n"
            f"Estimated Time Saved: {time_saved_str}\n"
            f"==========================="
        )

    @_locked
    def cache_answer(self, prompt, response, dependencies, tags=""):
        """Cache response for prompt, invalidated when any dependency changes.

        Each dependency is a file path, or a dict of external resources: {"<uri>": "<version>"}
        or {"uri": ..., "version_hash": ...}. Returns the dependencies that were ignored: paths
        that don't exist, and external URIs without a version.
        """
        import hashlib
        import json
        import os
        from .dsa import get_file_digest

        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()

        deps_data = {}
        ignored = []
        total_dependency_size = 0
        for d in dependencies:
            if isinstance(d, dict):
                pairs = [(d["uri"], d["version_hash"])] if "uri" in d and "version_hash" in d else d.items()
                for uri, version in pairs:
                    # search_answer tells external deps from file paths by the "://"
                    if "://" in uri and version:
                        deps_data[uri] = version
                    else:
                        ignored.append(uri)
                continue
            if "://" in d:
                ignored.append(d)
                continue

            db_path = self.normalize_path(d)
            os_path = self._to_os_path(db_path)
            if os.path.exists(os_path):
                digest, raw_size = get_file_digest(os_path)
                deps_data[db_path] = digest
                total_dependency_size += raw_size
            else:
                ignored.append(d)

        tokens_saved_per_hit = max(0, (total_dependency_size // 4) - (len(response) // 4))
        deps_json = json.dumps(deps_data)

        if self.project_root:
            context_dir, projects = ".", None
        else:
            context_dir = self._session_scope()
            projects = self._projects_for(deps_data, context_dir)

        # Replace earlier answers to this question about the same code. In global mode answers about other
        # projects are kept: the same question can mean different things in different repos.
        rows = self.conn.execute(
            "SELECT id, projects, dependency_files, context_dir FROM prompt_cache WHERE query_hash = ?", (query_hash,)
        ).fetchall()
        replaced = [row[0] for row in rows if self.project_root or set(self._row_projects(*row)) & set(projects)]

        with self.conn:
            for row_id in replaced:
                self._delete_answer(row_id)
            cursor = self.conn.execute('''
                INSERT INTO prompt_cache (query_hash, context_dir, prompt, response, dependency_files, tags, tokens_saved_per_hit, projects)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (query_hash, context_dir, prompt, response, deps_json, tags, tokens_saved_per_hit,
                  None if projects is None else json.dumps(projects)))
            self.conn.execute('''
                INSERT INTO prompt_search (rowid, prompt, tags, query_hash)
                VALUES (?, ?, ?, ?)
            ''', (cursor.lastrowid, prompt, tags, query_hash))
        return ignored
        
    @_locked
    def search_questions(self, query, max_results=5):
        # Sanitize query
        safe_query = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in query).strip()
        if not safe_query:
            return "No valid search terms provided."
        
        # Prepare FTS exact match string format: "word1" "word2"
        fts_query = ' OR '.join(f'"{word}"*' for word in safe_query.split())
            
        try:
            # The join also skips search rows written by older versions that don't match an answer.
            rows = self.conn.execute('''
                SELECT c.id, c.prompt, c.tags, c.projects, c.dependency_files, c.context_dir
                FROM (SELECT rowid, query_hash, rank FROM prompt_search WHERE prompt_search MATCH ?) s
                JOIN prompt_cache c ON c.id = s.rowid AND c.query_hash = s.query_hash
                ORDER BY s.rank
            ''', (fts_query,)).fetchall()
        except Exception as e:
            return f"Search failed: {e}"

        if not rows:
            return "No matching questions found in the cache."

        # Up to max_results from this project, and as many again from other projects.
        if self.project_root:
            sections = [("", [(p, t, None) for _, p, t, _, _, _ in rows[:max_results]])]
        else:
            scope = self._session_scope()
            local, other = [], []
            for row_id, p, t, projects_json, deps_json, context_dir in rows:
                projects = self._row_projects(row_id, projects_json, deps_json, context_dir)
                (local if self._in_scope(projects, scope) else other).append((p, t, projects))
            sections = [("From this project:", local[:max_results]), ("From other projects:", other[:max_results])]

        found = sum(len(items) for _, items in sections)
        out = [f"Found {found} matching cached questions:"]
        i = 0
        for heading, items in sections:
            if not items:
                continue
            if heading:
                out.append(heading)
            for p, t, projects in items:
                i += 1
                out.append(f"--- Option {i} ---")
                out.append(f"Prompt: {p}")
                out.append(f"Tags: {t if t else 'None'}")
                if projects:
                    out.append(f"Project: {', '.join(projects)}")
                out.append("")
        out.append("Use vault_search_answer with the exact Prompt string if one matches your intent.")
        if not self.project_root and other:
            out.append('For a question from another project, also pass project="<its Project path>".')
        return "\n".join(out)


    @_locked
    def search_answer(self, prompt, project=None):
        """Find the cached answer to prompt that applies to this session.

        In global mode that's an answer for one of the session's projects, or for `project` (a path) when
        given. Stale answers are evicted. Returns None if nothing is cached, else a dict whose "status" is:
          "hit": with "response", its "projects" and "unvalidated_dependencies" (external ones to re-check)
          "choose": answers exist for several projects in scope; "projects" lists them, one label each
          "elsewhere": answers exist only for other projects; "projects" lists them
        """
        import hashlib

        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        if self.project_root:
            scope = None
        elif project:
            scope = _on_disk_case(os.path.realpath(os.path.expanduser(project)))
        else:
            scope = self._session_scope()

        rows = self.conn.execute(
            "SELECT id, response, dependency_files, tokens_saved_per_hit, projects, context_dir "
            "FROM prompt_cache WHERE query_hash = ? ORDER BY id DESC", (query_hash,)
        ).fetchall()
        valid, elsewhere = [], set()
        for row_id, response, deps_json, tokens_saved_per_hit, projects_json, context_dir in rows:
            projects = [] if scope is None else self._row_projects(row_id, projects_json, deps_json, context_dir)
            if scope is not None and not self._in_scope(projects, scope):
                elsewhere.update(projects)
                continue
            unvalidated = self._check_dependencies(json.loads(deps_json))
            if unvalidated is None:
                with self.conn:
                    self._delete_answer(row_id)
                continue
            valid.append((response, projects, unvalidated, tokens_saved_per_hit))

        labels = list(dict.fromkeys(", ".join(projects) for _, projects, _, _ in valid))
        if len(labels) > 1 and not project:
            return {"status": "choose", "projects": labels}
        if valid:
            response, projects, unvalidated, tokens_saved_per_hit = valid[0]  # newest
            with self.conn:
                self.conn.execute('''
                    INSERT INTO metrics (filepath, is_hit, tokens_saved, reason)
                    VALUES (?, 1, ?, ?)
                ''', ("semantic_cache", tokens_saved_per_hit or 0, "Semantic cache hit"))
            return {"status": "hit", "response": response, "projects": projects,
                    "unvalidated_dependencies": unvalidated}
        if elsewhere:
            return {"status": "elsewhere", "projects": sorted(elsewhere)}
        return None

    def _check_dependencies(self, deps_data):
        """None if a local dependency changed or is gone; else the external deps for the caller to re-check."""
        from .dsa import get_file_digest

        unvalidated_deps = {}
        if isinstance(deps_data, list):
            # Backwards compatibility: a list of filepaths, checked against the file cache
            for filepath in deps_data:
                current_digest, _ = get_file_digest(self._to_os_path(filepath))
                cached_row = self.conn.execute("SELECT digest FROM file_cache WHERE filepath = ?", (filepath,)).fetchone()
                if not current_digest or not cached_row or cached_row[0] != current_digest:
                    return None
            return unvalidated_deps
        # A dict of {filepath: digest}, or {uri: version} for external dependencies
        for filepath, saved_hash in deps_data.items():
            if "://" in filepath:
                unvalidated_deps[filepath] = saved_hash
                continue
            current_digest, _ = get_file_digest(self._to_os_path(filepath))
            if current_digest != saved_hash:
                return None
        return unvalidated_deps

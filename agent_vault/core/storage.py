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

class VaultStorage:
    def __init__(self, db_path="vault.db", project_root=None):
        self.db_path = db_path
        # Canonicalise the root the same way as file paths, or relpath() between them breaks.
        self.project_root = _on_disk_case(os.path.realpath(project_root)) if project_root else None
        self._lock = threading.RLock()
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

    def _context_dirs(self):
        """Values of prompt_cache.context_dir that belong to this project; the first is used for new rows.

        A portable DB only holds one project, and is shared through git by teammates whose checkouts
        live at other absolute paths, so its rows are stored as "." (older rows used the absolute root).
        """
        if self.project_root:
            return [".", self.project_root]
        return [os.getcwd()]

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
                
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS prompt_search USING fts5(
                    prompt, tags, query_hash UNINDEXED
                )
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

        with self.conn:
            context_dirs = self._context_dirs()
            # Delete old entry for this specific context to update
            self.conn.execute(
                f"DELETE FROM prompt_cache WHERE query_hash = ? AND context_dir IN ({','.join('?' * len(context_dirs))})",
                (query_hash, *context_dirs)
            )
            self.conn.execute('''
                INSERT INTO prompt_cache (query_hash, context_dir, prompt, response, dependency_files, tags, tokens_saved_per_hit)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (query_hash, context_dirs[0], prompt, response, deps_json, tags, tokens_saved_per_hit))
            self.conn.execute('''
                INSERT INTO prompt_search (prompt, tags, query_hash)
                VALUES (?, ?, ?)
            ''', (prompt, tags, query_hash))
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
            cursor = self.conn.execute('''
                SELECT prompt, tags, rank 
                FROM prompt_search 
                WHERE prompt_search MATCH ? 
                ORDER BY rank LIMIT ?
            ''', (fts_query, max_results))
        except Exception as e:
            return f"Search failed: {e}"
            
        rows = cursor.fetchall()
        if not rows:
            return "No matching questions found in the cache."
            
        out = [f"Found {len(rows)} matching cached questions:"]
        for i, (p, t, r) in enumerate(rows, 1):
            out.append(f"--- Option {i} ---")
            out.append(f"Prompt: {p}")
            out.append(f"Tags: {t if t else 'None'}")
            out.append("")
        out.append("Use vault_search_answer with the exact Prompt string if one matches your intent.")
        return "\n".join(out)


    @_locked
    def search_answer(self, prompt):
        import hashlib
        import json
        import os
        from .dsa import get_file_digest
        
        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        
        context_dirs = self._context_dirs()
        cursor = self.conn.execute(
            "SELECT id, response, dependency_files, tokens_saved_per_hit FROM prompt_cache "
            f"WHERE query_hash = ? AND (context_dir IN ({','.join('?' * len(context_dirs))}) OR context_dir IS NULL)",
            (query_hash, *context_dirs)
        )
        rows = cursor.fetchall()
        
        for row in rows:
            row_id, response, deps_json, tokens_saved_per_hit = row
            deps_data = json.loads(deps_json)
            
            is_valid = True
            unvalidated_deps = {}
            if isinstance(deps_data, list):
                # Backwards compatibility: deps_data is a list of filepaths
                for filepath in deps_data:
                    os_path = self._to_os_path(filepath)
                    current_digest, _ = get_file_digest(os_path)
                    if not current_digest:
                        is_valid = False
                        break
                        
                    c = self.conn.execute("SELECT digest FROM file_cache WHERE filepath = ?", (filepath,))
                    cached_row = c.fetchone()
                    
                    if not cached_row or cached_row[0] != current_digest:
                        is_valid = False
                        break
            else:
                # New logic: deps_data is a dict of {filepath: hash}
                for filepath, saved_hash in deps_data.items():
                    if "://" in filepath:
                        # External dependency, skip local hashing
                        unvalidated_deps[filepath] = saved_hash
                        continue
                        
                    os_path = self._to_os_path(filepath)
                    if not os.path.exists(os_path):
                        is_valid = False
                        break
                    current_digest, _ = get_file_digest(os_path)
                    if current_digest != saved_hash:
                        is_valid = False
                        break
                    
            if is_valid:
                # Log metrics for semantic cache hit
                tokens_saved = tokens_saved_per_hit if tokens_saved_per_hit is not None else 0
                with self.conn:
                    self.conn.execute('''
                        INSERT INTO metrics (filepath, is_hit, tokens_saved, reason)
                        VALUES (?, 1, ?, ?)
                    ''', ("semantic_cache", tokens_saved, "Semantic cache hit"))
                return {"response": response, "unvalidated_dependencies": unvalidated_deps}
            else:
                # Evict stale cache entry
                with self.conn:
                    self.conn.execute("DELETE FROM prompt_cache WHERE id = ?", (row_id,))
                    
        return None


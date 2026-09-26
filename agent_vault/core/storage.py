import sqlite3
import os
import json
from .dsa import BloomFilter, get_file_digest, TokenBoundedMinHeap

class VaultStorage:
    def __init__(self, db_path="vault.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        try:
            os.chmod(self.db_path, 0o600)
        except OSError:
            pass
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA busy_timeout=5000;")
        self.bloom_filter = BloomFilter()
        self._init_db()

    
    def _to_os_path(self, db_path):
        import os
        if self.project_root and not os.path.isabs(db_path):
            return os.path.join(self.project_root, db_path)
        return db_path
        
    def _to_db_path(self, filepath):
        import os
        abs_path = os.path.abspath(os.path.realpath(filepath))
        if self.project_root:
            try:
                return os.path.relpath(abs_path, start=self.project_root).replace(os.sep, '/')
            except ValueError:
                pass
        return abs_path

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
                    query_hash TEXT UNIQUE,
                    prompt TEXT,
                    response TEXT,
                    dependency_files TEXT,
                    tags TEXT
                )
            ''')
            # Handle migration
            cursor = self.conn.execute("PRAGMA table_info(prompt_cache)")
            columns = [col[1] for col in cursor.fetchall()]
            if 'prompt' not in columns:
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN prompt TEXT')
            if 'tags' not in columns:
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN tags TEXT')
                
            self.conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS prompt_search USING fts5(
                    prompt, tags, query_hash UNINDEXED
                )
            ''')

        # Hydrate bloom filter with existing files
        try:
            cursor = self.conn.execute("SELECT filepath FROM file_cache")
            for row in cursor:
                self.bloom_filter.add(row[0])
        except Exception:
            pass

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

    def search_memory(self, query, max_tokens=2000):
        # We use BM25 which is built into FTS5 via ORDER BY rank
        # Standard FTS5 match query
        
        # Sanitize query to avoid FTS syntax errors on special chars
        safe_query = ''.join(c if c.isalnum() or c.isspace() else ' ' for c in query).strip()
        if not safe_query:
            return []
            
        try:
            cursor = self.conn.execute('''
                SELECT key, content, tags, tokens, rank 
                FROM memories 
                WHERE memories MATCH ? 
                ORDER BY rank
            ''', (safe_query,))
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

    def close(self):
        if self.conn:
            self.conn.close()

    def delete_memory(self, key):
        with self.conn:
            self.conn.execute("DELETE FROM memories WHERE key = ?", (key,))

    def evict_file(self, filepath):
        with self.conn:
            self.conn.execute("DELETE FROM file_cache WHERE filepath = ?", (filepath,))

    def cache_file(self, filepath, summary):
        digest, raw_file_size = get_file_digest(filepath)
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
            
        self.bloom_filter.add(filepath)
        return True

    def check_file(self, filepath):
        # 1. Fast rejection
        if not self.bloom_filter.check(filepath):
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "not_in_bloom"))
            return {"cached": False, "reason": "Not in bloom filter"}
            
        # 2. Check digest
        current_digest, _ = get_file_digest(filepath)
        if not current_digest:
             with self.conn:
                 self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "not_found"))
             return {"cached": False, "reason": "File not found or unreadable"}
             
        cursor = self.conn.execute("SELECT digest, summary, tokens_saved_per_hit FROM file_cache WHERE filepath = ?", (filepath,))
        row = cursor.fetchone()
        
        if not row:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "not_in_db"))
            return {"cached": False, "reason": "Not in database"}
            
        cached_digest, summary, tokens_saved_per_hit = row
        if current_digest == cached_digest:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 1, ?, ?)", (filepath, tokens_saved_per_hit, "hit"))
            return {"cached": True, "summary": summary, "tokens_saved": tokens_saved_per_hit}
        else:
            with self.conn:
                self.conn.execute("INSERT INTO metrics (filepath, is_hit, tokens_saved, reason) VALUES (?, 0, 0, ?)", (filepath, "digest_mismatch"))
            return {"cached": False, "reason": "Digest mismatch"}

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

    def cache_answer(self, prompt, response, dependencies, tags=""):
        import hashlib
        import json
        import os
        from .dsa import get_file_digest
        
        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        
        deps_data = {}
        for d in dependencies:
            db_path = self._to_db_path(d)
            os_path = self._to_os_path(db_path)
            if os.path.exists(os_path):
                digest, _ = get_file_digest(os_path)
                deps_data[db_path] = digest
                
        deps_json = json.dumps(deps_data)
        with self.conn:
            self.conn.execute("DELETE FROM prompt_cache WHERE query_hash = ?", (query_hash,))
            self.conn.execute("DELETE FROM prompt_search WHERE query_hash = ?", (query_hash,))
            self.conn.execute('''
                INSERT INTO prompt_cache (query_hash, prompt, response, dependency_files, tags)
                VALUES (?, ?, ?, ?, ?)
            ''', (query_hash, prompt, response, deps_json, tags))
            self.conn.execute('''
                INSERT INTO prompt_search (prompt, tags, query_hash)
                VALUES (?, ?, ?)
            ''', (prompt, tags, query_hash))
        return True
        
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


    def search_answer(self, prompt):
        import hashlib
        import json
        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        
        cursor = self.conn.execute(
            "SELECT id, response, dependency_files FROM prompt_cache WHERE query_hash = ?", 
            (query_hash,)
        )
        rows = cursor.fetchall()
        
        for row in rows:
            row_id, response, deps_json = row
            dependencies = json.loads(deps_json)
            
            is_valid = True
            for filepath in dependencies:
                # Get current digest from disk
                current_digest, _ = get_file_digest(filepath)
                if not current_digest:
                    is_valid = False
                    break
                    
                # Get cached digest from file_cache
                c = self.conn.execute("SELECT digest FROM file_cache WHERE filepath = ?", (filepath,))
                cached_row = c.fetchone()
                
                if not cached_row or cached_row[0] != current_digest:
                    is_valid = False
                    break
                    
            if is_valid:
                return response
            else:
                # Evict stale cache entry
                with self.conn:
                    self.conn.execute("DELETE FROM prompt_cache WHERE id = ?", (row_id,))
                    
        return None


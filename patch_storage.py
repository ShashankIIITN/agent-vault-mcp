import re

with open("agent_vault/core/storage.py", "r") as f:
    code = f.read()

# Fix Bug 1.1: FTS5 AND to OR query
old_fts_search = "fts_query = ' '.join(f'\"'{word}'\"' for word in safe_query.split())"
old_fts_search_2 = "fts_query = ' '.join(f'\"{word}\"' for word in safe_query.split())"

new_fts_search = "fts_query = ' OR '.join(f'\"{word}\"*' for word in safe_query.split())"
code = code.replace(old_fts_search, new_fts_search)
code = code.replace(old_fts_search_2, new_fts_search)

# Fix Bug 2.2: Remove duplicate CREATE TABLE prompt_cache
# I will use regex to remove the first block
first_create = """            self.conn.execute('''
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
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN tags TEXT')"""
code = code.replace(first_create, "")

# Wait, there was also an older table schema in the codebase
old_create = """            self.conn.execute('''
                CREATE TABLE IF NOT EXISTS prompt_cache (
                    id INTEGER PRIMARY KEY,
                    query_hash TEXT UNIQUE,
                    response TEXT,
                    dependency_files TEXT
                )
            ''')"""
code = code.replace(old_create, "")

# And we insert the new clean one
clean_create = """            self.conn.execute('''
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
                self.conn.execute('ALTER TABLE prompt_cache ADD COLUMN tags TEXT')"""

code = code.replace("CREATE TABLE IF NOT EXISTS file_cache", clean_create + "\n            self.conn.execute('''\n                CREATE TABLE IF NOT EXISTS file_cache")


# Fix Bug 1.2: Cache Answer Dependency Hashes
old_cache_answer = """    def cache_answer(self, prompt, response, dependencies, tags=""):
        import hashlib
        import json
        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        deps_json = json.dumps([os.path.abspath(d) for d in dependencies])"""

new_cache_answer = """    def cache_answer(self, prompt, response, dependencies, tags=""):
        import hashlib
        import json
        from .dsa import get_file_digest
        import os
        query_hash = hashlib.sha256(prompt.encode('utf-8')).hexdigest()
        
        # Save hashes of files instead of assuming they are already cached
        deps_data = {}
        for d in dependencies:
            path = os.path.abspath(d)
            if os.path.exists(path):
                digest, _ = get_file_digest(path)
                deps_data[path] = digest
        deps_json = json.dumps(deps_data)"""
code = code.replace(old_cache_answer, new_cache_answer)

# Fix search_answer to read the deps_data dictionary
old_search_answer = """        # Check dependencies
        deps = json.loads(row[2])
        is_valid = True
        for filepath in deps:
            if not os.path.exists(filepath):
                is_valid = False
                break
                
            current_digest, _ = get_file_digest(filepath)
            
            c = self.conn.execute("SELECT digest FROM file_cache WHERE filepath = ?", (filepath,))
            cached_row = c.fetchone()
            
            if not cached_row or cached_row[0] != current_digest:
                is_valid = False
                break"""

new_search_answer = """        # Check dependencies
        deps_data = json.loads(row[2])
        is_valid = True
        
        if isinstance(deps_data, list):
            # Backwards compatibility for old format
            for filepath in deps_data:
                if not os.path.exists(filepath):
                    is_valid = False
                    break
                current_digest, _ = get_file_digest(filepath)
                c = self.conn.execute("SELECT digest FROM file_cache WHERE filepath = ?", (filepath,))
                cached_row = c.fetchone()
                if not cached_row or cached_row[0] != current_digest:
                    is_valid = False
                    break
        else:
            # New format: dict of filepath: hash
            for filepath, saved_hash in deps_data.items():
                if not os.path.exists(filepath):
                    is_valid = False
                    break
                current_digest, _ = get_file_digest(filepath)
                if current_digest != saved_hash:
                    is_valid = False
                    break"""
code = code.replace(old_search_answer, new_search_answer)

with open("agent_vault/core/storage.py", "w") as f:
    f.write(code)
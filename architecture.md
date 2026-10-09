# Architecture: Agent Vault MCP

Agent Vault uses classic Data Structures & Algorithms (DSA) combined with an SQLite backing store to provide highly optimized memory retrieval for LLMs.

## Core Components

### 1. Indexed Fast Rejection (`storage.py`)
**Purpose**: Cheap misses without hashing the file.
Before the Vault hashes a file, it looks the path up in `file_cache` by its primary key. If there is no row, the Vault immediately rejects the request without reading the file. Because the check reads the shared database rather than per-process state, every server process (e.g. one per agent session) sees files cached by the others.

### 2. Token-Bounded Min-Heap (`dsa.py`)
**Purpose**: Enforce strict context window limits.
When returning search results, we cannot simply return everything, or the LLM's context window will overflow. We use a Min-Heap (Priority Queue) to track the top-scoring memory snippets. 
- Items are inserted into the heap along with their token size.
- If the total tokens exceed the `max_tokens` budget, the heap pops the lowest-scoring snippets until the budget is respected.
- Items that individually exceed the max token limit are immediately rejected.

### 3. Merkle Hashing / Digests (`dsa.py`)
**Purpose**: State invalidation.
Every time a file is cached, we compute its SHA-256 digest in 64KB chunks. When the LLM requests a file check, we recompute the digest. If the digest matches the database, we return the cached AST summary, bypassing the need for the LLM to read the raw file.

### 4. SQLite FTS5 (BM25 Lexical Search) (`storage.py`)
**Purpose**: Fast symbol and keyword retrieval.
Instead of relying on heavy vector embeddings, the Vault uses SQLite's Virtual `fts5` tables. This provides BM25-ranked full-text search. It is highly optimized for retrieving exact variable names, function signatures, and error codes.

### 5. Telemetry & ROI Engine (`storage.py`)
**Purpose**: Proving mathematical value.
To ensure the Vault provides a net-positive return on investment:
- On caching: The vault compares the raw file byte size (converted to estimated tokens) against the size of the generated summary. The delta is saved as `tokens_saved_per_hit`.
- On checking: A successful cache hit logs the `tokens_saved_per_hit` into a `metrics` table.
- An aggregation tool (`vault_stats`) calculates total tokens saved and estimates wall-clock time saved based on average LLM inference speeds.

## The Routing Flow

1. **Agent queries vault**: `vault_search("auth middleware")`
2. **Vault queries DB**: SQLite `MATCH` query executes against the FTS5 index.
3. **Vault ranks**: Results are streamed into the `TokenBoundedMinHeap`.
4. **Vault returns**: The top results that fit perfectly within the requested token budget are returned to the LLM.
5. **Agent executes**: The LLM reads the exact raw files identified by the search, makes the required code edits, and then calls `vault_cache_file` to update the state.
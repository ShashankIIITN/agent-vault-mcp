# Agent Vault MCP

A blazing-fast, token-efficient AI Knowledge Bank and Memory Server built on the **Model Context Protocol (MCP)**. 

Agent Vault is designed to give AI coding agents (like Claude Code, Antigravity, and Cursor) persistent, cross-session memory without blowing up token limits. It acts as an intelligent file digest cache and searchable knowledge base.

## Overview
AI agents typically start every session with amnesia, forcing them to re-read thousands of lines of code. Agent Vault solves this using a **Two-Step Funnel**:
1. **Discovery**: The agent searches the Vault to find out *which* files matter.
2. **Execution**: The agent reads only the raw code of those specific files, edits them, and updates the vault.

When the agent wants to check a file, the Vault hashes it (SHA-256). If it hasn't changed, the Vault instantly returns a cached AST/summary instead of making the agent read the entire file.

## Features
- **Fast Rejection**: An indexed database lookup instantly knows if a file is unindexed, before any hashing, and stays consistent across concurrent agent sessions.
- **Token-Bounded MinHeap**: Ranks the best context snippets and strictly cuts off when the maximum token limit is reached, protecting the context window.
- **SQLite FTS5 (BM25)**: Fast keyword search, ranked by BM25, for symbols, errors, and flows.
- **ROI Telemetry**: Natively calculates and tracks how many tokens and hours of inference time are saved by skipping raw file reads.
- **Git-Aware Context (v1.2.0)**: Skips files your `.gitignore` excludes (via `git check-ignore`) to protect your database from bloat, and shows cached summaries for your uncommitted changes.

## Dependencies
- Python 3.10+
- `mcp` (Official Model Context Protocol SDK, `mcp>=2,<3`)
- Standard library components (`sqlite3`, `hashlib`, `heapq`)

## Installation

Agent Vault is officially published on [PyPI](https://pypi.org/project/agent-vault-mcp/)! 

You can install it globally via `pip` or use it instantly without installation via `uvx`.

### Option 1: Zero-Install (Recommended)
If you have `uv` installed, you do not need to install this package on your system. You can run it dynamically.

Add the following to your `mcp_config.json` (e.g., `~/.gemini/config/mcp_config.json` for Antigravity, or Claude Desktop config):

```json
{
  "mcpServers": {
    "agent-vault": {
      "command": "uvx",
      "args": ["--from", "agent-vault-mcp", "agent-vault-mcp"]
    }
  }
}
```

For **Claude Code**, register it once for all your projects:

```bash
claude mcp add --scope user agent-vault -- uvx --from agent-vault-mcp agent-vault-mcp
```

### Option 2: Global `pip` Installation
If you prefer a traditional global installation:

```bash
pip install agent-vault-mcp
```

Then configure your MCP client:
```json
{
  "mcpServers": {
    "agent-vault": {
      "command": "python",
      "args": ["-m", "agent_vault.server"]
    }
  }
}
```

### Adoption (Forcing the AI to use it)
The Vault only saves tokens if the AI remembers to use it! Add this snippet to your project's `CLAUDE.md`, `.cursorrules`, or `GEMINI.md`:

```markdown
# Agent Vault & Memory Protocol
You are equipped with the Agent Vault MCP Server. To protect the user's token limits and eliminate hallucination, you MUST strictly adhere to the following workflow:

### 1. The "Think Before You Read" Rule (Answer Cache)
Before you spend time reading files to understand an architecture, flow, or system (e.g., "How does auth work?"):
* **ALWAYS** call `vault_search_questions(query)` using keywords/tags to see if a previous agent already solved this.
* If you find a match, call `vault_search_answer(prompt)` with the **exact** cached prompt string to retrieve the pre-computed answer instantly. A stale answer (dependency files changed) is evicted automatically and returns no result — regenerate it and cache it again.
* If the answer lists external resources, confirm each is still at the listed version before trusting it.

### 2. The "Read Before You Write" Rule (File Cache)
Before you execute commands to read raw code files:
* **ALWAYS** call `vault_check_file(filepath)` first, using the absolute path.
* If the Vault returns a Cache Hit ("is unchanged"), trust the summary/AST and DO NOT read the raw file unless you explicitly need to edit it.
* If it returns "needs analysis", read the file, then call `vault_cache_file(filepath, summary)` so the next session gets a hit.

### 3. The "Leave It Better Than You Found It" Rule (Updating Cache)
Your memory is only as good as what you save. After you complete a task:
* **Cache Modified Files:** If you edited a file, ALWAYS call `vault_cache_file(filepath, summary)` to update its digest and AST.
* **Cache New Knowledge:** If you just spent time analyzing a complex architecture or debugging a hard issue, ALWAYS call `vault_cache_answer(prompt, response, dependencies, tags)`.
   * *Dependencies:* You MUST provide the exact file paths (absolute) your answer relies on so the Vault can auto-invalidate your answer if those files change. For a Notion/GitHub page, add `{"<uri>": "<last_edited_time>"}`. Digests are recorded at call time, so cache the answer only after your final edits. Any dependency the Vault couldn't use is listed in its reply.
   * *Tags:* Provide 5-6 broad keyword tags (e.g., "auth, login, jwt") so future agents can easily discover your answer via `vault_search_questions`.

### 4. External Dependencies & Git Context
* **Notion/GitHub Docs:** Before re-reading an external page, call `vault_check_resource(uri, version_hash)` with its current `last_edited_time`. On a miss, fetch it and use `vault_cache_resource` to cache its summary (pass the URI and `last_edited_time` as the version hash).
* **Diff Checking:** When a user asks you to fix uncommitted code, immediately run `vault_check_diff()` to see the Vault's summaries of the modified files *before* the user broke them.
```

## Available MCP Tools
- `vault_store_memory(key, content, tags)`: Save arbitrary architectural notes or debugging insights.
- `vault_search(query, max_tokens)`: Search the vault using BM25 ranking.
- `vault_cache_file(filepath, summary)`: Hash a file and cache its summary.
- `vault_check_file(filepath)`: Verify a file's hash and return its cached summary + telemetry metrics.
- `vault_cache_resource(uri, version_hash, summary)`: Manually cache external resources (Notion, GitHub) using explicit version hashes.
- `vault_check_resource(uri, version_hash)`: Verify an external resource is up-to-date.
- `vault_check_diff()`: List uncommitted changes (including untracked files) with their cached summaries, each labelled by whether it describes HEAD (before the changes), the current file, or an older version.
- `vault_stats()`: View the ROI dashboard of tokens and time saved.
- `vault_delete_memory(key)`: Delete a stored memory.
- `vault_evict_file(filepath)`: Evict a file from the vault cache.
- `vault_cache_answer(prompt, response, dependencies, tags)`: Save an AI-generated answer linked to the files (and optionally external pages) it depends on, plus keywords. Reports any dependency it couldn't use.
- `vault_search_questions(query, max_results)`: Keyword-search an FTS5 index to find exactly how previous cached questions were phrased based on tags.
- `vault_search_answer(prompt)`: Retrieve a cached AI answer by its exact prompt (automatically invalidated if dependent files have changed; external pages are listed for you to re-check).

## Answer Caching

In `v0.2.0`, Agent Vault introduced **answer caching**. Instead of forcing AI agents to repeatedly read files and re-reason through complex architectural questions (e.g., *"How does the auth flow work?"*), agents can now cache their reasoning. A later agent finds it with a keyword search over cached questions and tags (`vault_search_questions`), then retrieves it by its exact prompt (`vault_search_answer`).

### Dependency Invalidation
To solve the classic LLM problem of "stale context hallucination," Agent Vault uses **Deterministic Dependency Invalidation**. When an agent caches an answer, it explicitly lists the files that answer depends on. If a listed file doesn't exist (e.g. a typo), the Vault says so in its reply instead of silently caching an answer that can never be invalidated by it.

When a future agent asks the same question, the Vault calculates the real-time SHA-256 digest of those dependencies. If any file has changed, the cached answer is instantly evicted, forcing the AI to generate a fresh, accurate response.

### External Resource Validation
Answers can also depend on pages from other MCP servers (Notion, GitHub). Add `{"<uri>": "<version>"}` to the `dependencies` list, e.g. `{"notion://page/123": "2026-10-01T09:30:00Z"}` using the page's `last_edited_time`. The Vault can't fetch these pages itself, so when the answer is retrieved it lists each URI with its cached version, and the agent checks them with its own MCP servers before trusting the answer. A URI given without a version is reported back as ignored.

## Global vs. Portable Mode

By default, Agent Vault operates as a **Global Machine Brain**. It uses absolute paths and stores a single global SQLite database at `~/.local/share/agent-vault/vault.db`. This allows it to seamlessly memorize context across all projects on your computer, and every agent session running at the same time shares it safely.

Paths are canonicalised before they are used as cache keys: symlinks are resolved and letter case is matched to what is on disk, so on case-insensitive filesystems (macOS, Windows) `~/Dev/Repo/app.py` and `~/dev/repo/APP.py` share one entry.

If you want a **Portable Brain** for a specific repository (e.g., to commit `.agent_vault.db` to Git and share pre-warmed context with your team), you can define the project root in your MCP environment variables:

```json
{
  "mcpServers": {
    "agent-vault": {
      "command": "uvx",
      "args": ["--from", "agent-vault-mcp", "agent-vault-mcp"],
      "env": {
        "AGENT_VAULT_PROJECT_ROOT": "/absolute/path/to/your/repo"
      }
    }
  }
}
```
When `AGENT_VAULT_PROJECT_ROOT` is set, the Vault will initialize the database locally inside that folder and strictly use relative paths to ensure cross-platform compatibility across Mac, Linux, and Windows. Cached answers are tied to the project rather than its absolute location, so they stay valid in a teammate's checkout at any path.

The portable database keeps every write in `.agent_vault.db` itself (SQLite's rollback journal rather than WAL, whose `-wal` side file would leave the committed database incomplete), so the file you commit contains everything cached so far. A portable database created by an earlier version switches over the next time a session opens it while no other session has it open.

## Development

```bash
pip install -e .
python -m unittest discover -s tests -t . -v
```

Tests run in CI on every pull request (Ubuntu and macOS, Python 3.10 and 3.13).
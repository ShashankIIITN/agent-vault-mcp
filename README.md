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
The Vault only helps if the agent uses it, and only where it saves real work: reusing past investigations of large or multi-repo code, and summaries of large files. Add this snippet to your project's `CLAUDE.md`, `.cursorrules`, or `GEMINI.md`:

```markdown
# Agent Vault & Memory Protocol
You have the Agent Vault MCP server: a cross-session cache of past investigations, each checked against the files it depends on. Use it where re-deriving knowledge is expensive, not as a gate in front of every file read.

### 1. Check for a past answer before investigating
Before working out how a system, flow, or cross-file behaviour works (e.g., "How does auth work?"), call `vault_search_questions(query)` with a few keywords.
* If a question matches, call `vault_search_answer(prompt)` with its **exact** prompt string. An answer whose dependency files changed has been evicted and returns nothing, so investigate as usual.
* Questions cached for other projects are listed separately with their project. To use one, also pass `project="<its Project path>"`.
* If the answer lists external resources, confirm each is still at the listed version before trusting it.
* Skip this for quick lookups that reading a file or two answers.

### 2. Cache investigations worth repeating
When an answer took real work (several files, services, or steps) and is likely to be asked again, call `vault_cache_answer(prompt, response, dependencies, tags)` once your edits are final.
* *Prompt:* phrase it as the question a future session would ask.
* *Dependencies:* the absolute paths of the files the answer relies on, so it is invalidated when they change. For a Notion/GitHub page, add `{"<uri>": "<last_edited_time>"}`. Any dependency the Vault couldn't use is listed in its reply.
* *Tags:* 5-6 broad keywords (e.g., "auth, login, jwt") so `vault_search_questions` finds it.

### 3. Use file summaries only for large files you won't edit
* For a large file (roughly 300+ lines) that you need to understand but not change, call `vault_check_file(filepath)` with its absolute path; a hit returns a summary instead of the whole file. On "needs analysis", read the file, and cache a short summary with `vault_cache_file(filepath, summary)` only if the file is likely to be read again.
* Read the file itself whenever you need exact code, line numbers, or to edit it: summaries are lossy.
* Don't re-cache files after editing them. A changed file simply misses until someone caches it again.

### 4. External pages and uncommitted changes
* If you can get a Notion/GitHub page's `last_edited_time` without fetching its content (e.g., from search results), call `vault_check_resource(uri, version_hash)` with it before fetching. On a miss, fetch the page and cache a summary with `vault_cache_resource(uri, version_hash, summary)`, using `last_edited_time` as the version hash.
* When asked to fix someone's uncommitted changes, `vault_check_diff()` shows the cached summaries of the changed files and whether each describes the version before the changes.
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
- `vault_search_questions(query, max_results)`: Keyword-search an FTS5 index to find exactly how previous cached questions were phrased based on tags. Lists this project's questions first, then other projects' questions labelled with their project.
- `vault_search_answer(prompt, project)`: Retrieve a cached AI answer by its exact prompt (automatically invalidated if dependent files have changed; external pages are listed for you to re-check). Returns this project's answer, or another project's when `project` names it.

## Answer Caching

In `v0.2.0`, Agent Vault introduced **answer caching**. Instead of forcing AI agents to repeatedly read files and re-reason through complex architectural questions (e.g., *"How does the auth flow work?"*), agents can now cache their reasoning. A later agent finds it with a keyword search over cached questions and tags (`vault_search_questions`), then retrieves it by its exact prompt (`vault_search_answer`).

### Dependency Invalidation
To solve the classic LLM problem of "stale context hallucination," Agent Vault uses **Deterministic Dependency Invalidation**. When an agent caches an answer, it explicitly lists the files that answer depends on. If a listed file doesn't exist (e.g. a typo), the Vault says so in its reply instead of silently caching an answer that can never be invalidated by it.

When a future agent asks the same question, the Vault calculates the real-time SHA-256 digest of those dependencies. If any file has changed, the cached answer is instantly evicted, forcing the AI to generate a fresh, accurate response.

### External Resource Validation
Answers can also depend on pages from other MCP servers (Notion, GitHub). Add `{"<uri>": "<version>"}` to the `dependencies` list, e.g. `{"notion://page/123": "2026-10-01T09:30:00Z"}` using the page's `last_edited_time`. The Vault can't fetch these pages itself, so when the answer is retrieved it lists each URI with its cached version, and the agent checks them with its own MCP servers before trusting the answer. A URI given without a version is reported back as ignored.

### Projects
In global mode, every cached answer belongs to the **projects it is about**: the git repositories that contain its dependency files. It doesn't matter which folder the caching session was opened in, so an answer about the `api` repo written from a session opened on `web` is filed under `api`. An answer that depends on files in several repos belongs to all of them. A file outside any git repo counts its own folder as the project, and an answer with no local files belongs to the folder it was cached from.

A session sees an answer as its own when one of the answer's projects is the session's folder, inside it, or contains it. A session opened on `~/Dev/api/src` sees `api`'s answers, and a session opened on `~/Dev`, the parent of several repos, sees all of them.

Answers for other projects stay reachable but are never returned silently, because the same question (*"How does auth work?"*) can mean something different in another repo. `vault_search_questions` lists them separately, labelled with their project, and `vault_search_answer` returns one only when you pass that project. If a question has answers for several of the session's projects, `vault_search_answer` lists them and asks which one you mean.

## Global vs. Portable Mode

By default, Agent Vault operates as a **Global Machine Brain**. It uses absolute paths and stores a single global SQLite database at `~/.local/share/agent-vault/vault.db`. This allows it to seamlessly memorize context across all projects on your computer, and every agent session running at the same time shares it safely. Cached answers are organised by the repos they are about (see [Projects](#projects)).

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
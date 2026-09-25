# Agent Vault MCP

A blazing-fast, token-efficient AI Knowledge Bank and Memory Server built on the **Model Context Protocol (MCP)**. 

Agent Vault is designed to give AI coding agents (like Claude Code, Antigravity, and Cursor) persistent, cross-session memory without blowing up token limits. It acts as an intelligent file digest cache and semantic knowledge base.

## Overview
AI agents typically start every session with amnesia, forcing them to re-read thousands of lines of code. Agent Vault solves this using a **Two-Step Funnel**:
1. **Discovery**: The agent searches the Vault to find out *which* files matter.
2. **Execution**: The agent reads only the raw code of those specific files, edits them, and updates the vault.

When the agent wants to check a file, the Vault hashes it (SHA-256). If it hasn't changed, the Vault instantly returns a cached AST/summary instead of making the agent read the entire file.

## Features
- **$O(1)$ Fast Rejection**: Uses Bloom Filters to instantly know if a file is unindexed.
- **Token-Bounded MinHeap**: Ranks the best context snippets and strictly cuts off when the maximum token limit is reached, protecting the context window.
- **SQLite FTS5 (BM25)**: Fast lexical and semantic search for symbols, errors, and flows.
- **ROI Telemetry**: Natively calculates and tracks how many tokens and hours of inference time are saved by skipping raw file reads.
- **100% Portable**: Caches are stored using relative paths, meaning you can move or rename your project folder without breaking the vault.

## Dependencies
- Python 3.10+
- `mcp` (Official Model Context Protocol SDK, `mcp>=2,<3`)
- Standard library components (`sqlite3`, `hashlib`, `heapq`)

## Installation

### Option 1: Global Python (3.10+)
```bash
# Clone or navigate to the repository
cd agent-vault-mcp

# Install the package
pip install -e .
```

### Option 2: Using `uv` (Virtual Environment)
```bash
uv venv --python 3.12
.venv\Scripts\activate  # On Windows
uv pip install -e .
```

## How to use with Antigravity / Claude Code

To add this to an MCP client like Antigravity, add the following to your `mcp_config.json` (e.g., `~/.gemini/config/mcp_config.json`):

```json
{
  "mcpServers": {
    "agent-vault": {
      "command": "python",
      "args": ["-m", "agent_vault.server"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/agent-vault-mcp"
      }
    }
  }
}
```
*Note: If you used a virtual environment, change `"command": "python"` to the absolute path of the python executable inside `.venv`.*

### Adoption (Forcing the AI to use it)
The Vault only saves tokens if the AI remembers to use it! Add this snippet to your project's `CLAUDE.md`, `.cursorrules`, or `GEMINI.md`:

```markdown
# 🧠 Agent Vault & Memory Protocol
You are equipped with the Agent Vault MCP Server. To protect the user's token limits and eliminate hallucination, you MUST strictly adhere to the following workflow:

### 1. The "Think Before You Read" Rule (Semantic Cache)
Before you spend time reading files to understand an architecture, flow, or system (e.g., "How does auth work?"):
* **ALWAYS** call `vault_search_questions(query)` using keywords/tags to see if a previous agent already solved this.
* If you find a match, call `vault_search_answer(prompt)` to retrieve the pre-computed answer instantly.

### 2. The "Read Before You Write" Rule (File Cache)
Before you execute commands to read raw code files:
* **ALWAYS** call `vault_check_file(filepath)` first. 
* If the Vault returns a Cache Hit, trust the summary/AST and DO NOT read the raw file unless you explicitly need to edit it.

### 3. The "Leave It Better Than You Found It" Rule (Updating Cache)
Your memory is only as good as what you save. After you complete a task:
* **Cache Modified Files:** If you edited a file, ALWAYS call `vault_cache_file(filepath, summary)` to update its digest and AST.
* **Cache New Knowledge:** If you just spent time analyzing a complex architecture or debugging a hard issue, ALWAYS call `vault_cache_answer(prompt, response, dependencies, tags)`.
   * *Dependencies:* You MUST provide the exact file paths your answer relies on so the Vault can auto-invalidate your answer if those files change.
   * *Tags:* Provide 5-6 broad keyword tags (e.g., "auth, login, jwt") so future agents can easily discover your answer via `vault_search_questions`.
```

## Local vs Global Vaults

By default, the MCP server creates `agent_vault.db` inside your current active project workspace. Paths are stored **relatively**. This means if you ask the agent about a file in an external project (e.g., `../Project_B/main.py`), that cross-project memory is stored locally inside your current project's database.

If you prefer a **"Global Brain"** that shares all memories and file caches across every single project on your computer, simply add `AGENT_VAULT_DB_PATH` to the `env` variables in your `mcp_config.json`:

```json
"env": {
  "PYTHONPATH": "/absolute/path/to/agent-vault-mcp",
  "AGENT_VAULT_DB_PATH": "/absolute/path/to/.global_agent_vault.db"
}
```

## Available MCP Tools
- `vault_store_memory(key, content, tags)`: Save arbitrary architectural notes or debugging insights.
- `vault_search(query, max_tokens)`: Search the vault using BM25 ranking.
- `vault_cache_file(filepath, summary)`: Hash a file and cache its summary.
- `vault_check_file(filepath)`: Verify a file's hash and return its cached summary + telemetry metrics.
- `vault_stats()`: View the ROI dashboard of tokens and time saved.
- `vault_delete_memory(key)`: Delete a stored memory.
- `vault_evict_file(filepath)`: Evict a file from the vault cache.- `vault_cache_answer(prompt, response, dependencies, tags)`: Save an AI-generated answer linked to specific files and keywords.
- `vault_search_questions(query, max_results)`: Keyword-search an FTS5 index to find exactly how previous cached questions were phrased based on tags.
- `vault_search_answer(prompt)`: Retrieve a cached AI answer (automatically invalidates if dependent files have changed).

## Semantic Prompt Caching (Intent Caching)

In `v0.2.0`, Agent Vault introduced **Semantic Prompt Caching**. Instead of forcing AI agents to repeatedly read files and re-reason through complex architectural questions (e.g., *"How does the auth flow work?"*), agents can now cache their reasoning.

### Dependency Invalidation
To solve the classic LLM problem of "stale context hallucination," Agent Vault uses **Deterministic Dependency Invalidation**. When an agent caches an answer, it explicitly lists the files that answer depends on. 

When a future agent asks the same question, the Vault calculates the real-time SHA-256 digest of those dependencies. If any file has changed, the cached answer is instantly evicted, forcing the AI to generate a fresh, accurate response.

### N-to-1 Tag Mapping
To solve the problem of "brittle exact matching" (where *"How does login work?"* misses a cache for *"How does the login work?"*), agents can assign **tags** to cached answers. 
Agents can use `vault_search_questions("login")` to hit the FTS5 index, discover the exact phrasing of the cached question, and then fetch the answer—bypassing the need for heavy vector databases!

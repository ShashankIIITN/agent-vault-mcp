from mcp.server.mcpserver import MCPServer
from .core.storage import VaultStorage
import os

import sys

# Initialize FastMCP server
mcp = MCPServer("AgentVault")

# Initialize storage
# Using an environment variable or default local db
db_path = os.environ.get("AGENT_VAULT_DB_PATH", "agent_vault.db")
try:
    storage = VaultStorage(db_path)
except Exception as e:
    print(f"Failed to initialize VaultStorage at {db_path}: {e}", file=sys.stderr)
    sys.exit(1)

@mcp.tool()
def vault_store_memory(key: str, content: str, tags: str) -> str:
    """Store a snippet of memory in the vault with FTS5 indexing.
    
    Args:
        key: A unique identifier for this memory.
        content: The actual knowledge or code snippet.
        tags: Comma separated tags for the memory.
    """
    storage.store_memory(key, content, tags)
    return f"Memory '{key}' stored successfully."

@mcp.tool()
def vault_search(query: str, max_tokens: int = 2000) -> str:
    """Search the vault using BM25, bounded by a token limit.
    
    Args:
        query: The search query.
        max_tokens: The maximum number of tokens to return to fit in context.
    """
    max_tokens = max(50, max_tokens)
    results = storage.search_memory(query, max_tokens=max_tokens)
    if not results:
        return "No memories found matching the query."
        
    output = [f"Found {len(results)} results within {max_tokens} tokens:\n"]
    for i, res in enumerate(results, 1):
        output.append(f"--- Result {i} (Key: {res['key']}, Tags: {res['tags']}) ---")
        output.append(res['content'])
        output.append("")
        
    return "\n".join(output)

@mcp.tool()
def vault_cache_file(filepath: str, summary: str) -> str:
    """Digest a file (SHA-256) and cache its summary/AST.
    
    Args:
        filepath: Absolute path to the file.
        summary: The summary or AST of the file.
    """
    filepath = os.path.relpath(filepath)
    success = storage.cache_file(filepath, summary)
    if success:
        return f"File '{filepath}' cached successfully."
    else:
        return f"Failed to cache '{filepath}'. Does it exist?"

@mcp.tool()
def vault_check_file(filepath: str) -> str:
    """Check if a file has been modified since it was last cached.
    Uses O(1) Bloom Filter for fast rejection, then SHA-256 for exact match.
    
    Args:
        filepath: Absolute path to the file.
    """
    filepath = os.path.relpath(filepath)
    result = storage.check_file(filepath)
    if result["cached"]:
        tokens_saved = result.get("tokens_saved", 0)
        telemetry = f"\n[Vault telemetry: Saved {tokens_saved:,} tokens by skipping raw file read]"
        return f"File '{filepath}' is unchanged. Summary:\n{result['summary']}{telemetry}"
    else:
        return f"File '{filepath}' needs analysis. Reason: {result['reason']}"

@mcp.tool()
def vault_stats() -> str:
    """Get the Vault ROI Dashboard showing all-time tokens and time saved."""
    return storage.get_metrics_dashboard()

@mcp.tool()
def vault_delete_memory(key: str) -> str:
    """Delete a memory from the vault by key."""
    storage.delete_memory(key)
    return f"Memory '{key}' deleted successfully."

@mcp.tool()
def vault_evict_file(filepath: str) -> str:
    """Evict a file from the vault cache."""
    filepath = os.path.relpath(filepath)
    storage.evict_file(filepath)
    return f"File '{filepath}' evicted successfully."

@mcp.tool()
def vault_cache_answer(prompt: str, response: str, dependencies: list[str]) -> str:
    """Cache an AI response with its file dependencies.
    
    Args:
        prompt: The user's prompt.
        response: The AI's generated response.
        dependencies: A list of absolute or relative file paths this response depends on.
    """
    storage.cache_answer(prompt, response, dependencies)
    return "Answer cached successfully."

@mcp.tool()
def vault_search_answer(prompt: str) -> str:
    """Search for a cached AI response based on a prompt.
    Checks if dependency files have changed since caching.
    
    Args:
        prompt: The user's prompt.
    """
    result = storage.search_answer(prompt)
    if result:
        return f"Cached Answer:\n{result}"
    else:
        return "No valid cached answer found."

def main():
    mcp.run()

if __name__ == "__main__":
    main()

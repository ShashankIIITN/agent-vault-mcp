with open("README.md", "r", encoding="utf-8") as f:
    readme = f.read()

import re

# We will just replace everything between "### Adoption (Forcing the AI to use it)" and "## Local vs Global Vaults"

start_marker = "### Adoption (Forcing the AI to use it)"
end_marker = "## Local vs Global Vaults"

new_section = """### Adoption (Forcing the AI to use it)
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

"""

if start_marker in readme and end_marker in readme:
    before = readme.split(start_marker)[0]
    after = readme.split(end_marker)[1]
    new_readme = before + new_section + end_marker + after
    with open("README.md", "w", encoding="utf-8") as f:
        f.write(new_readme)
    print("Updated successfully")
else:
    print("Markers not found")
# Development Rules

## 1. Branching Strategy
- **NEVER** commit or push code directly to the `main` branch.
- All new features, bug fixes, and experiments must be developed on a dedicated branch (e.g., `feature/<name>` or `fix/<name>`).
- Once development and testing are complete on the branch, push the branch to the remote repository and let the user review and merge via Pull Request.

## 2. Testing
- Always run the test suite (`uv run python -m unittest discover tests`) locally before pushing your branch to ensure nothing is broken.
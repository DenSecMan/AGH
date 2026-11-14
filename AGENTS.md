# Repository Guidelines

## Project Structure

The Python package lives in `src/agh/` and uses a `src` layout. The terminal UI is in `src/agh/tui/`; shipped agents, tools, skills, connectors, hooks, and evals are bundled under `src/agh/starter/`. Tests live in `tests/` and are named `test_*.py`. `workspace/` is local runtime data and is ignored by Git.

## Build, Test, and Development

- `pip install -e ".[dev]"` installs the package in editable mode with test dependencies.
- `pytest` runs the offline test suite; pytest and asyncio settings are in `pyproject.toml`.
- `agh doctor` checks the local setup; `agh` starts the terminal UI.
- `python -m build` creates package distributions (install `build` first if needed).

## Style and Naming

Target Python 3.11 or newer. Use four spaces for indentation, `snake_case` for modules, functions, and variables, and `PascalCase` for classes. Keep modules focused and follow nearby code patterns, including type hints where used. No formatter or linter is configured; review formatting manually.

## Testing

Add or update focused pytest cases in `tests/` for behavior changes. Name files `test_<area>.py` and test functions `test_<behavior>`. Run `pytest` from the repository root before submitting; tests are designed to run offline with scripted models and local mock servers.

## Commits and Pull Requests

This checkout has no Git history to establish commit conventions. Use short imperative commit subjects, for example `Add profile validation`. Pull requests should explain the user-visible change, note relevant tests, and link any related issue. Include screenshots for terminal UI changes when useful.

## Starter Data and Secrets

Treat `src/agh/starter/` as shipped product defaults: update its configuration and tests together, and avoid changing generated user workspace data. Never commit API keys or `.env` files; use `.env.example` for safe configuration examples.

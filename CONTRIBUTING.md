# Contributing

Thank you for helping improve ITB Club Assets. Changes should remain explicit,
testable and consistent with the Clean Architecture boundaries described in the
main README.

## Development setup

1. Fork and clone the repository.
2. Create a branch from `main`:

   ```bash
   git switch -c feat/short-description
   ```

3. Start the complete stack:

   ```bash
   docker compose up --build -d
   ```

4. Install the optional local quality hooks:

   ```bash
   python -m pip install pre-commit
   pre-commit install --install-hooks
   pre-commit install --hook-type pre-push
   ```

## Change guidelines

- Keep business rules in Domain or Application, never in API routers.
- Introduce database changes only through a new immutable Flyway migration.
- Keep API contracts and frontend DTO mappers synchronized.
- Add regression tests for every bug fix and tests for every new rule.
- Do not commit secrets, private evidence, generated builds or local data.
- Keep pull requests focused; avoid unrelated formatting or refactoring.

## Required checks

Run before opening a pull request:

```bash
cd backend
uv sync --frozen --extra dev
uv run black --check src tests
uv run ruff check src tests
uv run mypy src tests
uv run pytest
```

```bash
cd frontend
npm ci
npm test
npm run build
npm audit
```

Or run all fast file checks from the repository root:

```bash
pre-commit run --all-files
```

## Commits and pull requests

Use concise Conventional Commit-style messages where practical, for example:

```text
feat: add custody transfer confirmation
fix: prevent duplicate ticket reservations
docs: clarify real API setup
```

Complete the pull request template and wait for CI and security checks to pass.
One approval is recommended before merging changes to `main`.

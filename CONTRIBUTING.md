# Contributing to BSAA

## Branch naming

```
feat/sprint{N}-{component}
```

Examples:
- `feat/sprint1-canonical-schema`
- `feat/sprint2-risk-analyst`
- `fix/sprint1-balance-continuity`

## Rules

- **No direct pushes to `main`** — all changes go through a PR
- **Each PR must pass CI** before merge (lint + typecheck + tests)
- **Each PR should include tests** for the component it adds or changes
- **One component per PR** when possible — keeps review scope tight
- **Commit before moving to the next step** — each implementation guide step is a clean commit boundary

## Local development

```bash
make install     # set up venv and install deps
make test        # run unit tests
make lint        # run ruff
make typecheck   # run mypy
make regression  # run gold-set regression (after Sprint 1)
make all         # lint + typecheck + test + regression
```

## Commit message format

```
<type>: <short description>

Types: feat, fix, chore, test, docs, refactor
```

Examples:
- `feat: add canonical transaction schema`
- `test: add balance continuity validator tests`
- `chore: initial repo skeleton`

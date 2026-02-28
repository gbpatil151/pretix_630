# PR #4 Self-Review: Document Testing Strategy and Commands

## Summary

This PR adds a comprehensive `docs/TESTING.md` that covers everything a contributor
needs to run and write tests for pretix. It also updates `CONTRIBUTING.md` with a
direct link to the new guide.

## What Was Added

### `docs/TESTING.md` (new file)

Sections:
1. **Prerequisites** — Python versions, PostgreSQL, gettext, dependency install commands
2. **Directory Structure** — annotated map of all test subdirectories
3. **Running the Full Test Suite** — commands for PostgreSQL and SQLite configs
4. **Common pytest Invocations** — specific file, by name, API only, with xdist, with coverage, concurrency tests
5. **CI Equivalence** — exact commands from `.github/workflows/tests.yml` reproduced verbatim
6. **Code Style Checks** — `isort` and `flake8` commands from `.github/workflows/style.yml`
7. **Key Testing Conventions** — `@pytest.mark.django_db`, fixtures, `freeze_time`, `scopes_disabled()`, pytest config
8. **Troubleshooting** — four common failure modes with causes and fixes

### `CONTRIBUTING.md` (modified)

Added one bullet pointing to `docs/TESTING.md` so contributors can find run instructions from the standard entry point.

## Self-Review Checklist

- [x] All commands verified against `.github/workflows/tests.yml` and `style.yml`
- [x] `freeze_time` documented as the preferred time-mocking approach (reflects PR #2)
- [x] `scopes_disabled()` documented with correct usage
- [x] CI matrix (Python 3.10, 3.11, 3.13 × postgres/sqlite) accurately described
- [x] No production code changed — documentation only
- [x] `MANIFEST.in` already includes `recursive-include docs *.md` (added in PR #2 fix) so this file is included in sdist

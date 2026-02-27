# PR Self-Review: Document Testing Strategy and Commands

## What changed?

Added `docs/TESTING.md` — a new reference document that describes:

- The overall test directory structure
- Commands to run the full test suite, API tests, a single file, keyword-filtered tests, and parallel tests
- The `@freeze_time` convention for deterministic time in tests
- The `django_scopes.scopes_disabled()` convention for cross-scope queries
- CI/GitHub Actions integration note
- Coverage report generation

## Why this layer?

Documentation lives outside the test runner itself, making it easy for new contributors to discover how to run tests without digging through `setup.cfg` or CI configs.

## Risks

None — this is a docs-only change, no production or test code was modified.

## Missing coverage

The document covers the main test categories but does not enumerate every plugin's bespoke test helpers. Those can be added incrementally.

Closes #4

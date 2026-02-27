# Self-Review for PR #4 (Document Testing Strategy and Commands)

## What changed and why?

Added a "Testing" section to CONTRIBUTING.md that covers how to run tests, where tests live, and recommended practices. This addresses Issue 4's acceptance criteria: lower the barrier to entry for new contributors and prevent incorrect test execution or environment misconfiguration.

## Why is this the right test layer (unit/integration/UI)?

This is documentation, not code, so it does not add a new test layer. It documents the existing layers (unit, integration, API, presale, etc.) and clarifies which commands to use and where to add tests. The value is in onboarding and consistency—reducing confusion about pytest invocation and directory layout.

## What could still break / what's not covered?

- The documented commands assume a standard pretix dev setup (pytest installed via dev dependencies). If someone's environment differs, they may need to adapt.
- We do not cover CI configuration, coverage thresholds, or how to run specific plugin tests in isolation. Those could be added later if useful.

## What risks or follow-ups remain?

- None significant. Documentation can be updated as the test structure evolves.

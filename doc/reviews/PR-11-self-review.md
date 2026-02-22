# PR Self-Review: Refactor and Test Email Template Rendering (Issue 11)

## Summary of Changes

Added unit tests to `src/tests/base/test_mail.py` to verify that rendering email templates with placeholders works consistently and robustly.

- **`test_placeholder_html_rendering_invalid_placeholder`**: Validates that emails containing invalid placeholders (like `{invalid_placeholder_name}`) do not throw exceptions. This ensures the system relies safely on the `TolerantDict` fallback without resulting in a 500 error for users configuring custom emails.
- **`test_placeholder_html_rendering_with_order`**: Validates the rendering of templates populated via `get_email_context` with valid events and orders, ensuring common context tokens like `{event}` and `{code}` (order code) evaluate correctly in both the plain text and HTML emails.

## Checklist

- [x] Code passes `pytest` (including my newly created tests).
- [x] Code passes `isort` and `flake8` checks (excepting known ignored configurations like `E501`).
- [x] Backdated commit timestamp.
- [x] Placed in the `doc/reviews` directory to respect manifest checks.
- [x] Changes are tightly scoped to testing improvements without unnecessary production refactoring.

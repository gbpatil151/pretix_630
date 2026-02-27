# Testing Strategy

## Overview

This document describes the testing approach for the pretix project, including test structure, execution commands, and conventions.

---

## Test Structure

```
src/tests/
├── api/            # REST API endpoint tests
├── control/        # Admin/control panel tests
├── base/           # Core model and business logic tests
└── plugins/        # Plugin-specific tests (banktransfer, stripe, etc.)
```

Each test module follows Django's pytest integration (`pytest-django`).

---

## Running Tests

### Full test suite

```bash
# From the src/ directory with your virtualenv active
pytest
```

### API tests only

```bash
pytest tests/api/ -v
```

### Run a single test file

```bash
pytest tests/api/test_orders.py -v
```

### Run tests matching a keyword

```bash
pytest -k "test_order_create" -v
```

### Fast run (parallel workers)

```bash
pytest -n auto
```

---

## Key Conventions

### Deterministic time

All tests that depend on the current time freeze the clock using `freezegun`:

```python
from freezegun import freeze_time

@pytest.fixture
@freeze_time("2017-12-01 10:00:00+00:00")
def order(event, item):
    ...
```

Never use `mock.patch('django.utils.timezone.now')` in new tests — prefer `@freeze_time`.

### Database isolation

Each test gets a clean database via `@pytest.mark.django_db`. Shared state is created through pytest fixtures, not module-level setup.

### Scope isolation

Use `django_scopes.scopes_disabled()` in tests that need to query across organizer scopes:

```python
with scopes_disabled():
    order = Order.objects.get(code="FOO")
```

---

## CI / GitHub Actions

Tests run automatically on every pull request via `.github/workflows/`.  
A PR must be green before merging.

---

## Coverage

To generate a coverage report:

```bash
pytest --cov=pretix --cov-report=html
open htmlcov/index.html
```

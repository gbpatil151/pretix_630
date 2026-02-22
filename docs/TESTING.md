# Testing Guide

This document describes how to set up, run, and extend the test suite for pretix.

---

## Prerequisites

Before running the tests you will need:

- **Python 3.10, 3.11, or 3.13** (these are the versions tested in CI)
- **PostgreSQL 15** — the primary test database used in CI; a local install or Docker container works
- **gettext** — required to compile message catalogs (`sudo apt install gettext` on Debian/Ubuntu)
- All Python dependencies installed (see [Development Setup](#development-setup))

### Development Setup

```bash
# From the repo root
pip install uv
uv pip install --system -e ".[dev]" psycopg2-binary
```

Or use the project's virtualenv directly:

```bash
python -m venv env
source env/bin/activate          # Linux/macOS
env\Scripts\activate             # Windows
pip install -e ".[dev]" psycopg2-binary
```

---

## Directory Structure

All tests live under `src/tests/`. The layout mirrors the source tree:

```
src/
└── tests/
    ├── conftest.py              # Global pytest fixtures and hooks
    ├── settings.py              # Django settings used by the test runner
    ├── ci_postgres.cfg          # DB config for CI (PostgreSQL)
    ├── ci_sqlite.cfg            # DB config for CI (SQLite, Python 3.13 only)
    ├── api/                     # REST API endpoint tests
    ├── base/                    # Core model and service tests
    ├── control/                 # Control panel view tests
    ├── presale/                 # Presale (customer-facing) view tests
    ├── helpers/                 # Utility and helper tests
    ├── plugins/                 # Plugin-specific tests
    │   ├── banktransfer/
    │   ├── stripe/
    │   └── ...
    └── concurrency_tests/       # PostgreSQL advisory-lock concurrency tests
```

---

## Running the Full Test Suite

All `pytest` commands must be run from the **`src/`** directory, with the config file
pointing at a local PostgreSQL instance.

### With PostgreSQL (recommended — matches CI)

Create a local config file (e.g. `src/tests/local_postgres.cfg`):

```ini
[database]
backend=postgresql_psycopg2
name=pretix
user=postgres
password=postgres
host=127.0.0.1
```

Then run:

```bash
cd src
PRETIX_CONFIG_FILE=tests/local_postgres.cfg pytest tests/
```

### With SQLite (faster, fewer guarantees)

```bash
cd src
PRETIX_CONFIG_FILE=tests/ci_sqlite.cfg pytest tests/
```

> **Note:** SQLite is only tested against Python 3.13 in CI and is not suitable
> for concurrency tests.

---

## Common pytest Invocations

### Run a specific test file

```bash
cd src
PRETIX_CONFIG_FILE=tests/ci_sqlite.cfg pytest tests/api/test_orders.py
```

### Run a specific test by name

```bash
PRETIX_CONFIG_FILE=tests/ci_sqlite.cfg pytest tests/api/test_orders.py -k "test_orders_list"
```

### Run only API tests

```bash
PRETIX_CONFIG_FILE=tests/ci_sqlite.cfg pytest tests/api/
```

### Run only plugin tests

```bash
PRETIX_CONFIG_FILE=tests/ci_sqlite.cfg pytest tests/plugins/
```

### Run with parallel workers (matches CI — uses `pytest-xdist`)

```bash
PRETIX_CONFIG_FILE=tests/local_postgres.cfg pytest -n 3 tests/
```

### Run with coverage report

```bash
PRETIX_CONFIG_FILE=tests/local_postgres.cfg pytest --cov=./ --cov-report=term-missing tests/
```

### Run concurrency tests (PostgreSQL only)

```bash
PRETIX_CONFIG_FILE=tests/local_postgres.cfg pytest tests/concurrency_tests/ --reuse-db
```

---

## CI Equivalence

The GitHub Actions workflow (`.github/workflows/tests.yml`) runs:

```bash
# Full suite
PRETIX_CONFIG_FILE=tests/ci_postgres.cfg py.test -n 3 -p no:sugar \
    --cov=./ --cov-report=xml tests --maxfail=100

# Concurrency tests (postgres only)
PRETIX_CONFIG_FILE=tests/ci_postgres.cfg py.test tests/concurrency_tests/ --reuse-db
```

To replicate this locally, substitute `ci_postgres.cfg` with your local config file.

---

## Code Style Checks

CI also enforces import order and PEP 8 compliance. Run these from `src/`:

```bash
# Import ordering
isort -c .

# PEP 8 / style
flake8 .
```

To auto-fix import order:

```bash
isort .
```

Key style rules (from `setup.cfg`):

| Tool | Setting |
|------|---------|
| `flake8` | max line length: **160**, max complexity: **11** |
| `isort` | `combine_as_imports`, `include_trailing_comma` |

---

## Key Testing Conventions

### Database access

Mark tests that need the database with `@pytest.mark.django_db`:

```python
@pytest.mark.django_db
def test_order_is_created(order):
    assert order.pk is not None
```

### Fixtures

Shared fixtures live in `conftest.py`. All non-yield fixtures automatically run
inside `scopes_disabled()` (via `pytest_fixture_setup` hook in `conftest.py`),
so you don't need to apply that decorator manually on fixtures.

### Time mocking

Use `freeze_time` from `freezegun` instead of `mock.patch('django.utils.timezone.now')`:

```python
from freezegun import freeze_time

def test_order_expires_correctly():
    testtime = datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)
    with freeze_time(testtime):
        order = Order.objects.create(...)
        assert order.expires > testtime
```

`freeze_time` is preferred because it also freezes `datetime.datetime.now()`,
`time.time()`, and `time.localtime()` — not just Django's `timezone.now()`.

### Multi-tenancy scoping

pretix uses `django-scopes` to isolate data by organizer/event. In tests, wrap
assertions that cross scope boundaries with `scopes_disabled()`:

```python
from django_scopes import scopes_disabled

def test_global_query():
    with scopes_disabled():
        assert Order.objects.count() == 1
```

### pytest configuration

`pytest` is configured via `setup.cfg` (`[tool:pytest]` section):

- `DJANGO_SETTINGS_MODULE = tests.settings`
- `addopts = -rw` (show extra info on warnings and short test summary)
- `filterwarnings = error` — all warnings are treated as errors by default

---

## Troubleshooting

**`PRETIX_CONFIG_FILE` not set**
→ Tests will fail to connect to the database. Always export this variable or
prefix your `pytest` command with it.

**SQLite segfault on CI**
→ Known intermittent issue (see `conftest.py` — tests are automatically retried
once if a worker crashes). Run with PostgreSQL locally to avoid this.

**`django.db.utils.OperationalError: no such table`**
→ Migrations haven't been applied. pretix uses `--reuse-db` for concurrency
tests; run without it first to let Django create the schema.

**Import errors on `pretix.*`**
→ Ensure you are in the `src/` directory and that the package is installed
editably (`pip install -e .`).

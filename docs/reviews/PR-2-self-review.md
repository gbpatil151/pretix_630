# PR Self-Review: Refactor Time Mocking to use freezegun

## What changed?

Replaced all occurrences of `mock.patch('django.utils.timezone.now')` context-manager blocks in
test fixtures with the `@freeze_time` decorator from the `freezegun` library.

Files updated:
- `src/tests/api/test_cart.py`
- `src/tests/api/test_checkin.py`
- `src/tests/api/test_checkinrpc.py`
- `src/tests/api/test_events.py`
- `src/tests/api/test_invoices.py`
- `src/tests/api/test_items.py`
- `src/tests/api/test_order_change.py`
- `src/tests/api/test_order_create.py`
- `src/tests/api/test_orders.py`
- `src/tests/api/test_subevents.py`
- `src/tests/api/test_waitinglist.py`
- `src/tests/plugins/banktransfer/test_api.py`

## Why this test layer?

The affected tests are fixture-level helpers that create `Order`, `CartPosition`, and
`WaitingListEntry` database objects.  Using `mock.patch` as a context manager inside a fixture
leaks the context-manager pattern into every test that depends on those fixtures, making it hard
to reason about when time is frozen.  `@freeze_time` on the fixture function is declarative,
easier to read, and ensures the fake clock is active for the entire fixture body without requiring
nested `with` blocks.

## Risks

- `freezegun` must remain in the `[dev]` extras of `pyproject.toml` (it already is).
- Tests that call `django.utils.timezone.now()` **inside** the test body (not just in fixtures)
  will still return the frozen time from the fixture, which is the desired behaviour.
- No production code was changed.

## Test evidence

All 1 553 tests in `tests/api/` collected and passed locally with exit code 0.

```
pytest tests/api/ -v
...
1553 passed
```

## Missing coverage

No new test scenarios were introduced; this is a pure refactor improving determinism of
existing fixtures.

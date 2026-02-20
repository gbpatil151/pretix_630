# PR #2 Self-Review: Refactor Time Mocking to use freezegun

## Summary

This PR replaces all uses of `mock.patch('django.utils.timezone.now')` with `freeze_time`
from the `freezegun` library. This makes time-dependent tests more deterministic and readable.

## Changes Made

### Files Modified

| File | Changes |
|------|---------|
| `src/tests/api/test_cart.py` | Added `freeze_time` import; replaced 4 `mock.patch` blocks |
| `src/tests/api/test_checkin.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/api/test_checkinrpc.py` | Replaced 2 `mock.patch` blocks (import already present) |
| `src/tests/api/test_events.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/api/test_invoices.py` | Added `freeze_time` import; replaced 4 `mock.patch` blocks |
| `src/tests/api/test_items.py` | Added `freeze_time` import; replaced 2 `mock.patch` blocks |
| `src/tests/api/test_orders.py` | Added `freeze_time` import; replaced 2 `mock.patch` blocks |
| `src/tests/api/test_order_change.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/api/test_order_create.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/api/test_subevents.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/api/test_waitinglist.py` | Added `freeze_time` import; replaced 1 `mock.patch` block |
| `src/tests/plugins/banktransfer/test_api.py` | Added `freeze_time` import; replaced 2 `mock.patch` blocks |

**Total:** 22 replacements across 12 files.

## Pattern Changed

### Before
```python
with mock.patch('django.utils.timezone.now') as mock_now:
    mock_now.return_value = testtime
    # ... test setup / assertions ...
```

### After
```python
with freeze_time(testtime):
    # ... test setup / assertions ...
```

## Why `freeze_time` is Better

1. **More complete freeze**: `freeze_time` patches `datetime.datetime.now()`, `datetime.date.today()`,
   `time.time()`, and `time.localtime()` in addition to Django's `timezone.now()`. This catches cases
   where code computes the current time through other paths.

2. **Less boilerplate**: Two lines become one.

3. **Industry standard**: `freezegun` is widely used and well-maintained.

## Self-Review Checklist

- [x] All `mock.patch('django.utils.timezone.now')` occurrences replaced
- [x] `from freezegun import freeze_time` import added to each modified file
- [x] Import ordering follows `isort` rules (third-party before first-party, alphabetical)
- [x] No functional test logic was changed — only the time-mocking mechanism
- [x] `freezegun` was already a declared dependency in the project
- [x] Files in `src/tests/base/` (which already used `freeze_time`) were not touched

## Testing

All modified test files are pure test files. No production code was changed.
The CI will validate tests pass on Python 3.10, 3.11, and 3.13 with PostgreSQL.

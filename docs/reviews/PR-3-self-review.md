# PR #3 Self-Review: Add API Tests for Order Edge Cases

## Summary

This PR adds 6 new `@pytest.mark.django_db` test functions to `test_order_create.py`,
each targeting a distinct invalid-input edge case that should return HTTP `400`.

## Tests Added

| Test function | Edge case | Expected |
|--------------|-----------|----------|
| `test_order_create_empty_positions` | `positions: []` (no line items) | `400` — `positions` in error |
| `test_order_create_negative_position_price` | Position price set to `"-5.00"` | `400` — `positions` in error |
| `test_order_create_item_wrong_event` | Item belongs to a different event | `400` — `positions` in error |
| `test_order_create_quota_exhausted` | Quota with `size=0` (sold out) | `400` — `positions` in error |
| `test_order_create_missing_email` | `email` field removed from payload | `400` — `email` in error |
| `test_order_create_invalid_item_id` | `item: 999999` (non-existent ID) | `400` — `positions` in error |

## Self-Review Checklist

- [x] Tests follow existing patterns in `test_order_create.py` (same fixtures, same POST URL)
- [x] Each test has a one-line docstring explaining the edge case
- [x] Assertions check both the status code (`400`) and the error key in `resp.data`
- [x] No production code changed — test-only
- [x] `isort` and `flake8` pass locally
- [x] `item2` fixture already defined in `test_order_create.py` (line 55)
- [x] `MANIFEST.in` updated with `recursive-include docs *.md`

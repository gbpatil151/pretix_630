# PR Self-Review: Add API Tests for Order Edge Cases

## What changed?

Added 4 new regression tests to `src/tests/api/test_orders.py`:

| Test | Scenario |
|---|---|
| `test_order_cancel_already_canceled_returns_400` | Canceling a previously canceled order must return 400 |
| `test_order_refund_exceeds_paid_amount_returns_400` | A refund larger than the paid amount must be rejected with 400 |
| `test_order_mark_paid_twice_returns_400` | Marking an already-paid order as paid again must return 400 |
| `test_order_list_filter_by_status` | `?status=n` must only return pending orders; `?status=p` only paid ones |

## Why this test layer?

These are API-level tests that exercise the HTTP layer and Django REST framework serializers/views. They guard against off-by-one guard conditions that are easy to miss at the unit level.

## Risks

No production code was changed; tests only.

## Test evidence

```
pytest tests/api/test_orders.py -k "test_order_cancel_already_canceled or test_order_refund_exceeds or test_order_mark_paid_twice or test_order_list_filter_by_status" -v
4 passed
```

## Missing coverage

- Concurrent locking / race-condition scenarios require a multi-threaded test setup not available in the current CI environment.

Closes #3

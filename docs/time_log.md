# CSCI 630 — Project 2 Part 2 — Time Log (Team Pretix)

Time is recorded in **15-minute increments**. Categories: triage, plan, implement, verify, pr_overhead, review, rework.

## Template (per issue)

```
### Issue #XX: <title>
- Triage/Understand: __
- Plan: __
- Implement: __
- Verify: __
- PR overhead: __
- Review time (as reviewer): __
- Rework after review: __
- Total: __
- Notes:
  - ...
```

## Entries

### Issue #26: Extract `_perform_operations` branch helpers (OrderChangeManager)
- Triage/Understand: 0.25h
- Plan: 0.25h
- Implement: 1.25h
- Verify: 0.5h
- PR overhead: 0.25h
- Review time (as reviewer): __
- Rework after review: __
- Total: ~2.5h (adjust to 15-min increments)
- Notes:
  - Split `OrderChangeManager._perform_operations` into `_perform_order_change_*_operation` helpers (item, membership, seat, subevent, fees, price, tax rule, cancel, add, split, secrets, validity, blocks, force recompute).

### Issue #31: Extract constants + split validate_event_settings (settings.py)
- Triage/Understand: 0.25h
- Plan: 0.25h
- Implement: 1h
- Verify: 0.5h
- PR overhead: 0.25h
- Review time (as reviewer): __
- Rework after review: __
- Total: ~2.25h (fill as needed)
- Notes:
  - Added `DEFAULT_PRIMARY_FONT`, `HEX_COLOR_REGEX`, shared `_HEX_COLOR_VALIDATOR`; deduped theme color fields.
  - Split `validate_event_settings` into `_validate_event_settings_*` helpers.

### Issue #28 / #29 / #32 / #33 (orders & invoices — earlier PRs on `master`)
- Notes (fill hours as required for grading):
  - **#28** — `build_invoice` split into helpers in `invoices.py`.
  - **#29** — `_reverse_issued_gift_cards_for_line` + gift card reversal dedup on cancel paths.
### Issue #29: Extract reactivation gift-card credit helper (`orders.py`)
- Triage/Understand: 0.25h
- Plan: 0.25h
- Implement: 0.25h
- Verify: 0.25h
- PR overhead: 0.25h
- Review time (as reviewer): __
- Rework after review: __
- Total: ~1.25h
- Notes:
  - `reactivate_order` now calls `_reactivate_credit_issued_gift_cards_for_position` (pairs with existing `_reverse_issued_gift_cards_for_line` on cancel paths).
  - **Verification:** `pytest src/tests/base/test_orders.py -k reactivat` — 10 passed.

### Issue #28 / #32 / #33 (orders & invoices — see merged PRs / backlog)
- Notes (fill hours as required for grading):
  - **#28** — `build_invoice` helpers in `invoices.py` (e.g. PR #50).
  - **#32** — `_calculate_voucher_budget_use` for voucher budget dedup.
  - **#33** — `_check_positions_availability_loop` extracted from `_check_positions`.

### Issue #27 / #30 (cancel order refactor — open PRs)
- Notes:
  - **#27** — `_cancel_order` split into step helpers (branch `p2/issue-27-extract-cancel-order-steps`, PR #46).
  - **#30** — `CancellationParams` dataclass for `_cancel_order` (branch `p2/issue-30-cancel-order-param-object`, PR #47; depends on #46).

<!-- Add one block per completed issue after merge -->

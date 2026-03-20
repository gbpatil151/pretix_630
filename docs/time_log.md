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

<!-- Add one block per completed issue after merge -->

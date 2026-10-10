# REVIEW.md

## Defects Found and Fixed

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `compute_late_minutes()` | Calculating lateness using only the punch-in time and shift-start time can be ambiguous for overnight shifts. The current implementation uses a 24-hour adjustment for sufficiently negative time differences, but its correctness depends on the intended shift rules. | Test punches before and after midnight against explicitly defined overnight-shift expectations. | Added handling to adjust sufficiently negative time differences by 24 hours before applying the 10-minute grace period. Further boundary testing is needed to verify overnight-shift behavior. |
| 2 | `build_late_leaderboard_pipeline()` | Applying `$limit` after ranking can exclude employees who share the rank at the cutoff. | If `limit=2` and ranks are `1, 2, 2`, a simple `$limit: 2` returns only two employees and excludes one tied at rank 2. | Replaced `$limit` with a `$match` on `rank <= limit`, preserving all employees whose rank falls within the requested limit. |

## Reviewed but Not Considered Defects

- `late_leaderboard()` delegates aggregation construction to `build_late_leaderboard_pipeline()` and returns the month and items. No defect was identified in this endpoint wrapper during the tests performed.
- The leaderboard sorts by total late minutes in descending order and employee code in ascending order, making the display order deterministic when totals are equal.
- The 10-minute grace-period comparison uses `delta_seconds <= 600`, so a punch exactly 10 minutes after shift start is not considered late. This matches the intended strict-greater-than-10-minute rule.

## Testing Notes

- Python syntax was checked using `python -m py_compile app/main.py`.
- The API server started successfully.
- `GET /analytics/leaderboard/late?month=2026-10&department=Engineering&limit=10` returned HTTP 200.
- The test data returned employee `EMP9001` with 330 late minutes and rank 1.
- A dedicated test with multiple employees tied at the cutoff rank is still needed to verify tie handling end to end.
- Dedicated overnight-shift boundary tests are still needed to verify the behavior of `compute_late_minutes()`.

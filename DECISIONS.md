# DECISIONS.md

## 1. Indexes

I created a unique index on `emp_code` to prevent duplicate employees and a unique compound index on `(emp_code, date)` to prevent duplicate daily attendance records. I added indexes for department and employee sorting, employee join dates, attendance date sorting, status/date filtering, and finding an employee's latest punch-in. I would evaluate additional compound indexes using query patterns and explain execution statistics before adding them.

## 2. Punch-in race

Two identical punch-in requests can arrive concurrently. Both may initially find no existing record, so application-level checking alone is insufficient. The unique `(emp_code, date)` index ensures that only one attendance record can be inserted for an employee on a given date. The request that encounters a duplicate-key error should return HTTP 409 Conflict.

## 3. Leaderboard ties

The leaderboard uses MongoDB's `$rank` based on total late minutes. Employees with equal totals receive the same rank. Sorting by employee code makes the display order deterministic when totals are equal. Instead of applying `$limit` directly, the pipeline filters for `rank <= limit`. This retains all employees sharing the cutoff rank, although the number of returned employees can exceed the requested limit.

## 4. Department headcount

The department summary starts with employees who joined on or before the month's end, then looks up their attendance records. Preserving employees without matching attendance logs allows them to contribute to headcount even when they have no attendance records.

## 5. Scaling

For 100 times more data, I would benchmark realistic workloads, inspect explain execution statistics, and tune indexes and aggregation pipelines based on observed query patterns. I would also test concurrent requests, monitor query latency and memory usage, and evaluate whether frequently requested analytics need caching or pre-aggregation.

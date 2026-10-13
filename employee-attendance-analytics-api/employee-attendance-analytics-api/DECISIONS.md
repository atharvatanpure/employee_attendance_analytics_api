# Design decisions

1. **Indexes.** I create a unique `employees.emp_code` index and a unique compound `attendance_logs(emp_code, date)` index. The latter both enforces one attendance record per employee/day and makes punch-in race-safe. Department/employee and department/join-date indexes support employee lists and headcount lookups. Attendance date/employee and date/status/employee indexes support range filters, sorting, and monthly analytics. I would consider a separate late-minutes index only if profiling showed it helped leaderboard queries enough to justify its write cost.

2. **Punch-in race.** Both requests may pass the employee lookup, but the unique compound index allows only one insert for that `(emp_code, date)`. MongoDB rejects the other insert with a duplicate-key error, which the API translates to 409; it does not depend on a fragile check-then-insert sequence.

3. **Ties.** The leaderboard uses `$rank`, then filters ranks after ranking. If the cutoff rank is tied, every employee with that rank is returned; the next rank is skipped, following standard competition ranking.

4. **Headcount.** Department summary starts from employees who joined by month-end and uses a preserving lookup/unwind, so employees without logs still count. Employees who join after month-end are excluded.

5. **100× data.** I would review `explain` and production query metrics, tune compound indexes against real filters, add bounded date ranges and connection-pool monitoring, and consider pre-aggregated monthly summaries if live aggregation latency became too high.

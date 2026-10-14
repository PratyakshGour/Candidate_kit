# DECISIONS.md

1. **Indexes:**
We created:
- `employees.emp_code` (unique): Guarantees employee identity uniqueness and concurrency safety (`409`).
- `employees.department`: Accelerates department filtering and pipeline lookups via `IXSCAN`.
- `attendance_logs.(emp_code, date)` (unique): Enforces the natural key constraint and eliminates duplicate punch-in races.
- `attendance_logs.(date, emp_code)`: Backs `/attendance` listing sorted by `date` desc, `emp_code` asc, avoiding memory limits.
- `attendance_logs.status`: Optimizes status filtering.
*Rejected:* An index on `(punch_in, punch_out)`, because filtering occurs on calendar `date`, not timestamp instants, making it redundant.

2. **Punch-in race:**
Both simultaneous requests compute the IST attendance date and execute `insert_one`. The unique compound index on `(emp_code, date)` atomically commits the first write (201) and throws a `DuplicateKeyError` for the second, which our exception handler intercepts to return `409 Conflict`.

3. **Ties:**
The leaderboard pipeline applies `$setWindowFields` with `$rank` (standard competition ranking: 1, 2, 2, 4), followed by `rank <= limit`. Employees tied at the cutoff share the same rank and are both returned, ordered deterministically by `emp_code` ascending.

4. **Headcount:**
The pipeline queries `employees` where `joined_on <= last_day_of_month`, then `$lookup`s `attendance_logs` with `preserveNullAndEmptyArrays: true`. Headcount is computed via `$size: "$emp_set"` (`$addToSet: "$emp_code"`), ensuring employees with zero logs are accurately counted.

5. **One thing you would change for 100x data:**
Shard `attendance_logs` by `(emp_code, date)` and maintain pre-aggregated daily/monthly departmental roll-up collections to avoid repeated full-month log aggregations.



# DECISIONS.md

1. **Indexes.** `employees.emp_code` unique (stops duplicate employees). `attendance_logs (emp_code, date)` unique (one record per day, and fast per-employee lookups). `attendance_logs (date desc, emp_code)` serves the default list and every month query. `(status, date desc, emp_code)` serves the status filter. Employees get `(department, emp_code)`, `(department, joined_on)` and `(joined_on)` for filters and headcount. I rejected `(emp_code, punch_in)` for punch-out: the unique index already finds the record.
2. **Punch-in race.** Both requests try to insert. The unique index lets one succeed (201); the database rejects the other with a duplicate-key error, which I turn into 409. There is no check-then-write step.
3. **Ties.** Employees tied at the cutoff are all returned, so the list can be longer than `limit`. Ranks are 1, 2, 2, 4, and the limit applies to the rank.
4. **Headcount.** The summary starts from the `employees` collection and joins logs onto it, so someone with no logs still counts.
5. **Change at 100x data.** Keep pre-computed monthly totals per employee instead of scanning every log, and move the trend's employee join into the database.

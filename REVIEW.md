# REVIEW.md

Defects found in the starter `app/main.py`. All are fixed in the new `app/main.py`. The "How I'd notice it" column says how each one shows up, and `tests/` contains a test for most of them.

| # | Where | What is wrong | How I'd notice it | How I fixed it |
|---|---|---|---|---|
| 1 | `/health` | Always returns "ok". It never talks to MongoDB, so it says healthy even when the database is down. | Stop MongoDB, call `/health`. | It pings MongoDB and returns 503 if the ping fails. |
| 2 | `create_employee` | Check-then-insert race: two requests can both pass the `find_one` and both insert. No unique index. | 20 parallel creates of one `emp_code`. | Unique index on `emp_code`; insert directly and turn `DuplicateKeyError` into 409. |
| 3 | `create_employee` | `created_at` is naive local time and not returned as epoch ms. No validation of `emp_code`, email, shift format, `shift_start == shift_end` or `joined_on`. | Send a bad body such as `shift_start: "25:99"`. | Pydantic model with patterns and checks (bad input gives 422); `created_at` stored in UTC and returned as ms. |
| 4 | `list_employees` | `skip = page * page_size` skips the whole first page when `page=1`. | `GET /employees?page=1`. | `skip = (page - 1) * page_size`. |
| 5 | `list_employees` | `total` ignores the `department` filter. | Filtered request: `total` is the whole collection. | `count_documents` uses the same filter as the query. |
| 6 | `list_employees` | No sort order and no page bounds. | `page_size=1000`, or paging gives unstable order. | Sorted by `emp_code`, `page >= 1` and `page_size` 1 to 100, otherwise 422. |
| 7 | `punch_in` | Unknown employee: `emp` is `None`, so `emp["shift_start"]` raises `TypeError` and gives a 500. | Punch in with an unknown `emp_code`. | Look up the employee first and return 404. |
| 8 | `punch_in` | `fromtimestamp` uses the server's local time and returns a naive datetime. Epoch seconds, 0 and out-of-range values are accepted. Seconds are not truncated. | Send epoch seconds, or `punched_at: 0`. | `StrictInt` with bounds, conversion to UTC, seconds kept per the rules, bad values give 422. |
| 9 | `punch_in` | `date` comes from server time, not IST, and there is no overnight rule. | Punch at 01:00 IST on a 22:00-06:00 shift. | `attendance_date()` works in IST and applies the overnight rule. |
| 10 | `punch_in` | `status` accepts any string, including `ABSENT` and `LEAVE` and nonsense. | Send `status: "X"`. | Only `PRESENT`, `WFH` and `ON_DUTY` are allowed on punch-in; anything else gives 422. |
| 11 | `punch_in` | Check-then-insert race. | 20 parallel identical punch-ins. | Unique index on `(emp_code, date)`; one 201, the rest 409. |
| 12 | `punch_in` | The response exposes `id`, and datetimes are not epoch ms. | Look at the response. | Response built by a serializer: no `_id`/`id`, datetimes as ms. |
| 13 | `compute_late_minutes` | Truncates minutes before comparing (09:40:01 gives 0). Uses naive time and ignores overnight shifts. | 09:40:00 gives 0 but 09:40:01 should give 10. | Compares seconds (late only if more than 600 s), uses IST, handles overnight; the minutes counted from shift start are floored. |
| 14 | `compute_work_hours` | Float `round()` (banker-style, float noise) and no truncation. | 16182 s should give 4.50 and `half_day` false. | Integer half-up rounding in cents; `half_day` when rounded hours are below 4.50. |
| 15 | `compute_overtime` | No 30-minute minimum, ignores overnight shift ends, mixes aware and naive datetimes (`TypeError`). | Punch out 10 minutes after shift end. | Overtime counts only if it is at least 30 minutes, uses the real shift end (next day for overnight), everything timezone-aware. |
| 16 | `list_attendance` | Loads every document, sorts in Python, no tie-break, unvalidated paging and dates, leaks `_id`, no defaults for missing fields. | 100,000 records: slow and memory-heavy. | Filter, sort `(date desc, emp_code)`, `skip` and `limit` all in MongoDB; validated input; serializer fills defaults. |
| 17 | whole file | No indexes, MongoDB client is not timezone-aware, no 422 for `date_from > date_to`. | `explain` shows a COLLSCAN. | Indexes created at startup, `tz_aware=True`, and a 422 for a reversed range. |

## Looked at and not a defect

- `load_dotenv()` does not override variables already set in the environment, so the real environment still wins.
- The IST offset constant (+05:30) is correct.
- `201` on create is the right status code.
- Comparing ISO date strings (`YYYY-MM-DD`) with `$gte` and `$lte` is valid for ranges, because that format sorts correctly as text.

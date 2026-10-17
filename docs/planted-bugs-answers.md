# Planted bugs: answer key

Spoiler. Enabled only with `SANDBOX_BUGS=1` (`make run-bugs`). All three live in `app/reports.py`.

| # | Symptom | Where | Test that catches it |
|---|---|---|---|
| 1 | `summary.total_leads` excludes lost leads, so it no longer equals the sum of `by_status`, and `conversion_rate` is inflated | `summary()` | `test_summary_matches_database`, `test_by_source_rolls_up_to_summary` |
| 2 | By-source `conversion_rate` uses integer division (e.g. 18.75 becomes 18.0) | `by_source()` | `test_by_source_rolls_up_to_summary` |
| 3 | Every CSV export silently drops its last row | `to_csv()` | `test_csv_export_matches_json` |

Why they are realistic: #1 is a "filter applied in the wrong place" bug, #2 a type/rounding bug that only shows on non-round numbers, #3 an off-by-one that passes any test that only checks headers or the first rows.

Ideas for your own bug hunts: add a fourth bug (e.g. a timezone shift in `daily_ingestion`) and check whether your suite notices.

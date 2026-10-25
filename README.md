# garmin-data-handler

Backs up all Garmin Connect Personal API data to one SQLite file per person.
The first run downloads the full history. Later runs download only new days,
plus the last 7 days again (Garmin finalizes sleep and HRV later).

## Use

```shell
mkdir person/<name>
./run            # creates the person files, asks for the API key
# put the key from https://connect.garmin.com/app/settings/personalApiToken
# into person/<name>/api_key.txt
./run            # full backup the first time, incremental after that
```

Needs `uv`. Stopping a run (Ctrl+C) is safe: the next run continues.

## Files per person (`person/<name>/`, never committed)

| File | What |
|---|---|
| `api_key.txt` | Personal API key (mode 600) |
| `datasets.txt` | Data sets to back up; put `#` in front of a line to turn it off |
| `start_date.txt` | Optional: first day to back up (`yyyy-MM-dd`). Default: auto-detected |
| `garmin.sqlite` | The data |
| `sync.log` | Human-readable log of every download: OK with rows, or FAIL with reason |
| `token.json` | Cached bearer token |

## Database

- One table per data set (`steps_daily`, `sleep_daily`, `activities`, ...).
- Columns are the top-level Garmin fields, typed from the Swagger spec
  (`app/garmin_data_handler/openapi.json`). Nested values are JSON text.
- `raw_json` holds every item exactly as Garmin returned it.
- `_catalog` describes every table and column. Read it first.
- `sync_log` records every download. Dates with `status = 'ok'` are not
  downloaded again; failed ranges are retried on the next run.
- `activity_file.zip` holds the original activity file (FIT/GPX/TCX in a ZIP).

```sql
SELECT table_name, column_name, description FROM _catalog WHERE table_name = 'sleep_daily';
SELECT calendarDate, sleepTimeSeconds / 3600.0 FROM sleep_daily ORDER BY calendarDate DESC LIMIT 7;
SELECT calendarDate, json_extract(hrvSummary, '$.lastNightAvg') FROM hrv_detail;
```

## Tests

```shell
cd app && uv run python -m unittest discover -s tests
```

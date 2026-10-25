# garmin-data-handler

Backs up all Garmin Connect Personal API data to one SQLite file per person.
The first run downloads the full history. Later runs download only new days,
plus the last 7 days again (Garmin finalizes sleep and HRV later).

## Use

```shell
./run            # first run: asks for a person name, creates person/<name>/,
                 # asks for the API key (hidden input), then backs up
./run            # full backup the first time, incremental after that
```

The key comes from https://connect.garmin.com/app/settings/personalApiToken.
`./run` creates `person/` if it is missing. In an interactive terminal it also
asks for the key of any person whose `api_key.txt` is empty (empty answer =
skip; put the key into `api_key.txt` yourself later). Without a terminal
(cron) it asks nothing.

Or use the subcommands (`--person-root` goes before the subcommand, default `./person`):

```shell
cd app
uv run python -m garmin_data_handler new anna
uv run python -m garmin_data_handler edit anna --api-key <key> --datasets steps_daily,sleep_daily --start-date 2024-01-01
uv run python -m garmin_data_handler sync anna          # full the first time, then only what is missing
uv run python -m garmin_data_handler sync anna --full   # download everything again
uv run python -m garmin_data_handler --person-root /data/people sync anna
```

`--datasets` is comma-separated; the others are turned off. `--api-key` on the command line lands in your shell history, so for a real key edit `api_key.txt` instead.
`./run` (or `python -m garmin_data_handler <person root>`) still syncs every person folder.

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

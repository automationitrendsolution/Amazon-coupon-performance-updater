# Amazon Coupon Performance Updater

Keeps the Power BI database up to date with Amazon **coupon performance** data (Amazon.com). It runs on a
schedule and needs no manual work once it is set up.

| Data | Source | MySQL table (`powerbidb`) | Schedule |
| --- | --- | --- | --- |
| **Coupon performance**: per coupon and ASIN, the coupon's settings, budget, clips, redemptions, discount and sales | Amazon **SP-API** report `GET_COUPON_PERFORMANCE_REPORT` | `api_coupon_data` | Every 5 hours (Linux cron) |

Each run replaces the coupons in the report. **Other coupons are never touched**, and running twice never
writes duplicates.

---

## Contents

1. [How it works](#how-it-works)
2. [Requirements](#requirements)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Usage](#usage)
6. [Scheduling](#scheduling)
7. [How data is written](#how-data-is-written)
8. [Monitoring and troubleshooting](#monitoring-and-troubleshooting)
9. [Security](#security)
10. [Project structure](#project-structure)
11. [Development](#development)

---

## How it works

```
Window: coupons whose campaign starts from 30 days ago to 10 days ahead (UTC)
      │
      ▼
Amazon SP-API ── 1 report (GET_COUPON_PERFORMANCE_REPORT); a run waits up to 2 h,
      │          a later run the same day resumes the same report
      ▼
Validate ─────── "coupons" list, every coupon has a couponId, right marketplace, no duplicates
      │
      ▼
Flatten ──────── one row per coupon and ASIN (the ASIN's fields + the coupon's fields)
      │
      ▼
Write ────────── one transaction: delete the report's coupons, insert their rows, check the count
```

- The window is the same as the old `amazon_coupon_performance.py` script: 40 days of campaign start dates,
  ending 10 days ahead, so running, recently ended and upcoming coupons are all refreshed every day.
- A failed or malformed report is **never** written. An empty report writes nothing.

---

## Requirements

| | |
| --- | --- |
| Python | 3.13, managed with [uv](https://docs.astral.sh/uv/) |
| Database | MySQL `powerbidb` with the user allowed to `SELECT`, `INSERT`, `DELETE` on `api_coupon_data` |
| Amazon SP-API | A Login with Amazon app with a role that covers coupon reports, self-authorised for the seller account |

---

## Installation

### Linux server (recommended for the schedule)

```bash
git clone <repository-url> /data/cron/amazon-coupon-performance-updater
cd /data/cron/amazon-coupon-performance-updater

cp .env.example .env              # then fill in the values (see Configuration)
chmod 600 .env
mkdir -p logs

uv sync                           # Python 3.13 and the packages
```

Check the setup:

```bash
uv run python run.py --inspect    # database: table, columns, coupons (read-only)
uv run python run.py --daily      # dry run: fetch today's report, show what would be written
```

### Windows (development, or running the schedule on a PC)

```powershell
cd C:\Users\Admin\Desktop\amazon-coupon-performance-updater
uv sync                                              # creates .venv with Python 3.13 and the packages
# without uv: python -m venv .venv
#             .venv\Scripts\python.exe -m pip install pymysql python-dotenv requests pytest
copy .env.example .env                               # then fill in the values
.venv\Scripts\python.exe -B run.py --inspect
.venv\Scripts\python.exe -B run.py --daily           # dry run
```

On Windows, run commands as `.venv\Scripts\python.exe -B run.py ...`; on Linux as `uv run python run.py ...`.

---

## Configuration

All settings live in `.env` (copied from `.env.example`). `.env` holds passwords and tokens: keep it private
(`chmod 600`) and **never commit it**. Values still containing `replace-me` count as not set.

### Amazon SP-API

| Variable | Meaning |
| --- | --- |
| `SPAPI_LWA_CLIENT_ID`, `SPAPI_LWA_CLIENT_SECRET` | The app's Login with Amazon credentials |
| `SPAPI_LWA_REFRESH_TOKEN` | Refresh token from self-authorising the app |
| `SPAPI_MARKETPLACE_ID` | `ATVPDKIKX0DER` (Amazon.com) |
| `SPAPI_ENDPOINT` | Optional; derived from the marketplace |

AWS access keys, the role ARN and SigV4 signing are **not** needed (SP-API is LWA-only since October 2023).

### Database

| Variable | Default | Meaning |
| --- | --- | --- |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD` | –, 3306 | MySQL server (`POWERBI_DB_*` names work too) |
| `DB_NAME` | `powerbidb` | Database |
| `COUPON_TABLE` | `api_coupon_data` | Table |

### Behaviour

| Variable | Default | Meaning |
| --- | --- | --- |
| `COUPON_LOOKBACK_DAYS` | 30 | `--daily`: campaigns starting from this many days ago |
| `COUPON_LOOKAHEAD_DAYS` | 10 | `--daily`: … up to this many days ahead |
| `COUPON_POLL_INTERVAL_SECONDS` | 60 | How often to ask Amazon whether the report is ready |
| `COUPON_POLL_TIMEOUT_SECONDS` | 7200 | How long one run waits (the next run resumes) |
| `COUPON_MAX_RETRIES` | 6 | Retries on throttling and server errors |
| `LOG_LEVEL` | `INFO` | |
| `COUPON_DATA_DIR`, `COUPON_LOG_DIR` | `data/`, `logs/` | Optional other locations |

---

## Usage

```bash
python run.py --inspect                                 # read-only check of the table
python run.py --daily                                   # today's window, dry run: fetch, validate, preview
python run.py --daily --apply                           # write it to MySQL (cron)
python run.py --window 2026-09-01 2026-10-01 --dry-run  # coupons starting in that window, preview
python run.py --window 2026-09-01 2026-10-01 --apply    # write them
python run.py --daily --apply --refetch                 # ask Amazon again (ignore today's download)
python run.py --help                                    # every option
```

Every fetch saves the raw report to `data/reports/coupons_<from>_<to>_<reportId>.json` and its rows to
`data/reports/coupons_<from>_<to>_records.csv` (what `api_coupon_data.csv` used to be).

---

## Scheduling

Run the job in **one place only** (the server **or** a Windows PC), never both.

### Linux cron (recommended)

Add to `crontab -e` (full example in [`deploy/crontab.example`](deploy/crontab.example)). Use the path that
`which uv` shows, and create `logs/` first (`mkdir -p logs`).

```cron
MAILTO=""
# Every 5 hours: 00:00, 05:00, 10:00, 15:00, 20:00 server time (cron restarts at midnight, so the last gap is 4 h)
0 */5 * * * cd /data/cron/amazon-coupon-performance-updater && /root/.local/bin/uv run --no-sync python run.py --daily --apply --refetch --quiet >> logs/cron-output.log 2>&1
```

`--refetch` is required here: without it, a run finds the day already written and stops at once, so only the
first run of each (UTC) day would update the table. With it, every run requests a fresh report and replaces
the coupons again. A run waits at most 2 h for Amazon, well inside the 5 h gap; overlapping runs exit 11.

### Windows Task Scheduler (alternative)

```powershell
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1            # daily 09:00 (+ 15:00 backup)
powershell -ExecutionPolicy Bypass -File deploy\install_task.ps1 -Remove    # unregister
```

The PC must stay on (sleep: never) with the user signed in to Windows; a locked screen is fine. A run missed
while the PC was off starts as soon as it is on again.

### Typical day

Every 5 hours a fresh report for today's window is requested and written to `api_coupon_data` as soon as
Amazon delivers. A run that fails or times out is simply retried with a new report 5 hours later.

---

## How data is written

- One row per **coupon and ASIN**. Columns are named like the report's fields: the ASIN's fields first
  (`asin`, …), then the coupon's: `couponId`, `merchantId`, `marketplaceId`, `currencyCode`, `name`,
  `websiteMessage`, `startDateTime`, `endDateTime`, `discountType`, `discountAmount`, `totalDiscount`, `clips`,
  `redemptions`, `budget`, `budgetSpent`, `budgetRemaining`, `budgetPercentageUsed`, `sales`.
- In **one transaction**: the rows of every coupon in the report are deleted, the report's rows inserted, and the
  count checked. Anything unexpected rolls it all back (exit 3, nothing changed).
- Coupons not in the report, and coupons the report has **without ASINs**, keep their rows.
- A report field the table has no column for is left out and named in the log (add the column to store it).
  A field both the ASIN and the coupon have keeps the coupon's value; the ASIN's is `asin.<field>`.

---

## Monitoring and troubleshooting

| Log | Content |
| --- | --- |
| `logs/cron.log` | One line per run: time, exit code, command, summary (report ID, rows deleted / inserted) |
| `logs/coupon_updater.log` | Full details: report status, warnings (rotated daily, 30 days kept) |
| `logs/cron-output.log` | Console output of cron runs |

### Exit codes

| Code | Meaning | What to do |
| --- | --- | --- |
| 0 | Success | – |
| 1 | Unexpected error | Read `logs/coupon_updater.log` |
| 2 | A setting is missing or wrong | Read the message; fix `.env` |
| 3 | MySQL unreachable, or a write was rolled back (**nothing changed**) | Check the `DB_*` settings and the message |
| 5 | `--window` is not valid | Fix the dates (`FROM` before `TO`, at most 366 days) |
| 6 | SP-API request failed, or the report was not ready in time | Timeout: the next run resumes. HTTP 403: check the app's roles |
| 7 | Report for another marketplace, or not shaped as expected | Nothing written; the raw JSON is in `data/reports/` |
| 11 | Another run was still going | Nothing; the next run continues |

### Common situations

| Symptom | Cause and fix |
| --- | --- |
| `FATAL` report about the date range | Amazon limits the campaign start window; lower `COUPON_LOOKBACK_DAYS` / `COUPON_LOOKAHEAD_DAYS` |
| New coupons not visible in Power BI | The data is in MySQL; refresh the Power BI dataset |
| Start today over | Delete `data/state/coupons_<YYYY-MM-DD>.json` or use `--refetch` |

---

## Security

- **Secrets** live only in `.env` (never committed); nothing is hard-coded in the code.
- The SP-API part uses Amazon's official API.

---

## Project structure

| Path | Purpose |
| --- | --- |
| `run.py` | Command-line entry point (`--help` lists every option) |
| `app/spapi.py` | SP-API: access token, report, polling, download, retries, resume |
| `app/report.py` | Validates the report JSON, one row per coupon and ASIN |
| `app/db.py` | MySQL: inspection, plan, the transactional write for `api_coupon_data` |
| `app/schedule.py` | Campaign start window, run lock |
| `app/config.py`, `app/exceptions.py`, `app/logging_config.py` | Settings, exit codes, logging |
| `deploy/crontab.example` | Cron lines for the Linux server |
| `deploy/install_task.ps1` | Windows Task Scheduler setup |
| `data/` (not in git) | Downloaded reports, rows as CSV, run state |
| `logs/` (not in git) | Run history and logs |
| `tests/` | Automated tests (no Amazon or real database needed) |

---

## Development

```bash
uv sync                      # installs the packages, including the test tools
uv run python -B -m pytest   # runs the whole test suite (Amazon and MySQL are faked)
```

The tests cover the window, report parsing, the SP-API flow (resume, failed and slow reports), the MySQL write
(replace, no duplicates, rollback) and the command line.
#   A m a z o n - c o u p o n - p e r f o r m a n c e - u p d a t e r  
 
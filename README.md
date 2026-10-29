# Amazon Coupon Performance Updater

Automatically loads Amazon.com **coupon performance** data into the Power BI database, so coupon dashboards
stay current without any manual exports.

| | |
| --- | --- |
| **Source** | Amazon Selling Partner API: report `GET_COUPON_PERFORMANCE_REPORT` |
| **Destination** | MySQL `powerbidb.api_coupon_data` |
| **Schedule** | Every 5 hours (Linux cron) |
| **Coverage** | Coupons whose campaign starts between 30 days ago and 10 days ahead |
| **Runtime** | Python 3.13, managed with [uv](https://docs.astral.sh/uv/) |

**Safe by design:** each run replaces only the coupons in the latest report, inside a single database
transaction. Other coupons are never touched, re-running never creates duplicates, and a failed run changes
nothing.

---

## Contents

- [How it works](#how-it-works)
- [Quick start](#quick-start)
- [Deploying to the cron server](#deploying-to-the-cron-server)
- [Configuration](#configuration)
- [Commands](#commands)
- [The data in MySQL](#the-data-in-mysql)
- [Monitoring](#monitoring)
- [Troubleshooting](#troubleshooting)
- [Updating the deployment](#updating-the-deployment)
- [Project structure](#project-structure)
- [Development](#development)
- [Security](#security)

---

## How it works

```
 ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐    ┌──────────────┐
 │ 1. Window    │ ─▶ │ 2. Request   │ ─▶ │ 3. Validate  │ ─▶ │ 4. Flatten   │ ─▶ │ 5. Write     │
 │ -30 … +10    │    │ SP-API       │    │ marketplace, │    │ one row per  │    │ one MySQL    │
 │ days (UTC)   │    │ report       │    │ IDs, shape   │    │ coupon+ASIN  │    │ transaction  │
 └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘    └──────────────┘
```

1. **Window**: selects coupons whose campaign starts from 30 days ago to 10 days ahead, so running, recently
   ended and upcoming coupons are all refreshed.
2. **Request**: asks Amazon for the coupon performance report and waits until it is ready (up to 2 hours).
3. **Validate**: rejects reports for the wrong marketplace, coupons without an ID, and duplicates.
   A malformed report is never written.
4. **Flatten**: produces one row per coupon and ASIN.
5. **Write**: deletes the report's coupons, inserts the new rows and verifies the count, all in one transaction.

---

## Quick start

For running locally on Windows (testing and one-off loads):

```powershell
cd C:\Users\Admin\Desktop\amazon-coupon-performance-updater
uv sync                                          # create .venv with Python 3.13 and dependencies
copy .env.example .env                           # then fill in your credentials (see Configuration)

.venv\Scripts\python.exe -B run.py --inspect     # 1. check the database connection (read-only)
.venv\Scripts\python.exe -B run.py --daily       # 2. dry run: fetch and preview, writes nothing
.venv\Scripts\python.exe -B run.py               # 3. fetch a fresh report and write it to MySQL
```

> **Tip:** a dry run saves the rows it fetched to `data/reports/coupons_<from>_<to>_records.csv`, so you can
> review them in Excel before writing.

---

## Deploying to the cron server

> **Run the job in one place only.** If it is scheduled on the server, do not also schedule it on a PC.

### 1. Install

```bash
git clone <repository-url> /data/cron/amazon-coupon-performance-updater
cd /data/cron/amazon-coupon-performance-updater
mkdir -p logs
uv sync
```

If `uv` is not installed: `curl -LsSf https://astral.sh/uv/install.sh | sh`

### 2. Add the credentials

Copy your working `.env` to the server. **Never commit it to git.**

```powershell
scp .env <user>@<server>:/data/cron/amazon-coupon-performance-updater/.env
```

```bash
chmod 600 /data/cron/amazon-coupon-performance-updater/.env
```

### 3. Test

```bash
uv run python run.py --inspect    # should list the api_coupon_data table and its row count
uv run python run.py --daily      # should end with "dry run: nothing written"
```

If either fails, check that the server can reach MySQL (port 3306) and `sellingpartnerapi-na.amazon.com` (HTTPS).

### 4. Schedule

Find the full path to `uv` with `which uv`, then add these lines with `crontab -e`:

```cron
MAILTO=""

# Coupon performance every 5 hours
0 */5 * * * cd /data/cron/amazon-coupon-performance-updater && /root/.local/bin/uv run --no-sync python run.py >> logs/cron-output.log 2>&1

# Delete downloaded reports older than 30 days
30 1 * * * find /data/cron/amazon-coupon-performance-updater/data/reports -name 'coupons_*' -mtime +30 -delete
```

| Detail | |
| --- | --- |
| Run times | 00:00, 05:00, 10:00, 15:00, 20:00 (server time). Cron restarts at midnight, so the last gap is 4 hours. |
| Command | Plain `run.py` is the scheduled job: every run fetches a fresh report and writes it. Cron provides the 5-hour timing. |
| Overlaps | A run waits at most 2 hours for Amazon. If a run is still going when the next starts, the new one exits safely (code 11). |

Confirm with `crontab -l`. A ready-to-copy version is in [`deploy/crontab.example`](deploy/crontab.example).

### 5. Verify after the first run

```bash
tail -3 logs/cron.log       # expect: exit=0 ... deleted=… inserted=…
```

---

## Configuration

All settings are read from `.env` in the project folder. Start from the template:
`cp .env.example .env`. Values that still contain `replace-me` are treated as not set.

### Amazon SP-API (required)

| Variable | Description |
| --- | --- |
| `SPAPI_LWA_CLIENT_ID` | Login with Amazon client ID of the SP-API app |
| `SPAPI_LWA_CLIENT_SECRET` | Login with Amazon client secret |
| `SPAPI_LWA_REFRESH_TOKEN` | Refresh token from self-authorising the app (starts with `Atzr\|`) |
| `SPAPI_MARKETPLACE_ID` | `ATVPDKIKX0DER` for Amazon.com |
| `SPAPI_ENDPOINT` | *Optional.* Derived automatically from the marketplace |

The app needs a role that covers coupon reports. AWS access keys, role ARNs and request signing are **not**
needed; SP-API has used Login with Amazon only since October 2023.

### MySQL (required)

| Variable | Default | Description |
| --- | --- | --- |
| `DB_HOST` | | MySQL server address |
| `DB_PORT` | `3306` | MySQL port |
| `DB_USER` / `DB_PASSWORD` | | Account with `SELECT`, `INSERT`, `DELETE` on the table |
| `DB_NAME` | `powerbidb` | Database |
| `COUPON_TABLE` | `api_coupon_data` | Table |

### Behaviour (optional)

| Variable | Default | Description |
| --- | --- | --- |
| `COUPON_LOOKBACK_DAYS` | `30` | Include campaigns that started up to this many days ago |
| `COUPON_LOOKAHEAD_DAYS` | `10` | Include campaigns starting up to this many days ahead |
| `COUPON_POLL_INTERVAL_SECONDS` | `60` | How often to check whether Amazon's report is ready |
| `COUPON_POLL_TIMEOUT_SECONDS` | `7200` | Maximum wait for one report (2 hours) |
| `COUPON_MAX_RETRIES` | `6` | Retries on Amazon throttling or server errors |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `COUPON_DATA_DIR` / `COUPON_LOG_DIR` | `data/` / `logs/` | Alternative storage locations |

---

## Commands

On Linux use `uv run python run.py …`; on Windows use `.venv\Scripts\python.exe -B run.py …`.

| Command | What it does | Writes to MySQL? |
| --- | --- | :---: |
| `run.py` | **The scheduled job.** Fetches a fresh report for the current window and writes it | **Yes** |
| `run.py --dry-run` | Same fetch, but only previews the change | No |
| `run.py --inspect` | Shows the table's columns, row count and coupon count | No |
| `run.py --daily` | Fetches the current window and previews the change (reuses today's report if downloaded) | No |
| `run.py --daily --apply` | Writes the current window once per day; later runs that day stop at once | **Yes** |
| `run.py --window 2026-09-01 2026-10-01 --dry-run` | Previews coupons starting in a custom date range | No |
| `run.py --window 2026-09-01 2026-10-01 --apply` | Writes coupons starting in a custom date range | **Yes** |
| `run.py --help` | Lists every option | – |

A preview prints exactly what a write would do, for example:

```
report: 61 coupons, 1335 coupon/ASIN rows, 554 ASINs
plan: replace 59 coupon(s) already in the table (1328 rows) and add the others: 1328 rows deleted, 1335 inserted
dry run: nothing written to powerbidb.api_coupon_data
```

---

## The data in MySQL

Each row is one **coupon + ASIN** combination in `powerbidb.api_coupon_data`:

| Group | Columns |
| --- | --- |
| Product | `asin` |
| Coupon | `couponId`, `merchantId`, `marketplaceId`, `currencyCode`, `name`, `websiteMessage` |
| Campaign | `startDateTime`, `endDateTime`, `discountType`, `discountAmount` |
| Budget | `budget`, `budgetSpent`, `budgetRemaining`, `budgetPercentageUsed` |
| Performance | `clips`, `redemptions`, `totalDiscount`, `sales` |

**How updates work**

- Every coupon in the report is replaced as a whole: its old rows are deleted and the new rows inserted.
- Coupons that are not in the report keep their existing rows.
- If anything goes wrong, the whole write is rolled back and the table is left exactly as it was.

> **Power BI note:** `clips`, `redemptions`, `sales`, `totalDiscount` and the budget columns are
> **coupon-level** totals, repeated on every ASIN row of that coupon. Do not `SUM` them across rows; take one
> value per `couponId` (for example `MAX`), or the totals will be multiplied by the number of ASINs.

> **Precision note:** `totalDiscount`, `budgetSpent`, `budgetRemaining` and `budgetPercentageUsed` are `int`
> columns, so decimals from Amazon are rounded (3.65 → 4). Change them to `DECIMAL(12,2)` if exact values
> are needed.

---

## Monitoring

| Log file | Contents |
| --- | --- |
| `logs/cron.log` | **Start here.** One line per run: time, exit code and result |
| `logs/coupon_updater.log` | Full detail of every run (rotated daily, 30 days kept) |
| `logs/cron-output.log` | Warnings and errors printed by scheduled runs |

A healthy `logs/cron.log` looks like this:

```
2026-10-29 15:00:02 exit=0 (scheduled job) | 2026-09-29..2026-11-08 report=2001861020755 coupons=61 rows=1335 deleted=1328 inserted=1335
```

### Exit codes

| Code | Meaning | Action |
| :---: | --- | --- |
| `0` | Success | None |
| `1` | Unexpected error | See `logs/coupon_updater.log` |
| `2` | Setting missing or invalid | Fix `.env`; the message names the variable |
| `3` | Database error; the write was rolled back, **nothing changed** | Check the `DB_*` settings and that MySQL is reachable |
| `5` | Invalid `--window` dates | `FROM` must be before `TO`, at most 366 days apart |
| `6` | Amazon request failed or report not ready in time | Usually temporary; the next run retries. For HTTP 403, check the app's roles |
| `7` | Unexpected report (wrong marketplace or shape) | Nothing written; the raw report is in `data/reports/` |
| `11` | Previous run still in progress | None; this is normal if Amazon is slow |

---

## Troubleshooting

| Problem | Solution |
| --- | --- |
| `Missing SP-API settings` (exit 2) | `.env` must use the exact names `SPAPI_LWA_CLIENT_ID`, `SPAPI_LWA_CLIENT_SECRET`, `SPAPI_LWA_REFRESH_TOKEN`, `SPAPI_MARKETPLACE_ID` |
| `Unknown marketplace` (exit 2) | Write the marketplace ID as plain text: `SPAPI_MARKETPLACE_ID=ATVPDKIKX0DER`, not as a list |
| HTTP 403 from Amazon (exit 6) | The SP-API app lacks the role for coupon reports, or the refresh token belongs to another app |
| `MySQL connection failed` (exit 3) | Check `DB_HOST`/`DB_PORT` and that the server's IP is allowed through the MySQL firewall |
| Amazon rejects the date range | Reduce `COUPON_LOOKBACK_DAYS` or `COUPON_LOOKAHEAD_DAYS` |
| Cron never runs | Check `crontab -l`, use the full path to `uv`, and make sure `logs/` exists |
| Only one update per day | The cron line uses `--daily --apply`; use plain `run.py` instead |
| New data not visible in Power BI | The data is in MySQL; refresh the Power BI dataset |

---

## Updating the deployment

After pushing changes to git, on the server:

```bash
cd /data/cron/amazon-coupon-performance-updater
git pull
uv sync          # needed because cron runs with --no-sync
uv run python run.py --daily    # optional dry run to confirm
```

The cron entry does not need to change.

---

## Project structure

```
amazon-coupon-performance-updater/
├── run.py                  Command-line entry point
├── app/
│   ├── spapi.py            Amazon SP-API: authentication, report request, polling, download, retries
│   ├── report.py           Report validation and flattening to coupon/ASIN rows
│   ├── db.py               MySQL: inspection, preview and transactional write
│   ├── schedule.py         Date window and single-run lock
│   ├── config.py           Settings from .env
│   ├── exceptions.py       Error types and exit codes
│   └── logging_config.py   Log files
├── deploy/
│   ├── crontab.example     Cron lines for the Linux server
│   └── install_task.ps1    Windows Task Scheduler alternative (daily 09:00 / 15:00)
├── tests/                  Automated tests (Amazon and MySQL are simulated)
├── data/                   Downloaded reports and run state (not in git)
├── logs/                   Log files (not in git)
├── .env.example            Configuration template
└── pyproject.toml          Python version and dependencies
```

---

## Development

```bash
uv sync                          # install dependencies, including test tools
uv run python -B -m pytest       # run the test suite
```

The tests need no Amazon account or database. They cover the date window, report validation, the SP-API flow
(including slow and failed reports), the MySQL write (replace, no duplicates, rollback) and the command line.

---

## Security

- Credentials live only in `.env`, which is git-ignored. Never put real values in `.env.example`.
- On the server, restrict `.env` with `chmod 600`.
- Downloaded reports in `data/` are created readable by the owner only.
- `--inspect` and dry runs open the database in read-only mode, so they cannot change data.
- Recommended: give the cron job a dedicated MySQL user limited to `SELECT`, `INSERT` and `DELETE` on
  `api_coupon_data`.

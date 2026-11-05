# PiezaLink

Python CLI and interactive terminal application for Rovira’s catalogue synchronization.
`import <feed>` loads a complete catalogue into Postgres. `sync` and `status`
will be added separately. The menu offers Import catalogue and Exit.

## Install and run

Requires Python 3.11 or newer. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ./app
piezalink           # Interactive menu with the existing ASCII banner
piezalink --help    # CLI help, without opening the menu
```

After installing dependencies, direct execution also works:

```sh
cd app/piezalink
python __main__.py
python __main__.py --help
```

From `app/`, `python -m piezalink` also opens the menu. Arguments select CLI
execution; unknown commands/options report an error and exit with code 2.
Use `0` (Exit), Ctrl+C, or Ctrl+D to leave the menu cleanly.

## Structure and extension

- `piezalink/__main__.py`: shared entry point for direct, module, and installed
  execution.
- `piezalink/cli/commands.py`: argument parsing and future command registration.
- `piezalink/cli/menu.py`: banner, menu loop, and reusable submenus.
- `piezalink/core/feed_import.py`: CSV validation and shared import service.
- `piezalink/core/rovira_db.py`: transactions, staging, and bulk reconciliation.
- `piezalink/core/schema.sql`: tables, migrations, and timestamp/constraint triggers.
- `piezalink/src/logo.txt`: packaged ASCII banner.

Future CLI handlers and menu actions must call the same services in `core/`.
Register CLI commands in `COMMANDS` with argparse subparsers to provide
`<command> --help` and option help. Add menu entries using `MenuItem`; a `Menu`
can itself be an action to open a submenu. Submenus offer Back, and completing
an operation returns to the main menu.

## Docker

From the repository root:

```sh
docker build -t piezalink -f app/Dockerfile .
docker run --rm -it piezalink          # Interactive menu
docker run --rm piezalink --help      # CLI help
```

Dependencies are declared in `piezalink/requirements.txt`, which is also read by
`setup.py`. Docker installs them first with `pip install -r`, allowing dependency
layers to be reused when application code changes. Rebuild the image after edits.

The image installs the package and uses `ENTRYPOINT ["piezalink"]`, forwarding
container arguments to the CLI. Allocate a terminal with `-it` for the menu.
The existing `portal-sim/` image remains a separate marketplace simulator.

The build context is the repository root, so the image also includes
`data/feed_dia*.csv` at `/app/data/`. For example, from a container shell:

```sh
piezalink import data/feed_dia1.csv
piezalink import data/feed_dia2.csv
```

These commands use the regular validation rules; the supplied feeds contain
invalid records and will be rejected until corrected in a separate copy.
The image only bundles the CSVs; building or starting it does not import data.


## Database interface

Install the package to include Psycopg and python-dotenv. Edit the repository's
`.env` to point at your running PostgreSQL server (or copy `.env.example` for a
fresh checkout):

```dotenv
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/rovira
```

Configuration priority is an explicit `init_db(dsn)` argument, then the process
`DATABASE_URL`, then the nearest `.env` found from the working directory upward.
Existing connections keep their configuration; close the connection before changing
it. `.env` is excluded from Git and Docker build contexts; `.env.example` is the
shareable template. Run cron from the project directory or supply `DATABASE_URL`
in the cron environment.

`init_db()` creates the database if missing and applies the packaged `schema.sql`.
Database creation requires `CREATEDB`; existing databases need schema creation
permissions. Accounts are registered explicitly; no CSV is imported automatically.

```python
from piezalink.core.rovira_db import init_db

db = init_db()
db.upsert_account(1, "your-token")
entry_id = db.create_listing("0044000602", "Renault light", "6.00", [1])
db.update_price(entry_id, "7.50")
db.update_listing_id(entry_id, 1, "L123")
item = db.get_listing(entry_id)
```

Update/remove methods use the internal catalogue ID, including for manual entries.
`product_id` remains a supplied unique string; only manual entries may omit it.
Prices use `Decimal`/`NUMERIC(7,2)`. Active entries require accounts; sold entries
may have none. Updating memberships preserves portal IDs for retained accounts.
Removing a local entry makes no marketplace calls. Removed account memberships
with portal IDs are retained as `desired=false` until sync retires them.
`get_listing()` exposes desired memberships in `accounts` and retained ones in
`pending_removals`. Clearing or retiring those references is deferred to sync.

The wrapper is shared within a process. Each operation runs in a locked transaction;
use `with db.transaction():` to group multiple method calls atomically. Separate
processes have separate connections. `db.close()` releases the connection at shutdown.
The import command uses this interface; individual CRUD methods remain Python APIs.

Run integration tests against a **dedicated disposable database** (the tests clear
the catalogue/account tables, including a legacy-schema migration test):

```sh
DATABASE_URL='postgresql://postgres:postgres@localhost:5432/rovira_test' \
    python -m unittest discover -s tests -v
```

## Import a complete feed

Set `DATABASE_URL` and register accounts with `upsert_account()` before importing.
To register the supplied accounts from the repository root, using the installed package:

```python
import csv
from piezalink.core.rovira_db import init_db

with open("accounts.csv", encoding="utf-8", newline="") as stream:
    db = init_db()
    with db.transaction():
        for account in csv.DictReader(stream):
            db.upsert_account(int(account["account_id"]), account["token"])
```

```sh
piezalink import /path/to/catalogue.csv
piezalink import --help
piezalink import /path/to/catalogue.csv --allow-large-removals
```

Or select **Import catalogue** in the interactive menu and enter the CSV path.
The menu returns after success, errors, or cancelling the removal override.
CLI success returns 0, import failure returns 1, usage errors return 2, and
Ctrl+C during a CLI import returns 130.

The CSV must have exactly `product_id,title,price,store_id` headers (any order).
UTF-8 BOM, CRLF, quoted fields, and surrounding value whitespace are supported.
IDs remain strings; prices require a decimal point if fractional and at most two
fractional digits. Titles must contain 1–80 characters. Separate known account
IDs with `|`; order and repeated account IDs do not matter. Duplicate product
IDs, missing fields, unknown accounts, invalid values, and manual-entry collisions
reject the whole feed. Diagnostics identify CSV records, with bounded error output.

The supplied day-one and day-two feeds intentionally fail strict validation,
including overlong titles on day one. Correct a separate copy before importing;
the importer never truncates titles or silently skips invalid records.

The service streams through `COPY` into temporary tables, then applies set-based
SQL in one transaction. New products become `unlisted`; changes to title, price,
or desired accounts become `modified`. Reappearing sold products also become
`modified`. Absent listed/modified products become `sold`; absent unlisted
products are deleted only if they have no remote listing IDs. Already-sold absent
products and manual entries remain unchanged. No Piezalia API calls are made.
The summary's unchanged count includes retained sold products absent from the feed.

Empty feeds always fail. If more than 20% of active nonmanual products disappear,
the import fails unless `--allow-large-removals` is supplied. That flag bypasses
only the percentage check, not validation. This is a heuristic: a syntactically
valid incomplete export cannot always be detected. Without feed version metadata,
accepted imports apply in execution order, even if an older feed is submitted later.

Imports use a transaction-scoped advisory lock; a competing import fails promptly.
Reconciliation temporarily blocks catalogue/account writes while allowing ordinary
reads. Errors and interruption before commit roll back the complete catalogue
change. Newly created database/schema objects can remain after a failed first import.
Schema migration is automatic and preserves existing data. Historical rows receive
the migration time for `created_at`/`modified_at`; later imports change timestamps
only when catalogue data, status, or desired accounts change. Schema version 1
marks this migration; future migrations must advance that version and its check.
CSV parsing uses Python's standard library. `.env` configuration uses python-dotenv,
listed in the shared requirements file.

Docker declares a development `DATABASE_URL` default targeting host `postgres`.
Use `--env-file .env` or `-e DATABASE_URL=...` to override it. Before passing `.env`
to Docker, set its database hostname to a name reachable from the container
(e.g. `your-postgres`, rather than `localhost`, on a shared Docker network).
Mount the feed directory when importing:

```sh
docker run --rm --network your-network \
    --env-file .env \
    -v "$PWD/data:/feeds:ro" piezalink import /feeds/your-valid-feed.csv
```

## Decisions

### CLI and interactive menu

We want both interfaces because the statement said clearly the python script will
be executed periodically using a `cron`. Therefore it's more convenient to set up
the cronjob like `* 2 * * * piezalink import <feed>`, look at the [Source consulted](#source-consulted).

In the other hand, we want a way of using the application without knowing all the
command (here are little but it might increase in the future), adding a `man` page
or even having a `--help` option.

This last one could be interesting, so I'd keep it anyway.

### Packaging and Docker entry point

<!-- Simply reproducibility, this way I know th -->

## Source consulted

- [The easy (and nice) way to do CLI apps in Python](https://trstringer.com/easy-and-nice-python-cli/)
- [Programar tareas con cron y crontab en Linux](https://www.stackscale.com/es/blog/cron-crontab-linux/)
- [The most popular PostgreSQL adapter for Python](https://www.psycopg.org/)

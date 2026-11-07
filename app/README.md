# PiezaLink

Python CLI and interactive terminal application for Rovira’s catalogue synchronization.
`import <feed>` loads a complete catalogue into Postgres; `sync` reconciles it
with Piezalia; `status <product_id>` shows the database record and synchronization
state for each account. The menu offers Import catalogue, Sync catalogue,
Product status, and Exit.

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
- `piezalink/core/sync.py`: bounded sync orchestration and revision acknowledgements.
- `piezalink/core/sync_db.py`: durable runs, operations, and per-account progress.
- `piezalink/core/portal.py`: reusable HTTPX client, discovery, and bounded retries.
- `piezalink/src/logo.txt`: packaged ASCII banner.

Future CLI handlers and menu actions must call the same services in `core/`.
Register CLI commands in `COMMANDS` with argparse subparsers to provide
`<command> --help` and option help. Add menu entries using `MenuItem`; a `Menu`
can itself be an action to open a submenu. Submenus offer Back, and completing
an operation returns to the main menu.

## Docker

The root `Dockerfile` and `compose.yaml` manage the app and persistent database.
From the repository root:

```sh
docker compose up -d --build
docker compose exec piezalink bash               # Shell
docker compose exec piezalink piezalink          # Menu
docker compose exec piezalink piezalink --help   # CLI help
```

See the [root README](../README.md) for `.env` setup, the `piezalia-db` service,
the shared network, and database volume lifecycle.

Dependencies are declared in `piezalink/requirements.txt`, which is also read by
`setup.py`. Docker installs them first with `pip install -r`, allowing dependency
layers to be reused when application code changes. Rebuild the image after edits.

The image installs PiezaLink and Bash, with no application entry point.
Its default command is `sleep infinity`, keeping the container available for
`docker compose exec`. Run `piezalink` explicitly from a shell or with exec.
For a one-off container, use `docker compose run --rm piezalink piezalink --help`.
The supplied `portal-sim/` remains unchanged. Compose starts it as `portal` on
the app network, accessible internally at `http://portal:8080/docs` and from your
machine at `http://localhost:80/docs` (mapping `127.0.0.1:80` to container port 8080).
The app waits for its health check and the database health check
before starting.

The build context is the repository root, so the image also includes
`data/feed_dia*.csv` at `/app/data/`. For example, from a container shell:

```sh
piezalink import data/feed_dia1.csv
piezalink import data/feed_dia2.csv
```

These commands use the regular validation rules. Day one imports full source
titles; day two contains other invalid records and needs correction in a separate copy.
The image only bundles the CSVs; building or starting it does not import data.


## Database interface

Install the package to include Psycopg and python-dotenv. Edit the repository's
`.env` to point at your running PostgreSQL server (or copy `.env.example` for a
fresh checkout):

```dotenv
DATABASE_URL=postgresql://postgres:postgres@piezalia-db:5432/rovira
```

For direct Python execution outside Docker, replace `piezalia-db` with a database
hostname reachable from your host. Compose does not publish a host database port.

Configuration priority is an explicit `init_db(dsn)` argument, then the process
`DATABASE_URL`, then the nearest `.env` found from the working directory upward.
Existing connections keep their configuration; close the connection before changing
it. `.env` is excluded from Git and Docker build contexts; `.env.example` is the
shareable template. Run cron from the project directory or supply `DATABASE_URL`
in the cron environment.

`init_db()` creates the database if missing and applies the packaged `schema.sql`.
Database creation requires `CREATEDB`; existing databases need schema creation
permissions. Schema initialization also loads the supplied `accounts.csv` in the
same transaction. A one-time upgrade inserts missing accounts into existing
simulation databases, preserving any tokens already stored. Subsequent startups
do not reload the CSV or replace renewed tokens. Invalid seed data rolls back
schema initialization. `upsert_account()` remains available for later token updates.

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

Set `DATABASE_URL`; `init_db()` automatically loads the supplied accounts before
the first feed import. Docker includes `/app/accounts.csv` and sets `ACCOUNTS_CSV`
to that path. Outside Docker, PiezaLink searches for `accounts.csv` from the working
directory upward. Override the file with `init_db(accounts_path="/path/accounts.csv")`
or `ACCOUNTS_CSV` in the environment/`.env`. A relative path in `.env` is resolved
against that file's directory. Bootstrap runs only during schema initialization
or upgrade; it does not continuously synchronize account files.

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
IDs remain strings. Prices accept a decimal point or comma; extra fractional zeros
are removed only when the value remains exactly representable with two decimals
(`9,950` becomes `9.95`, while `9,951` fails). Currency and thousands separators
remain unsupported. Titles must be nonempty; the catalogue stores their full length.
The marketplace's 80-character limit will be handled when publishing, without
changing the stored source title. Separate known account
IDs with `|`; order and repeated account IDs do not matter. Semicolons and empty
account segments are rejected. Blank/whitespace-only lines are skipped; delimiter-only
records such as `,,,` are invalid. An unquoted decimal comma can be reconstructed
only when there is exactly one extra field, both price fragments are digits, the
price needs no rounding, and the resulting account field is valid. Additional
fields are never discarded.

Product duplicates with identical normalized title, price, and accounts are merged;
conflicting duplicates reject the whole feed and identify two differing source lines.
Missing fields, unknown accounts, invalid values, and manual-entry collisions also
reject the whole feed. Row and database checks are reported together, with physical
source lines and product IDs. Up to 50 diagnostics are displayed; the full error
count is retained. No valid subset is committed when errors remain. Successful
summaries report unique products, skipped blank lines, merged duplicates, and
reconstructed prices.

The supplied day-one feed can be imported with the automatically seeded accounts,
including titles exceeding 80 characters. Day two still fails validation because
of malformed records, conflicting duplicate IDs, invalid prices, and unknown accounts.
Correct a separate copy before importing it; the importer never truncates titles
or silently skips invalid records. Publishing-time truncation or user alerts are
deferred to the future sync implementation.

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
only when catalogue data, status, or desired accounts change. Schema version 2
migrates existing title columns to `TEXT`, preserving titles and timestamps.
Schema version 3 bootstraps accounts without overwriting existing tokens; subsequent
schema upgrades do not repeat bootstrap. Schema version 4 extends temporary staging
for combined validation and normalized duplicate handling. Version 5 adds desired
revisions, accepted-feed markers, and synchronization progress/history.
Future migrations must advance that version and its check.
CSV parsing uses Python's standard library. `.env` configuration uses python-dotenv,
listed in the shared requirements file.

Docker declares a development `DATABASE_URL` default targeting host `piezalia-db`.
Compose reads the root `.env` and initializes Postgres using its `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD` values. Keep those settings consistent
with `DATABASE_URL`.
Use `--env-file .env` or `-e DATABASE_URL=...` to override it. Before passing `.env`
to Docker, set its database hostname to a name reachable from the container
(e.g. `your-postgres`, rather than `localhost`, on a shared Docker network).
Mount the feed directory when importing:

```sh
docker compose run --rm -v "$PWD/data:/feeds:ro" \
    piezalink piezalink import /feeds/your-valid-feed.csv
```

## Synchronize with Piezalia

Start the simulator and app from the repository root:

```sh
docker compose up -d --build --wait
# Establish the accepted-feed marker, including once after upgrading an older DB.
docker compose exec piezalink piezalink import data/feed_dia1.csv
docker compose exec piezalink piezalink sync
docker compose exec piezalink piezalink sync --help
```

Alternatively select **Sync catalogue** in the menu. An already-running external
simulator/provider can be selected with `PORTAL_BASE_URL`; make its hostname
reachable from the app's Docker network. Native Python execution defaults to
`http://localhost:8080`, while Compose defaults to `http://portal:8080`.
The Compose simulator is independent of previously started simulator
containers and their in-memory state.

The service requires a successfully imported complete feed. Older databases need
one successful reimport to establish this marker; existing catalogue data is retained.
It discovers all pages before writing to an account, adopts existing SKU listings,
and ignores `sku: null` manual ads and locally flagged manual entries. Retirements
are prioritized before updates and creations. The oldest ad for each SKU is retained;
additional duplicates are retired after confirming the retained ad's correct content.
Managed SKUs missing from the accepted catalogue are retired too.

`unlisted`/`modified` products and account changes are reconciled against actual
remote state; `sold` products are retired. An unchanged remote payload produces
no write. Full source titles stay in Postgres; outgoing titles are truncated to
80 characters. Truncation is recorded in `last_warning`/operation history and shown
in the summary. All desired account memberships must acknowledge the current
revision, with removals and errors resolved, before a product becomes `listed`.

A database session lock admits one sync across processes. A competing run exits
with an error. Candidate identifiers are captured once; each request reads the
latest desired state. Multiple imports before processing collapse into the latest
payload. Changes during/after processing and newly added products remain pending
for the next cronjob; the command does not chain more passes. Imports can continue:
only short, consistently ordered database transactions hold writer locks, and no
HTTP request or retry sleep occurs inside those transactions.

Each write is journaled before sending. Successful responses acknowledge only the
sent product revision and retain new listing IDs even if an import changed the
product meanwhile. Imports retain products/account links with pending requests,
so a creation arriving after a removal can be retired in a later sync. Confirmed
remote changes are retained after partial failure or Ctrl+C; synchronization has
no whole-run rollback across PostgreSQL and the remote API.

429 responses respect `Retry-After`; 503 responses use bounded backoff. A 401 pauses
that account while other accounts continue. Validation errors are recorded without
blind retries. A 504, network timeout, or invalid write response leaves an uncertain
operation, followed by complete rediscovery. If its outcome remains uncertain, the
current run defers it rather than immediately sending a replacement. The next run
rechecks the portal before retrying prior unresolved work. The API has no idempotency
key or conditional-write mechanism; a request can temporarily apply stale data,
which subsequent reconciliation corrects.

Settings are read from the environment or `.env` (environment wins):

| Setting | Default |
| --- | --- |
| `PORTAL_BASE_URL` | `http://portal:8080` in Compose |
| `PORTAL_TIMEOUT_SECONDS` | `10` |
| `SYNC_RETRY_ATTEMPTS` | `5` |
| `SYNC_RETRY_BASE_SECONDS` | `1` |
| `SYNC_RETRY_CAP_SECONDS` | `30` |
| `SYNC_RETRY_BUDGET_SECONDS` | `120` |

The budget caps accumulated retry sleeps per candidate/discovery, and provider
`Retry-After` delays exceeding the remaining budget defer work. Progress is printed
during discovery, retries, and every 25 candidates. Exit 0 means the pass completed
without known pending work; partial/failure/busy runs return 1, usage errors 2, and
Ctrl+C 130. Default simulator account 4 is expired, so its work remains pending and
the run reports partial success until that account is renewed.

`sync_runs` stores run summaries; `sync_operations` stores requested payloads,
revisions, remote IDs, states, warnings, and errors. Account rows expose the latest
account failure; membership rows expose applied revisions, attempt/success dates,
and errors. These records are displayed by the `status` command. Successful operations
are not undone or blindly repeated on restart.

### Sync integration tests

Use an isolated project because the integration suite resets its database and
simulator. It does not use the normal Compose project's data:

```sh
docker compose -p piezalink-test up -d --build --wait
docker compose -p piezalink-test run --rm -T \
    -e PIEZALINK_SYNC_INTEGRATION=1 -e PORTAL_BASE_URL=http://portal:8080 \
    -v "$PWD/tests:/tests:ro" -v "$PWD/data:/data:ro" \
    --entrypoint python piezalink -m unittest discover -s /tests -v
docker compose -p piezalink-test down --volumes
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

### Whole-feed validation and rollback

For testing, the reported errors in the supplied day-two feed were manually
corrected in [`data/feed_dia2_corrected.csv`](../../data/feed_dia2_corrected.csv).
The original is unchanged. Missing/invalid values use day-one data, clear format
errors are normalized, and conflicting duplicates keep their first occurrence.
The unidentified row matching product `26013` was removed in favor of its existing
identified row, along with the empty delimiter-only record. These choices are
test assumptions, not confirmed supplier corrections; the importer still rejects
the original feed and rolls back. See the root README for the correction policy.

We import the complete feed in one transaction and roll back the entire import
when unresolved validation errors remain. This decision follows the risks described
in the [exercise brief](../ENUNCIADO.md): missing products should be retired promptly,
but Rovira's management software can also export an incomplete catalogue.

Skipping an invalid record could make an existing product appear absent. A later
sync might then retire a valid listing, losing its visits and search position when
republished. Incoherent prices, account assignments, or conflicting duplicates also
need review before they become instructions for synchronization.

The importer reports the issues and preserves the last accepted catalogue. Review
problematic records individually to determine the correct update, account membership,
or genuine removal, then submit the corrected complete feed again. Existing listings
that remain for sale must be updated in place during sync.

Preserving the previous catalogue can delay legitimate removals, so failed imports
need prompt review and retry. Rollback protects consistency; it does not establish
that the previous catalogue is still up to date.

### Bounded syncs and revision acknowledgements

We let the next cronjob handle work arriving after a candidate was processed.
Automatically chaining full passes could keep sync running indefinitely while
feeds continue to arrive. Re-reading before each request coalesces earlier changes;
per-account revision acknowledgements preserve later ones without repeating
already successful writes. The operation journal preserves uncertain results and
in-flight creations when imports remove products. Long source titles are retained,
with deterministic truncation only in the publishing payload.

### Packaging and Docker entry point

We package all the services using `docker compose` and a `compose.yaml` to make
the whole project reproducible.

We add the simulator as an extra service apart from the database and piezalink ones.
The entrypoint in the piezalink service was originally the direct access to the menu, while developing we saw the necessity of holding it up, just stand by, for being able to enter the container for debugging.

## Source consulted

- [The easy (and nice) way to do CLI apps in Python](https://trstringer.com/easy-and-nice-python-cli/)
- [Programar tareas con cron y crontab en Linux](https://www.stackscale.com/es/blog/cron-crontab-linux/)
- [The most popular PostgreSQL adapter for Python](https://www.psycopg.org/)

## Product status for support

```sh
docker compose exec piezalink piezalink status 8090
```

Or choose **3. Product status** in the menu and enter the product ID / SKU;
a blank input returns to the menu. IDs are exact strings, including leading zeros.
The report includes the full title, price, catalogue status, creation/modification
dates, and the latest recorded listing confirmation. Each account shows whether
the product is requested, its portal listing ID, its own last confirmation and
write attempt, account check time, errors, warnings, and latest recorded operation.
It explains pending publication, updates, retirements, and uncertain requests.
All dates are UTC. A confirmation in one account does not imply success in all
accounts. No portal API requests are made: this is the database's last known state.
Account checks and historical operation records are labeled separately from
listing confirmations. Missing products produce a clear message and exit code 1;
a successful lookup returns 0 even if synchronization is pending. Products removed
from the local catalogue have no current details; historical operations alone
are not presented as a current product. No schema migration is needed.

Status includes accounts with current memberships, historical product operations,
or account errors. An unrelated account error is labeled explicitly. Confirmed
retirements count toward last sync even after their membership rows are cleaned
up; write attempts are also recovered from operation history. `sold` means the
product is absent from the accepted catalogue feed, while each account's status
shows whether portal retirement is confirmed, pending, or unverified. A partial
sync does not undo successful work in other accounts.

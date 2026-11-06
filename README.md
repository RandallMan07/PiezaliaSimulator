# PiezaLink

Rovira catalogue importer and synchronizer with a Python CLI, a terminal menu, and persistent
PostgreSQL storage. See [application documentation](app/README.md) for validation,
database APIs, and tests.
The [rollback decision](app/README.md#whole-feed-validation-and-rollback) explains
why invalid feeds preserve the last accepted catalogue.

## Repository layout

- `compose.yaml`: app and PostgreSQL services, shared network, and database volume.
- `Dockerfile` / `.dockerignore`: application image built from the repository root.
- `app/`: Python package, packaging configuration, and developer notes.
- `data/`: simulation feeds, bundled into the image at `/app/data/`.
- `accounts.csv`: supplied accounts, loaded automatically on first database initialization.
- `portal-sim/`: unchanged marketplace simulator, optionally started with the `simulation` profile.
- `tests/`: application and PostgreSQL integration tests.

## Run with Docker Compose

For a fresh checkout, copy `.env.example` to `.env`. Set `POSTGRES_DB`,
`POSTGRES_USER`, and `POSTGRES_PASSWORD`, and keep `DATABASE_URL` consistent.
Use `piezalia-db` as its hostname inside Compose. URL-special characters in
credentials must be percent-encoded in `DATABASE_URL` only.

```sh
cp .env.example .env                    # Fresh checkout only
# Edit .env as needed.
docker compose up -d --build            # Start both containers in the background
docker compose exec piezalink bash     # Enter the app container
```

Compose starts `piezalia-db` and waits for its health check before starting the
PiezaLink container. PiezaLink is installed but does not launch automatically;
the container stays running with `sleep infinity`. The first call to `init_db()`
(e.g. during import) creates/upgrades the schema and seeds `accounts.csv` in one
transaction. Existing tokens are preserved; later startups do not reload the file.
The one-time migration also adds missing accounts to existing simulation databases.
Both services use the shared `piezalink` network. The database is accessible within
that network on port 5432; no host port is needed for this setup.

```sh
docker compose exec piezalink piezalink          # Interactive menu
docker compose exec piezalink piezalink --help
docker compose exec piezalink piezalink import data/feed_dia1.csv
```

The first database initialization loads all four supplied accounts automatically.
Day one's full titles are stored,
including those over 80 characters; marketplace title checks belong to publishing.
Day two contains other invalid records and is rejected without partial changes.
Correct a separate copy before importing it. To mount corrected feeds, use:

```sh
docker compose run --rm -v "$PWD/data:/feeds:ro" \
    piezalink piezalink import /feeds/your-valid-feed.csv
```

## Synchronize the catalogue

```sh
docker compose --profile simulation up -d --build --wait
docker compose exec piezalink piezalink import data/feed_dia1.csv
docker compose exec piezalink piezalink sync
```

`sync` is also available in the menu. Reimport once after upgrading to establish
the accepted-feed marker. The optional simulator runs as `portal` on the shared
network; select another reachable API with `PORTAL_BASE_URL` in `.env`. Existing
standalone simulators retain their own state.

Sync performs one bounded pass and leaves newer work for the next cronjob.
It adopts existing ads, updates in place, retires sold/unwanted ads, and preserves
manual listings. Publishing titles are truncated to 80 characters; source titles
stay complete. Pending requests and per-account errors are stored in Postgres.
Default simulator account 4 has an expired token, so partial success is expected
until it is renewed. See [sync details and tests](app/README.md#synchronize-with-piezalia).

## Database lifecycle

```sh
docker compose up -d --wait piezalia-db  # Start just the database
docker compose ps
docker compose logs piezalia-db
docker compose exec piezalia-db sh      # Open a database-container shell
docker compose down                    # Remove containers/network; preserve data
```

The named `piezalia-db-data` volume stores PostgreSQL files independently of both
containers. A later Compose run reuses it, including when the database container
is recreated. Initialization variables configure a new, empty volume; changing
them later does not change existing database credentials.
`docker compose down --volumes` also deletes the database volume and its data.

Without Compose, use `docker run -d --name piezalink-dev piezalink:latest`, then
`docker exec -it piezalink-dev bash` or `docker exec -it piezalink-dev piezalink`.
Pass the database environment and network settings when creating that container.

The simulator runs as its own optional service. To build the app without Compose, use
`docker build -t piezalink .`; running it still requires a reachable PostgreSQL
server. For Python running directly on the host, use a host-reachable URL instead
of Compose's internal `piezalia-db` hostname.

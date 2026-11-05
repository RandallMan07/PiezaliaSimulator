"""Transactional PostgreSQL interface shared by CLI and menu services."""

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from threading import RLock

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict
from psycopg.rows import dict_row

from .config import get_database_url

_instance = None
_init_lock = RLock()


def _price(value):
    try:
        amount = Decimal(str(value))
        if (
            not amount.is_finite()
            or not 0 < amount <= Decimal("99999.99")
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise ValueError(
                "Price must be positive, at most 99999.99, with at most two decimals"
            )
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Invalid price") from exc
    return amount


def _accounts(values):
    values = list(values)
    if any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("Accounts must be positive integer IDs")
    return list(dict.fromkeys(values))


class RoviraDB:
    """One connection, serialized transactions; use transaction() for grouped work."""

    def __init__(self, connection):
        self._connection = connection
        self._lock = RLock()

    @contextmanager
    def transaction(self):
        """Commit all statements together, or roll them all back on failure.

        Access the connection only inside this context. Do not close it or change
        transaction settings here. Nested calls use PostgreSQL savepoints.
        """
        with self._lock, self._connection.transaction():
            yield self._connection

    def close(self):
        with self._lock:
            self._connection.close()

    def upsert_account(self, account_id, token):
        """Register or renew an account before associating catalogue entries."""
        _accounts([account_id])
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO accounts (account_id, token) VALUES (%s, %s) "
                "ON CONFLICT (account_id) DO UPDATE SET token = EXCLUDED.token",
                (account_id, token),
            )

    def create_listing(
        self,
        product_id,
        title,
        price,
        accounts,
        status="unlisted",
        listed_manually=False,
    ):
        accounts = _accounts(accounts)
        price = _price(price)
        with self.transaction() as conn:
            row = conn.execute(
                "INSERT INTO catalogue (product_id, title, price, status, listed_manually) "
                "VALUES (%s, %s, %s, %s, %s) RETURNING id",
                (product_id, title, price, status, listed_manually),
            ).fetchone()
            for account_id in accounts:
                conn.execute(
                    "INSERT INTO catalogue_accounts (catalogue_id, account_id) VALUES (%s, %s)",
                    (row["id"], account_id),
                )
        return row["id"]

    def _update(self, listing_id, field, value):
        with self.transaction() as conn:
            row = conn.execute(
                sql.SQL(
                    "UPDATE catalogue SET {} = %s WHERE id = %s RETURNING id"
                ).format(sql.Identifier(field)),
                (value, listing_id),
            ).fetchone()
            if row is None:
                raise LookupError(f"Catalogue entry {listing_id} does not exist")

    def update_price(self, listing_id, new_value):
        self._update(listing_id, "price", _price(new_value))

    def update_title(self, listing_id, new_title):
        self._update(listing_id, "title", new_title)

    def update_account(self, listing_id, accounts):
        """Replace memberships atomically, retaining listing IDs on unchanged links."""
        accounts = _accounts(accounts)
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM catalogue WHERE id = %s FOR UPDATE",
                (listing_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Catalogue entry {listing_id} does not exist")
            conn.execute(
                "DELETE FROM catalogue_accounts WHERE catalogue_id = %s "
                "AND NOT (account_id = ANY(%s::integer[])) AND listing_id IS NULL",
                (listing_id, accounts),
            )
            conn.execute(
                "UPDATE catalogue_accounts SET desired = FALSE WHERE catalogue_id = %s "
                "AND NOT (account_id = ANY(%s::integer[])) AND desired",
                (listing_id, accounts),
            )
            for account_id in accounts:
                conn.execute(
                    "INSERT INTO catalogue_accounts (catalogue_id, account_id) VALUES (%s, %s) "
                    "ON CONFLICT (catalogue_id, account_id) DO UPDATE SET desired = TRUE "
                    "WHERE NOT catalogue_accounts.desired",
                    (listing_id, account_id),
                )

    def update_listing_id(self, listing_id, account_id, portal_listing_id):
        """Record Piezalia's ID for one membership; None clears the reference."""
        with self.transaction() as conn:
            row = conn.execute(
                "UPDATE catalogue_accounts SET listing_id = %s "
                "WHERE catalogue_id = %s AND account_id = %s RETURNING catalogue_id",
                (portal_listing_id, listing_id, account_id),
            ).fetchone()
            if row is None:
                raise LookupError(
                    f"Catalogue entry {listing_id} has no account {account_id}"
                )

    def get_listing(self, listing_id):
        """Return the catalogue row plus account-to-portal-ID mappings, or None."""
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM catalogue WHERE id = %s", (listing_id,)
            ).fetchone()
            if row is not None:
                row["accounts"] = conn.execute(
                    "SELECT account_id, listing_id FROM catalogue_accounts "
                    "WHERE catalogue_id = %s AND desired ORDER BY account_id",
                    (listing_id,),
                ).fetchall()
                row["pending_removals"] = conn.execute(
                    "SELECT account_id, listing_id FROM catalogue_accounts "
                    "WHERE catalogue_id = %s AND NOT desired ORDER BY account_id",
                    (listing_id,),
                ).fetchall()
            return row

    def import_catalogue(self, rows, *, allow_large_removals=False):
        """Stage a validated row stream, then reconcile the complete catalogue."""
        from .feed_import import (
            FeedValidationError, ImportBusyError, ImportResult, LargeRemovalsError,
        )

        with self.transaction() as conn:
            locked = conn.execute(
                "SELECT pg_try_advisory_xact_lock(739271002) AS locked"
            ).fetchone()["locked"]
            if not locked:
                raise ImportBusyError("Another import is running; try again later.")
            conn.execute("SELECT create_feed_staging()")
            with conn.cursor().copy(
                "COPY pg_temp.feed_stage (record_no, product_id, title, price, account_ids) FROM STDIN"
            ) as copy:
                for row in rows:
                    copy.write_row(row)
            total = conn.execute("SELECT count(*) AS n FROM pg_temp.feed_stage").fetchone()["n"]
            if not total:
                raise FeedValidationError(["The feed is empty; no catalogue changes were applied."])
            # Other imports use the advisory lock. These locks also exclude writers
            # using ordinary database methods, while allowing plain SELECT queries.
            conn.execute("LOCK TABLE accounts IN SHARE MODE")
            conn.execute("LOCK TABLE catalogue, catalogue_accounts IN SHARE ROW EXCLUSIVE MODE")
            errors = []
            duplicates = conn.execute(
                "SELECT product_id, min(record_no) AS record_no FROM pg_temp.feed_stage "
                "GROUP BY product_id HAVING count(*) > 1 ORDER BY min(record_no) LIMIT 50"
            ).fetchall()
            for row in duplicates:
                errors.append(f"Record {row['record_no']}: duplicate product_id {row['product_id']!r}")
            if errors:
                raise FeedValidationError(errors)
            conn.execute(
                "INSERT INTO pg_temp.feed_accounts SELECT product_id, unnest(account_ids) "
                "FROM pg_temp.feed_stage"
            )
            unknown = conn.execute(
                "SELECT s.record_no, a.account_id FROM pg_temp.feed_accounts a "
                "JOIN pg_temp.feed_stage s USING (product_id) "
                "LEFT JOIN accounts known USING (account_id) WHERE known.account_id IS NULL "
                "ORDER BY s.record_no, a.account_id LIMIT 50"
            ).fetchall()
            errors.extend(f"Record {r['record_no']}: unknown account {r['account_id']}" for r in unknown)
            collisions = conn.execute(
                "SELECT s.record_no, s.product_id FROM pg_temp.feed_stage s "
                "JOIN catalogue c USING (product_id) WHERE c.listed_manually "
                "ORDER BY s.record_no LIMIT 50"
            ).fetchall()
            errors.extend(
                f"Record {r['record_no']}: product {r['product_id']!r} belongs to a manual entry"
                for r in collisions
            )
            if errors:
                raise FeedValidationError(errors)
            conn.execute("ANALYZE pg_temp.feed_stage")
            conn.execute("ANALYZE pg_temp.feed_accounts")
            missing = conn.execute(
                "SELECT count(*) AS active, count(*) FILTER (WHERE NOT EXISTS "
                "(SELECT 1 FROM pg_temp.feed_stage s WHERE s.product_id = c.product_id)) AS missing "
                "FROM catalogue c WHERE NOT listed_manually AND status <> 'sold'"
            ).fetchone()
            if not allow_large_removals and missing["missing"] * 5 > missing["active"]:
                raise LargeRemovalsError(missing["missing"], missing["active"])
            conn.execute(
                "INSERT INTO pg_temp.feed_actions (product_id, catalogue_id, action) "
                "SELECT s.product_id, c.id, CASE WHEN c.id IS NULL THEN 'added' "
                "WHEN c.status = 'sold' OR c.title IS DISTINCT FROM s.title "
                "OR c.price IS DISTINCT FROM s.price OR s.account_ids IS DISTINCT FROM "
                "ARRAY(SELECT a.account_id FROM catalogue_accounts a "
                "WHERE a.catalogue_id = c.id AND a.desired ORDER BY a.account_id) "
                "THEN 'modified' ELSE 'unchanged' END "
                "FROM pg_temp.feed_stage s LEFT JOIN catalogue c USING (product_id) "
                "UNION ALL SELECT c.product_id, c.id, CASE WHEN c.status = 'sold' THEN 'unchanged' "
                "WHEN c.status = 'unlisted' AND NOT EXISTS (SELECT 1 FROM catalogue_accounts a "
                "WHERE a.catalogue_id = c.id AND a.listing_id IS NOT NULL) THEN 'removed' "
                "ELSE 'sold' END FROM catalogue c WHERE NOT c.listed_manually "
                "AND NOT EXISTS (SELECT 1 FROM pg_temp.feed_stage s WHERE s.product_id = c.product_id)"
            )
            counts = {row["action"]: row["n"] for row in conn.execute(
                "SELECT action, count(*) AS n FROM pg_temp.feed_actions GROUP BY action"
            )}
            conn.execute(
                "INSERT INTO catalogue (product_id, title, price) "
                "SELECT s.product_id, s.title, s.price FROM pg_temp.feed_stage s "
                "JOIN pg_temp.feed_actions a USING (product_id) WHERE a.action = 'added'"
            )
            conn.execute(
                "UPDATE catalogue c SET title = s.title, price = s.price, status = 'modified' "
                "FROM pg_temp.feed_stage s JOIN pg_temp.feed_actions a USING (product_id) "
                "WHERE c.id = a.catalogue_id AND a.action = 'modified'"
            )
            conn.execute(
                "UPDATE catalogue c SET status = 'sold' FROM pg_temp.feed_actions a "
                "WHERE c.id = a.catalogue_id AND a.action = 'sold'"
            )
            # The desired set is empty for absent products. Keep remote references
            # until sync has retired them; drop only links without a remote ID.
            conn.execute(
                "DELETE FROM catalogue_accounts ca USING catalogue c "
                "WHERE ca.catalogue_id = c.id AND NOT c.listed_manually "
                "AND EXISTS (SELECT 1 FROM pg_temp.feed_actions a "
                "WHERE a.product_id = c.product_id AND a.action <> 'unchanged') "
                "AND ca.listing_id IS NULL AND NOT EXISTS "
                "(SELECT 1 FROM pg_temp.feed_accounts s "
                "WHERE s.product_id = c.product_id AND s.account_id = ca.account_id)"
            )
            conn.execute(
                "UPDATE catalogue_accounts ca SET desired = FALSE FROM catalogue c "
                "WHERE ca.catalogue_id = c.id AND NOT c.listed_manually AND ca.desired "
                "AND EXISTS (SELECT 1 FROM pg_temp.feed_actions a "
                "WHERE a.product_id = c.product_id AND a.action <> 'unchanged') "
                "AND NOT EXISTS (SELECT 1 FROM pg_temp.feed_accounts s "
                "WHERE s.product_id = c.product_id AND s.account_id = ca.account_id)"
            )
            conn.execute(
                "INSERT INTO catalogue_accounts (catalogue_id, account_id) "
                "SELECT c.id, s.account_id FROM pg_temp.feed_accounts s "
                "JOIN catalogue c USING (product_id) "
                "ON CONFLICT (catalogue_id, account_id) DO UPDATE SET desired = TRUE "
                "WHERE NOT catalogue_accounts.desired"
            )
            conn.execute(
                "DELETE FROM catalogue c USING pg_temp.feed_actions a "
                "WHERE c.id = a.catalogue_id AND a.action = 'removed'"
            )
            result = ImportResult(total=total, **counts)
        return result

    def remove_listing(self, listing_id):
        """Delete the local entry and its account links; make no marketplace calls."""
        with self.transaction() as conn:
            row = conn.execute(
                "DELETE FROM catalogue WHERE id = %s RETURNING id",
                (listing_id,),
            ).fetchone()
            if row is None:
                raise LookupError(f"Catalogue entry {listing_id} does not exist")


def _schema_current(conn):
    exists = conn.execute("SELECT to_regclass('piezalink_schema') IS NOT NULL AS present").fetchone()
    if not exists["present"]:
        return False
    row = conn.execute("SELECT version FROM piezalink_schema WHERE singleton").fetchone()
    if row and row["version"] > 1:
        raise RuntimeError("The database schema is newer than this application")
    return row is not None and row["version"] == 1


def init_db(dsn=None):
    """Create the database/schema and return the process-local shared interface.

    Supply a PostgreSQL DSN or DATABASE_URL (environment or nearest .env). Creating a missing database requires
    CREATEDB permission and access to the postgres maintenance database.
    """
    global _instance
    dsn = get_database_url(dsn)
    with _init_lock:
        if _instance is not None and not _instance._connection.closed:
            if _instance._dsn != dsn:
                raise ValueError(
                    "This process already has a connection to another database"
                )
            return _instance
        try:
            connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        except psycopg.OperationalError as original_error:
            # Failed startup connections may lack SQLSTATE (e.g. missing database).
            # Check the maintenance database instead of parsing error messages.
            params = conninfo_to_dict(dsn)
            database = params.get("dbname")
            if not database:
                raise ValueError(
                    "Specify a database name in the DSN"
                ) from original_error
            params["dbname"] = "postgres"
            try:
                admin = psycopg.connect(**params, autocommit=True)
            except psycopg.OperationalError:
                raise original_error from None
            with admin:
                if admin.execute(
                    "SELECT 1 FROM pg_database WHERE datname = %s", (database,)
                ).fetchone():
                    raise original_error
                try:
                    admin.execute(
                        sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database))
                    )
                except (
                    psycopg.errors.DuplicateDatabase,
                    psycopg.errors.UniqueViolation,
                ):
                    # Another process may have created the target after our first attempt.
                    pass
            connection = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        db = RoviraDB(connection)
        db._dsn = dsn
        try:
            with db.transaction() as conn:
                if not _schema_current(conn):
                    # Serialize migrations, but avoid DDL on normal CLI startup.
                    conn.execute("SELECT pg_advisory_xact_lock(739271001)")
                    if not _schema_current(conn):
                        conn.execute(
                            files("piezalink.core").joinpath("schema.sql").read_text(encoding="utf-8")
                        )
        except BaseException:
            db.close()
            raise
        _instance = db
        return db

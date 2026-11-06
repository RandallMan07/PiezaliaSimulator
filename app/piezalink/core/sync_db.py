"""Short, atomic persistence operations for long-running synchronizations."""

from contextlib import contextmanager

import psycopg
from psycopg.types.json import Jsonb


class SyncRepository:
    def __init__(self, db):
        self.db = db
        self.lock_connection = None

    @contextmanager
    def transaction(self):
        with self.db.catalogue_write_transaction() as conn:
            yield conn

    @contextmanager
    def exclusive(self):
        from .sync import SyncBusyError
        # A distinct session prevents same-session advisory-lock reentrancy.
        with psycopg.connect(self.db._dsn, autocommit=True) as connection:
            if not connection.execute("SELECT pg_try_advisory_lock(739271003)").fetchone()[0]:
                raise SyncBusyError("Another sync is running; try again later.")
            self.lock_connection = connection
            try:
                yield
            finally:
                if not connection.closed:
                    connection.execute("SELECT pg_advisory_unlock(739271003)")
                self.lock_connection = None

    def check_lock(self):
        if self.lock_connection is None:
            raise RuntimeError("The sync lock is not held")
        self.lock_connection.execute("SELECT 1")

    def start(self):
        from .sync import SyncError
        with self.transaction() as conn:
            conn.execute("LOCK TABLE catalogue, catalogue_accounts, accepted_feeds IN SHARE MODE")
            if not conn.execute("SELECT 1 FROM accepted_feeds LIMIT 1").fetchone():
                raise SyncError("Import a complete feed successfully before running sync (also required after upgrading).")
            conn.execute("UPDATE sync_runs SET status = 'interrupted', finished_at = now(), "
                         "last_error = 'Previous sync ended without completing' WHERE status = 'running'")
            conn.execute("UPDATE sync_operations SET state = 'unknown', updated_at = now() WHERE state = 'pending'")
            run_id = conn.execute("INSERT INTO sync_runs DEFAULT VALUES RETURNING id").fetchone()["id"]
            accounts = conn.execute("SELECT account_id FROM accounts ORDER BY account_id").fetchall()
            known = {r["product_id"] for r in conn.execute("SELECT product_id FROM catalogue WHERE NOT listed_manually")}
            pairs = conn.execute("SELECT c.product_id, a.account_id FROM catalogue c "
                                 "JOIN catalogue_accounts a ON a.catalogue_id = c.id WHERE NOT c.listed_manually").fetchall()
            pending = conn.execute("SELECT DISTINCT product_id, account_id FROM sync_operations "
                                   "WHERE state IN ('pending', 'unknown')").fetchall()
            return run_id, accounts, known, pairs + pending

    def token(self, account):
        with self.transaction() as conn:
            return conn.execute("SELECT token FROM accounts WHERE account_id = %s", (account,)).fetchone()["token"]

    def target(self, sku, account, *, lock=False):
        with self.transaction() as conn:
            if lock:
                conn.execute("SELECT id FROM catalogue WHERE product_id = %s FOR UPDATE", (sku,)).fetchone()
            return conn.execute(
                "SELECT c.*, a.desired, a.listing_id, a.synced_revision FROM catalogue c "
                "LEFT JOIN catalogue_accounts a ON a.catalogue_id = c.id AND a.account_id = %s "
                "WHERE c.product_id = %s", (account, sku),
            ).fetchone()

    @staticmethod
    def signature(target):
        return None if target is None else (target["id"], target["desired_revision"], target["desired"], target["listed_manually"])

    def prepare(self, run, account, sku, target, action, listing_id, payload):
        with self.transaction() as conn:
            current = self.target(sku, account, lock=True)
            if self.signature(current) != self.signature(target):
                return None
            row = conn.execute(
                "INSERT INTO sync_operations (run_id, catalogue_id, account_id, product_id, action, "
                "listing_id, revision, payload, warning) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
                (run, current["id"] if current else None, account, sku, action, listing_id,
                 current["desired_revision"] if current else None, Jsonb(payload) if payload is not None else None,
                 "Publishing title truncated to 80 characters" if current and action != 'DELETE' and len(current["title"]) > 80 else None),
            ).fetchone()
            if current:
                conn.execute("UPDATE catalogue_accounts SET last_attempt_at = now(), last_error = NULL "
                             "WHERE catalogue_id = %s AND account_id = %s", (current["id"], account))
            return row

    def failure(self, operation, message, *, uncertain=False):
        with self.transaction() as conn:
            if operation["catalogue_id"] is not None:
                conn.execute("SELECT id FROM catalogue WHERE id = %s FOR UPDATE", (operation["catalogue_id"],))
            conn.execute("UPDATE sync_operations SET state = %s, last_error = %s, updated_at = now() WHERE id = %s",
                         ('unknown' if uncertain else 'failed', str(message), operation["id"]))
            conn.execute("UPDATE catalogue_accounts SET last_error = %s WHERE catalogue_id = %s AND account_id = %s",
                         (str(message), operation["catalogue_id"], operation["account_id"]))

    def account_error(self, account, message=None):
        with self.transaction() as conn:
            conn.execute("UPDATE accounts SET last_sync_error = %s, last_sync_at = now() WHERE account_id = %s",
                         (str(message) if message is not None else None, account))

    def acknowledge(self, sku, account, target, listing, warning=None):
        """Record the actual remote reference even if the desired revision changed."""
        with self.transaction() as conn:
            current = self.target(sku, account, lock=True)
            if not current or current["listed_manually"] or (target and current["id"] != target["id"]):
                return
            revision = target["desired_revision"] if target else 0
            conn.execute(
                "INSERT INTO catalogue_accounts (catalogue_id, account_id, desired, listing_id, synced_revision, last_synced_at, last_warning) "
                "VALUES (%s,%s,FALSE,%s,%s,now(),%s) ON CONFLICT (catalogue_id, account_id) DO UPDATE "
                "SET listing_id = EXCLUDED.listing_id, synced_revision = EXCLUDED.synced_revision, "
                "last_synced_at = now(), last_error = NULL, last_warning = EXCLUDED.last_warning",
                (current["id"], account, listing["listing_id"], revision, warning),
            )

    def confirmed(self, operation, listing=None):
        with self.transaction() as conn:
            conn.execute("UPDATE sync_operations SET state = 'confirmed', last_error = NULL, updated_at = now(), "
                         "listing_id = COALESCE(%s, listing_id) WHERE id = %s",
                         (listing["listing_id"] if listing else None, operation["id"]))
            target = self.target(operation["product_id"], operation["account_id"], lock=True)
            if listing and target and target["id"] == operation["catalogue_id"]:
                sent = dict(target, desired_revision=operation["revision"])
                self.acknowledge(operation["product_id"], operation["account_id"], sent, listing, operation["warning"])
            elif operation["action"] == 'DELETE' and target:
                conn.execute("UPDATE catalogue_accounts SET listing_id = NULL, synced_revision = 0, "
                             "last_synced_at = now(), last_error = NULL, last_warning = NULL WHERE catalogue_id = %s "
                             "AND account_id = %s AND listing_id = %s",
                             (target["id"], operation["account_id"], operation["listing_id"]))

    def absent(self, sku, account):
        with self.transaction() as conn:
            target = self.target(sku, account, lock=True)
            if not target or target["listed_manually"]:
                return
            if target["status"] != 'sold' and target["desired"]:
                return
            conn.execute(
                "DELETE FROM catalogue_accounts a WHERE catalogue_id = %s AND account_id = %s "
                "AND NOT EXISTS (SELECT 1 FROM sync_operations op WHERE op.catalogue_id = a.catalogue_id "
                "AND op.account_id = a.account_id AND state IN ('pending','unknown'))",
                (target["id"], account),
            )

    def refresh_references(self, account, inventory):
        """Clear missing/reused IDs before adopting any newly discovered ads."""
        observed = {r["listing_id"]: r["sku"] for r in inventory}
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT a.catalogue_id, a.listing_id, c.product_id FROM catalogue_accounts a "
                "JOIN catalogue c ON c.id = a.catalogue_id WHERE a.account_id = %s AND a.listing_id IS NOT NULL",
                (account,),
            ).fetchall()
            for row in rows:
                if observed.get(row["listing_id"]) != row["product_id"]:
                    conn.execute("SELECT id FROM catalogue WHERE id = %s FOR UPDATE", (row["catalogue_id"],))
                    conn.execute(
                        "UPDATE catalogue_accounts SET listing_id = NULL, synced_revision = 0 "
                        "WHERE catalogue_id = %s AND account_id = %s AND listing_id = %s",
                        (row["catalogue_id"], account, row["listing_id"]),
                    )

    def operation_state(self, operation_id):
        with self.transaction() as conn:
            return conn.execute("SELECT state FROM sync_operations WHERE id = %s", (operation_id,)).fetchone()["state"]

    def recover(self, account, inventory, run):
        from .portal import same_payload
        by_id = {r["listing_id"]: r for r in inventory}
        with self.transaction() as conn:
            operations = conn.execute("SELECT * FROM sync_operations WHERE account_id = %s "
                                      "AND state IN ('pending','unknown') ORDER BY id", (account,)).fetchall()
            for op in operations:
                candidates = [r for r in inventory if r["sku"] == op["product_id"]]
                listing = by_id.get(op["listing_id"])
                if op["action"] == 'POST':
                    listing = min(candidates, key=lambda r: (r["created_at"], r["listing_id"])) if candidates else None
                    confirmed = listing is not None
                elif op["action"] == 'PUT':
                    confirmed = listing is not None and listing["sku"] == op["product_id"] and same_payload(listing, op["payload"])
                else:
                    confirmed = listing is None or listing["sku"] != op["product_id"]
                if confirmed:
                    # A recovered create can discover different content; adopt its ID,
                    # but acknowledge the sent revision only when the payload matches.
                    if listing and op["action"] != 'DELETE' and not same_payload(listing, op["payload"]):
                        conn.execute("UPDATE sync_operations SET state = 'confirmed', listing_id = %s, updated_at = now() "
                                     "WHERE id = %s", (listing["listing_id"], op["id"]))
                    else:
                        self.confirmed(op, listing if op["action"] != 'DELETE' else None)
                elif op["run_id"] != run:
                    conn.execute("UPDATE sync_operations SET state = 'superseded', updated_at = now() WHERE id = %s", (op["id"],))

    def unresolved(self, sku, account):
        with self.transaction() as conn:
            return conn.execute("SELECT 1 FROM sync_operations WHERE product_id = %s AND account_id = %s "
                                "AND state IN ('pending','unknown') LIMIT 1", (sku, account)).fetchone() is not None

    def finalize(self):
        with self.transaction() as conn:
            conn.execute(
                "UPDATE catalogue c SET status = 'listed' WHERE NOT listed_manually AND status NOT IN ('sold','listed') "
                "AND EXISTS (SELECT 1 FROM catalogue_accounts a WHERE a.catalogue_id = c.id AND desired) "
                "AND NOT EXISTS (SELECT 1 FROM catalogue_accounts a WHERE a.catalogue_id = c.id AND "
                "(NOT desired OR listing_id IS NULL OR synced_revision <> c.desired_revision OR a.last_error IS NOT NULL)) "
                "AND NOT EXISTS (SELECT 1 FROM sync_operations op WHERE op.catalogue_id = c.id AND state IN ('pending','unknown'))"
            )
            return conn.execute(
                "SELECT count(*) AS n FROM catalogue_accounts a JOIN catalogue c ON c.id = a.catalogue_id "
                "WHERE NOT listed_manually AND (NOT desired OR c.status = 'sold' OR listing_id IS NULL "
                "OR synced_revision <> c.desired_revision OR a.last_error IS NOT NULL)"
            ).fetchone()["n"]

    def finish(self, run, status, summary, error=None):
        with self.transaction() as conn:
            conn.execute("UPDATE sync_runs SET status = %s, summary = %s, last_error = %s, finished_at = now() WHERE id = %s",
                         (status, Jsonb(summary), str(error) if error else None, run))

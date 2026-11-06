"""Integration tests: DATABASE_URL must point to a disposable test database."""

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import psycopg

from piezalink.core.rovira_db import init_db


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "Set DATABASE_URL to a disposable database")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = init_db()

    def setUp(self):
        with self.db.transaction() as conn:
            conn.execute("TRUNCATE catalogue_accounts, catalogue, accounts RESTART IDENTITY CASCADE")
        for account in (1, 2, 3):
            self.db.upsert_account(account, f"token-{account}")

    def create(self, **kwargs):
        return self.db.create_listing(
            kwargs.pop("product_id", "00123"), kwargs.pop("title", "Test part"),
            kwargs.pop("price", "12.50"), kwargs.pop("accounts", [1, 2]), **kwargs,
        )

    def test_shared_interface_and_exact_values(self):
        self.assertIs(init_db(), self.db)
        item = self.db.get_listing(self.create())
        self.assertEqual(item["product_id"], "00123")
        self.assertEqual(item["price"], Decimal("12.50"))
        self.assertEqual(item["status"], "unlisted")
        self.assertFalse(item["listed_manually"])

    def test_manual_entries_have_distinct_keys(self):
        first = self.create(product_id=None, listed_manually=True)
        second = self.create(product_id=None, listed_manually=True)
        self.assertNotEqual(first, second)
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.create(product_id=None)
        self.assertIsNone(self.db.get_listing(first)["product_id"])

    def test_uniqueness_and_constraints(self):
        self.create()
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.create()
        for kwargs in ({"status": "unknown"}, {"title": ""}, {"product_id": ""}):
            with self.assertRaises(psycopg.errors.CheckViolation):
                self.create(**{"product_id": "new", **kwargs})

    def test_full_titles_are_stored_and_updated(self):
        original = "Long source title " * 20
        key = self.create(product_id="long", title=original)
        self.assertEqual(self.db.get_listing(key)["title"], original)
        updated = original + "updated"
        self.db.update_title(key, updated)
        self.assertEqual(self.db.get_listing(key)["title"], updated)

    def test_price_validation_and_updates(self):
        key = self.create()
        self.db.update_price(key, "99999.99")
        self.db.update_title(key, "Updated title")
        for bad in ("0", "-1", "1.001", "NaN", "Infinity", "bad", "100000"):
            with self.assertRaises(ValueError):
                self.db.update_price(key, bad)
        row = self.db.get_listing(key)
        self.assertEqual(row["price"], Decimal("99999.99"))
        self.assertEqual(row["title"], "Updated title")

    def test_failed_creation_rolls_back_whole_operation(self):
        with self.assertRaises(psycopg.errors.ForeignKeyViolation):
            self.create(accounts=[1, 999])
        with self.db.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM catalogue").fetchone()["n"], 0)
        self.create()  # The connection is usable after rollback.

    def test_account_replacement_preserves_ids_and_rolls_back(self):
        key = self.create()
        self.db.update_listing_id(key, 2, "L2")
        self.db.update_account(key, [2, 3, 3])
        expected = [{"account_id": 2, "listing_id": "L2"}, {"account_id": 3, "listing_id": None}]
        self.assertEqual(self.db.get_listing(key)["accounts"], expected)
        for accounts, error in (([999], psycopg.errors.ForeignKeyViolation), ([], psycopg.errors.CheckViolation)):
            with self.assertRaises(error):
                self.db.update_account(key, accounts)
            self.assertEqual(self.db.get_listing(key)["accounts"], expected)

    def test_active_requires_accounts_but_sold_may_have_none(self):
        with self.assertRaises(psycopg.errors.CheckViolation):
            self.create(accounts=[])
        key = self.create(accounts=[], status="sold")
        self.assertEqual(self.db.get_listing(key)["accounts"], [])
        active = self.create(product_id="active", accounts=[1])
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db.transaction() as conn:
                conn.execute("DELETE FROM catalogue_accounts WHERE catalogue_id = %s", (active,))

    def test_portal_ids_are_scoped_to_accounts(self):
        key = self.create()
        other = self.create(product_id="other", accounts=[1, 3])
        self.db.update_listing_id(key, 1, "L1")
        self.db.update_listing_id(other, 3, "L1")
        with self.assertRaises(psycopg.errors.UniqueViolation):
            self.db.update_listing_id(other, 1, "L1")
        self.db.update_listing_id(key, 1, None)
        self.assertIsNone(self.db.get_listing(key)["accounts"][0]["listing_id"])

    def test_remove_cascades_and_missing_targets_raise(self):
        key = self.create()
        self.db.remove_listing(key)
        self.assertIsNone(self.db.get_listing(key))
        with self.db.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM catalogue_accounts").fetchone()["n"], 0)
        for action in (lambda: self.db.update_price(key, "1"), lambda: self.db.update_title(key, "x"),
                       lambda: self.db.update_account(key, [1]), lambda: self.db.update_listing_id(key, 1, "L1"),
                       lambda: self.db.remove_listing(key)):
            with self.assertRaises(LookupError):
                action()

    def test_grouped_updates_roll_back_together(self):
        key = self.create()
        with self.assertRaises(psycopg.errors.CheckViolation):
            with self.db.transaction():
                self.db.update_price(key, "8.00")
                self.db.update_title(key, "")
        self.assertEqual(self.db.get_listing(key)["price"], Decimal("12.50"))

    def test_concurrent_callers_do_not_share_failed_transactions(self):
        def operation(number):
            if number % 2:
                with self.assertRaises(psycopg.errors.ForeignKeyViolation):
                    self.create(product_id=str(number), accounts=[1, 999])
            else:
                self.create(product_id=str(number))
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(operation, range(20)))
        with self.db.transaction() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM catalogue").fetchone()["n"], 10)


if __name__ == "__main__":
    unittest.main()

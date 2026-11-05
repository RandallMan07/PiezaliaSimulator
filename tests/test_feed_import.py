"""Importer integration tests; DATABASE_URL must select a disposable database."""

import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import psycopg

from piezalink.cli.commands import build_parser
from piezalink.core.feed_import import (
    FeedValidationError, LargeRemovalsError, _validated_rows, import_feed,
)
from piezalink.core.rovira_db import init_db


class ParsingTests(unittest.TestCase):
    def test_normalization_and_reordered_headers(self):
        rows = list(_validated_rows(io.StringIO(
            "title,store_id,price,product_id\n Part , 2 | 1 | 1 ,12.50, 00123 \n"
        )))
        self.assertEqual(rows[0][1], "00123")
        self.assertEqual(rows[0][2], "Part")
        self.assertEqual(rows[0][4], [1, 2])

    def test_invalid_record_width_header_and_quoting(self):
        for content in (
            "wrong\n", "product_id,title,price,store_id\na,Part,1\n",
            'product_id,title,price,store_id\na,"unterminated,1,1',
        ):
            with self.subTest(content=content), self.assertRaises(FeedValidationError):
                list(_validated_rows(io.StringIO(content)))

    def test_diagnostics_are_bounded(self):
        text = "product_id,title,price,store_id\n" + "a,Part,FREE,1\n" * 100
        with self.assertRaises(FeedValidationError) as failure:
            list(_validated_rows(io.StringIO(text)))
        self.assertEqual(len(failure.exception.errors), 50)
        self.assertEqual(failure.exception.error_count, 100)

    def test_command_parser_and_help(self):
        args = build_parser().parse_args(["import", "feed.csv", "--allow-large-removals"])
        self.assertEqual(args.feed, "feed.csv")
        self.assertTrue(args.allow_large_removals)
        result = subprocess.run(
            [sys.executable, "-m", "piezalink", "import", "--help"], capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("--allow-large-removals", result.stdout)

    def test_menu_returns_after_error_and_cancel(self):
        from piezalink.cli.menu import run_menu
        with patch("builtins.input", side_effect=["1", "/nonexistent/feed.csv", "0"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(run_menu(), 0)
        self.assertIn("Import failed", output.getvalue())
        self.assertEqual(output.getvalue().count("1. Import catalogue"), 2)
        with patch("builtins.input", side_effect=["1", "", "0"]), \
                patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(run_menu(), 0)

    def test_cli_interruption_returns_failure(self):
        from piezalink.__main__ import main
        with patch("piezalink.cli.commands.import_feed", side_effect=KeyboardInterrupt), \
                patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["import", "feed.csv"]), 130)


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "Set DATABASE_URL to a disposable database")
class FeedImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = init_db()

    def setUp(self):
        with self.db.transaction() as conn:
            conn.execute("TRUNCATE catalogue_accounts, catalogue, accounts RESTART IDENTITY CASCADE")
        for account in (1, 2, 3):
            self.db.upsert_account(account, f"token-{account}")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "feed.csv"

    def feed(self, rows, *, bom=False):
        with self.path.open("w", encoding="utf-8-sig" if bom else "utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["product_id", "title", "price", "store_id"])
            writer.writerows(rows)
        return self.path

    def snapshot(self):
        with self.db.transaction() as conn:
            return (
                conn.execute("SELECT * FROM catalogue ORDER BY id").fetchall(),
                conn.execute("SELECT * FROM catalogue_accounts ORDER BY catalogue_id, account_id").fetchall(),
            )

    def test_first_import_then_identical_reimport(self):
        path = self.feed([("001", "Part", "12.50", "2|1|1")], bom=True)
        result = import_feed(path)
        self.assertEqual(result.added, 1)
        before = self.snapshot()
        row = before[0][0]
        self.assertEqual(row["product_id"], "001")
        self.assertEqual(row["status"], "unlisted")
        self.assertEqual(row["created_at"], row["modified_at"])
        self.feed([("001", "Part", "12.50", "1|2")])
        result = import_feed(path)
        self.assertEqual(result.unchanged, 1)
        self.assertEqual(before, self.snapshot())

    def test_changed_data_and_accounts_keep_remote_references(self):
        key = self.db.create_listing("p", "Old", "1.00", [1, 2], status="listed")
        self.db.update_listing_id(key, 1, "L1")
        self.db.update_listing_id(key, 2, "L2")
        before = self.db.get_listing(key)
        result = import_feed(self.feed([("p", "New", "2.50", "2|3")]))
        after = self.db.get_listing(key)
        self.assertEqual(result.modified, 1)
        self.assertEqual(after["status"], "modified")
        self.assertEqual(after["created_at"], before["created_at"])
        self.assertGreater(after["modified_at"], before["modified_at"])
        self.assertEqual(after["accounts"], [
            {"account_id": 2, "listing_id": "L2"}, {"account_id": 3, "listing_id": None},
        ])
        self.assertEqual(after["pending_removals"], [{"account_id": 1, "listing_id": "L1"}])
        stable = self.snapshot()
        import_feed(self.path)
        self.assertEqual(stable, self.snapshot())

    def test_absent_products_and_manual_entries(self):
        listed = self.db.create_listing("listed", "Part", "1", [1], status="listed")
        modified = self.db.create_listing("modified", "Part", "1", [1], status="modified")
        unlisted = self.db.create_listing("unlisted", "Part", "1", [1])
        ambiguous = self.db.create_listing("ambiguous", "Part", "1", [1])
        self.db.update_listing_id(ambiguous, 1, "L9")
        sold = self.db.create_listing("sold", "Part", "1", [1], status="sold")
        manual = self.db.create_listing(None, "Manual", "1", [1], listed_manually=True)
        manual_before = self.db.get_listing(manual)
        sold_before = self.db.get_listing(sold)
        result = import_feed(self.feed([("new", "Part", "1", "1")]), allow_large_removals=True)
        self.assertEqual(result.sold, 3)
        self.assertEqual(result.removed, 1)
        self.assertIsNone(self.db.get_listing(unlisted))
        for key in (listed, modified, ambiguous):
            self.assertEqual(self.db.get_listing(key)["status"], "sold")
        self.assertEqual(self.db.get_listing(manual), manual_before)
        self.assertEqual(self.db.get_listing(sold), sold_before)

    def test_reappearing_sold_and_changed_unlisted_become_modified(self):
        sold = self.db.create_listing("sold", "Part", "1", [], status="sold")
        pending = self.db.create_listing("pending", "Part", "1", [1])
        result = import_feed(self.feed([("sold", "Part", "1", "1"), ("pending", "Changed", "1", "1")]))
        self.assertEqual(result.modified, 2)
        for key in (sold, pending):
            self.assertEqual(self.db.get_listing(key)["status"], "modified")

    def test_large_removal_guard_and_exact_boundary(self):
        for number in range(5):
            self.db.create_listing(str(number), "Part", "1", [1])
        import_feed(self.feed((str(n), "Part", "1", "1") for n in range(4)))
        before = self.snapshot()
        with self.assertRaises(LargeRemovalsError) as failure:
            import_feed(self.feed([("0", "Part", "1", "1")]))
        self.assertEqual(failure.exception.missing, 3)
        self.assertEqual(before, self.snapshot())
        import_feed(self.path, allow_large_removals=True)

    def test_invalid_inputs_never_change_catalogue(self):
        self.db.create_listing("existing", "Part", "1", [1])
        before = self.snapshot()
        feeds = (
            [], [("x", "Part", "1", "999")],
            [("x", "Part", "1", "1"), (" x ", "Part", "1", "1")],
            [("x", "a" * 81, "1", "1")], [("", "Part", "1", "1")],
            [("x", "Part", "1.000", "1")], [("x", "Part", "0", "1")],
            [("x", "Part", "1", "1;2")], [("x", "Part", "1", "1||2")],
        )
        for rows in feeds:
            with self.subTest(rows=rows), self.assertRaises(FeedValidationError):
                import_feed(self.feed(rows), allow_large_removals=True)
            self.assertEqual(before, self.snapshot())

    def test_manual_product_collision_is_rejected(self):
        self.db.create_listing("manual", "Manual", "1", [1], listed_manually=True)
        before = self.snapshot()
        with self.assertRaises(FeedValidationError):
            import_feed(self.feed([("manual", "Changed", "2", "1")]))
        self.assertEqual(before, self.snapshot())

    def test_sql_failure_rolls_back_prior_changes(self):
        self.db.create_listing("old", "Old", "1", [1])
        before = self.snapshot()
        with self.db.transaction() as conn:
            conn.execute("""
                CREATE OR REPLACE FUNCTION reject_test_account() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN RAISE EXCEPTION 'injected failure'; END; $$;
                CREATE TRIGGER reject_test_account BEFORE INSERT ON catalogue_accounts
                FOR EACH ROW WHEN (NEW.account_id = 3) EXECUTE FUNCTION reject_test_account();
            """)
        try:
            with self.assertRaises(psycopg.Error):
                import_feed(self.feed([("old", "Updated", "2", "1"), ("new", "Part", "1", "3")]))
            self.assertEqual(before, self.snapshot())
        finally:
            with self.db.transaction() as conn:
                conn.execute("DROP FUNCTION reject_test_account() CASCADE")

    def test_second_process_fails_promptly_and_releases_lock(self):
        path = self.feed([("p", "Part", "1", "1")])
        with self.db.transaction() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(739271002)")
            result = subprocess.run(
                [sys.executable, "-m", "piezalink", "import", str(path)],
                capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("Another import is running", result.stderr)
        self.assertEqual(import_feed(path).added, 1)

    def test_timestamp_helpers_and_pending_account_changes(self):
        key = self.db.create_listing("p", "Part", "1", [1, 2])
        before = self.db.get_listing(key)
        self.db.update_price(key, "1.00")
        self.db.update_account(key, [2, 1])
        self.assertEqual(before, self.db.get_listing(key))
        self.db.update_listing_id(key, 1, "L1")
        self.db.update_account(key, [2])
        after = self.db.get_listing(key)
        self.assertGreater(after["modified_at"], before["modified_at"])
        self.assertEqual(after["pending_removals"], [{"account_id": 1, "listing_id": "L1"}])
        self.db.update_account(key, [1, 2])
        self.assertEqual(self.db.get_listing(key)["pending_removals"], [])

    def test_interrupted_stream_rolls_back_and_unlocks(self):
        before = self.snapshot()
        def interrupted():
            yield (2, "p", "Part", 1, [1])
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.db.import_catalogue(interrupted())
        self.assertEqual(before, self.snapshot())
        self.assertEqual(import_feed(self.feed([("p", "Part", "1", "1")])).added, 1)

    def test_streaming_larger_catalogue(self):
        path = self.feed((f"p{n:06}", "Part", "1.25", "1|2") for n in range(5000))
        result = import_feed(path)
        self.assertEqual(result.added, 5000)
        self.assertEqual(result.total, 5000)

    def test_supplied_feeds_are_rejected_without_partial_changes(self):
        directory = Path(__file__).resolve().parent.parent / "data"
        if not directory.is_dir():
            self.skipTest("Repository data fixtures are not mounted")
        before = self.snapshot()
        for name in ("feed_dia1.csv", "feed_dia2.csv"):
            with self.subTest(name=name), self.assertRaises(FeedValidationError):
                import_feed(directory / name, allow_large_removals=True)
            self.assertEqual(before, self.snapshot())

    def test_menu_import_success_and_explicit_override(self):
        from piezalink.cli.menu import run_menu
        for number in range(5):
            self.db.create_listing(str(number), "Part", "1", [1])
        path = self.feed([("0", "Part", "1", "1")])
        with patch("builtins.input", side_effect=["1", str(path), "n", "0"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            before = self.snapshot()
            run_menu()
            self.assertEqual(before, self.snapshot())
        self.assertIn("Import cancelled", output.getvalue())
        with patch("builtins.input", side_effect=["1", str(path), "y", "0"]), \
                patch("sys.stdout", new_callable=io.StringIO) as output:
            run_menu()
        self.assertIn("4 removed", output.getvalue())

    def test_cli_success_and_validation_failure(self):
        path = self.feed([("p", "Part", "1", "1")])
        result = subprocess.run(
            [sys.executable, "-m", "piezalink", "import", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 added", result.stdout)
        before = self.snapshot()
        self.feed([("p", "Part", "FREE", "1")])
        result = subprocess.run(
            [sys.executable, "-m", "piezalink", "import", str(path)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("Record 2", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_upgrade_legacy_schema_preserves_data(self):
        with self.db.transaction() as conn:
            conn.execute("""
                DROP TABLE catalogue_accounts, catalogue, accounts, piezalink_schema CASCADE;
                CREATE TABLE accounts (account_id INTEGER PRIMARY KEY, token TEXT NOT NULL);
                CREATE TABLE catalogue (
                    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    product_id TEXT UNIQUE CHECK (btrim(product_id) <> ''),
                    title VARCHAR(80) NOT NULL CHECK (btrim(title) <> ''),
                    price NUMERIC(7,2) NOT NULL CHECK (price > 0 AND price <= 99999.99),
                    status TEXT NOT NULL DEFAULT 'unlisted'
                        CHECK (status IN ('unlisted','listed','sold')),
                    listed_manually BOOLEAN NOT NULL DEFAULT FALSE,
                    CHECK (listed_manually OR product_id IS NOT NULL)
                );
                CREATE TABLE catalogue_accounts (
                    catalogue_id BIGINT REFERENCES catalogue(id) ON DELETE CASCADE,
                    account_id INTEGER REFERENCES accounts(account_id), listing_id TEXT,
                    PRIMARY KEY (catalogue_id, account_id), UNIQUE (account_id, listing_id)
                );
                INSERT INTO accounts VALUES (1, 'legacy-token');
                INSERT INTO catalogue(product_id,title,price,status) VALUES ('legacy','Part',1,'listed');
                INSERT INTO catalogue_accounts VALUES (1,1,'L1');
            """)
        self.db.close()
        type(self).db = init_db()
        item = self.db.get_listing(1)
        self.assertEqual(item["product_id"], "legacy")
        self.assertEqual(item["accounts"], [{"account_id": 1, "listing_id": "L1"}])
        self.assertEqual(item["created_at"], item["modified_at"])
        self.assertEqual(import_feed(self.feed([("legacy", "Updated", "1", "1")])).modified, 1)
        self.db.close()
        type(self).db = init_db()
        self.assertEqual(self.db.get_listing(1)["status"], "modified")


if __name__ == "__main__":
    unittest.main()

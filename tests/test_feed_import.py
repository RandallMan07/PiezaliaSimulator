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

    def test_decimal_normalizations_stack_without_rounding(self):
        from decimal import Decimal
        for price in ("9,950", "9.9500", "9,95", "9.95"):
            with self.subTest(price=price):
                text = f'product_id,title,price,store_id\np,Part,"{price}",3\n'
                self.assertEqual(list(_validated_rows(io.StringIO(text)))[0][3], Decimal("9.95"))
        for price in ("9,951", "1.234,50", "45.90 €", "-5", "0", "100000"):
            with self.subTest(price=price), self.assertRaises(FeedValidationError):
                list(_validated_rows(io.StringIO(f'product_id,title,price,store_id\np,Part,"{price}",3\n')))

    def test_unquoted_decimal_comma_reconstruction(self):
        from decimal import Decimal
        from piezalink.core.feed_import import FeedRows
        for header, record in (
            ("product_id,title,price,store_id", 'p,"Part, red",9,950,3'),
            ("title,price,product_id,store_id", "Part,9,950,p,3"),
            ("product_id,title,store_id,price", "p,Part,3,9,950"),
        ):
            with self.subTest(header=header):
                rows = FeedRows(io.StringIO(header + "\n" + record + "\n"))
                values = list(rows)
                rows.report.raise_if_errors()
                self.assertEqual(values[0][1], "p")
                self.assertEqual(values[0][3], Decimal("9.95"))
                self.assertEqual(values[0][4], [3])
                self.assertEqual(rows.report.reconstructed_prices, 1)

    def test_reconstruction_rejects_ambiguous_or_extra_fields(self):
        for record in (
            "p,Part,1,00,3,3", "p,Part,9,951,3", "p,Part,red,9,3",
            "p,Part,1,00,1;3", "p,Part,1.00,3,extra",
        ):
            with self.subTest(record=record), self.assertRaises(FeedValidationError):
                list(_validated_rows(io.StringIO("product_id,title,price,store_id\n" + record)))

    def test_blank_lines_are_ignored_but_empty_records_are_invalid(self):
        from piezalink.core.feed_import import FeedRows
        rows = FeedRows(io.StringIO("\nproduct_id,title,price,store_id\n  \t\np,Part,1,1\n\n"))
        values = list(rows)
        rows.report.raise_if_errors()
        self.assertEqual(rows.report.skipped_blank_lines, 3)
        self.assertEqual(values[0][0], 4)
        with self.assertRaises(FeedValidationError):
            list(_validated_rows(io.StringIO("product_id,title,price,store_id\n,,,\n")))

    def test_errors_use_physical_start_lines_for_multiline_records(self):
        text = 'product_id,title,price,store_id\np,"Part\ncontinued",FREE,1\nq,Part,FREE,1\n'
        with self.assertRaises(FeedValidationError) as failure:
            list(_validated_rows(io.StringIO(text)))
        self.assertIn("Line 2, product 'p'", str(failure.exception))
        self.assertIn("Line 4, product 'q'", str(failure.exception))

    def test_reports_multiple_field_errors_and_rejects_nul_before_copy(self):
        text = "product_id,title,price,store_id\np,,FREE,1;2\nq,Part\x00,1,1\n"
        with self.assertRaises(FeedValidationError) as failure:
            list(_validated_rows(io.StringIO(text)))
        message = str(failure.exception)
        for expected in ("title must not be empty", "price must be numeric", "separated by |", "NUL"):
            self.assertIn(expected, message)


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
            [("x", "Part", "1", "1"), (" x ", "Different", "1", "1")],
            [("x", "   ", "1", "1")], [("", "Part", "1", "1")],
            [("x", "Part", "1.001", "1")], [("x", "Part", "0", "1")],
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

    def test_full_titles_survive_import_changes_and_identical_reimport(self):
        title = ("Full catalogue title " * 20).strip()
        path = self.feed([("p", title, "1", "1")])
        self.assertEqual(import_feed(path).added, 1)
        before = self.snapshot()
        self.assertEqual(before[0][0]["title"], title)
        self.assertEqual(import_feed(path).unchanged, 1)
        self.assertEqual(before, self.snapshot())
        changed = title + "changed"
        self.feed([("p", changed, "1", "1")])
        self.assertEqual(import_feed(path).modified, 1)
        after = self.snapshot()[0][0]
        self.assertEqual(after["title"], changed)
        self.assertEqual(after["status"], "modified")

    def test_day_one_preserves_full_titles_and_invalid_day_two_rolls_back(self):
        directory = Path(__file__).resolve().parent.parent / "data"
        if not directory.is_dir():
            self.skipTest("Repository data fixtures are not mounted")
        self.db.upsert_account(4, "token-4")
        result = import_feed(directory / "feed_dia1.csv")
        self.assertEqual(result.added, 600)
        with (directory / "feed_dia1.csv").open(encoding="utf-8-sig", newline="") as stream:
            expected = {r["product_id"]: r["title"].strip() for r in csv.DictReader(stream)}
        rows = self.snapshot()[0]
        self.assertTrue(any(len(row["title"]) > 80 for row in rows))
        for row in rows:
            self.assertEqual(row["title"], expected[row["product_id"]])
        before = self.snapshot()
        with self.assertRaises(FeedValidationError):
            import_feed(directory / "feed_dia2.csv", allow_large_removals=True)
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
        self.assertIn("Line 2", result.stderr)
        self.assertEqual(before, self.snapshot())

    def test_upgrade_legacy_schema_preserves_data(self):
        with self.db.transaction() as conn:
            conn.execute("""
                DROP TABLE sync_operations, sync_runs, accepted_feeds, catalogue_accounts, catalogue, accounts, piezalink_schema CASCADE;
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

    def test_exact_duplicates_merge_after_normalization(self):
        from piezalink.cli.commands import format_import_result
        rows = [(" p ", " Part ", "9,950", "2|1|1"), ("p", "Part", "9.95", "1|2")]
        result = import_feed(self.feed(rows))
        self.assertEqual(result.total, 1)
        self.assertEqual(result.added, 1)
        self.assertEqual(result.merged_duplicates, 1)
        self.assertIn("1 identical duplicates merged", format_import_result(result))
        before = self.snapshot()
        self.assertEqual(import_feed(self.path).unchanged, 1)
        self.assertEqual(before, self.snapshot())

    def test_blank_lines_and_unquoted_comma_are_reported(self):
        from decimal import Decimal
        from piezalink.cli.commands import format_import_result
        self.path.write_text("product_id,title,price,store_id\n\n  \np,Part,9,950,3\n")
        result = import_feed(self.path)
        self.assertEqual(result.skipped_blank_lines, 2)
        self.assertEqual(result.reconstructed_prices, 1)
        self.assertEqual(self.snapshot()[0][0]["price"], Decimal("9.95"))
        self.assertIn("2 blank lines skipped", format_import_result(result))
        self.assertIn("1 decimal-comma prices reconstructed", format_import_result(result))

    def test_all_validation_phases_report_before_rollback(self):
        self.db.create_listing("manual", "Manual", "1", [1], listed_manually=True)
        before = self.snapshot()
        rows = [
            ("p", "Part", "FREE", "7"), ("dup", "Part", "1", "1"),
            ("dup", "Part", "2", "1"), ("manual", "Changed", "1", "1"),
            ("q", "", "0", "1;3"),
        ]
        with self.assertRaises(FeedValidationError) as failure:
            import_feed(self.feed(rows), allow_large_removals=True)
        for expected in ("unknown account 7", "conflicting duplicate", "manual entry", "title must not be empty", "separated by |"):
            self.assertIn(expected, str(failure.exception))
        self.assertEqual(before, self.snapshot())

    def test_conflicting_duplicates_report_two_different_variants(self):
        rows = [("p", "Part", "1", "1"), ("p", "Part", "1.00", "1"), ("p", "Part", "2", "1")]
        with self.assertRaises(FeedValidationError) as failure:
            import_feed(self.feed(rows))
        self.assertIn("Lines 2 and 4", str(failure.exception))
        self.assertFalse(self.snapshot()[0])

    def test_only_blank_lines_never_remove_catalogue(self):
        self.db.create_listing("p", "Part", "1", [1])
        before = self.snapshot()
        self.path.write_text("product_id,title,price,store_id\n\n  \n")
        with self.assertRaises(FeedValidationError):
            import_feed(self.path, allow_large_removals=True)
        self.assertEqual(before, self.snapshot())

    def test_version_one_title_migration_preserves_existing_data(self):
        key = self.db.create_listing("existing", "Original title", "1.25", [1])
        self.db.update_listing_id(key, 1, "L1")
        before = self.db.get_listing(key)
        with self.db.transaction() as conn:
            conn.execute("ALTER TABLE catalogue ALTER COLUMN title TYPE VARCHAR(80)")
            conn.execute("UPDATE piezalink_schema SET version = 1")
        self.db.close()
        type(self).db = init_db()
        self.assertEqual(self.db.get_listing(key), before)
        title = "A long title after migrating " * 20
        self.db.update_title(key, title)
        self.assertEqual(self.db.get_listing(key)["title"], title)
        with self.db.transaction() as conn:
            self.assertEqual(conn.execute("SELECT version FROM piezalink_schema").fetchone()["version"], 5)


if __name__ == "__main__":
    unittest.main()

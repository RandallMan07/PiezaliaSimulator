"""Bootstrap integration tests; DATABASE_URL must select a disposable database."""

import os
import tempfile
import unittest
from pathlib import Path

import psycopg

from piezalink.core.config import get_accounts_path, get_database_url
from piezalink.core.rovira_db import init_db


@unittest.skipUnless(os.environ.get("DATABASE_URL"), "Set DATABASE_URL to a disposable database")
class AccountBootstrapTests(unittest.TestCase):
    def setUp(self):
        db = init_db()
        with db.transaction() as conn:
            conn.execute("TRUNCATE catalogue_accounts, catalogue, accounts RESTART IDENTITY CASCADE")
            conn.execute("UPDATE piezalink_schema SET version = 2")
        db.close()
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "accounts.csv"
        self.path.write_text("account_id,token\n1,initial-one\n2,initial-two\n")

    def tearDown(self):
        # Restore a usable default interface even after intentionally failed setup.
        db = init_db()
        db.close()
        self.temp.cleanup()

    def rows(self, db):
        with db.transaction() as conn:
            return conn.execute("SELECT account_id, token FROM accounts ORDER BY account_id").fetchall()

    def test_fresh_schema_loads_accounts_without_manual_registration(self):
        with psycopg.connect(get_database_url(), autocommit=True) as conn:
            conn.execute("DROP TABLE sync_operations, sync_runs, accepted_feeds, catalogue_accounts, catalogue, accounts, piezalink_schema CASCADE")
        db = init_db(accounts_path=self.path)
        self.assertEqual(self.rows(db), [
            {"account_id": 1, "token": "initial-one"},
            {"account_id": 2, "token": "initial-two"},
        ])
        db.create_listing("p", "Part", "1", [1, 2])

    def test_default_file_loads_all_supplied_accounts(self):
        path = get_accounts_path()
        self.assertTrue(path.is_file())
        db = init_db()
        self.assertEqual([row["account_id"] for row in self.rows(db)], [1, 2, 3, 4])

    def test_existing_tokens_are_preserved_and_bootstrap_does_not_repeat(self):
        with psycopg.connect(get_database_url(), autocommit=True) as conn:
            conn.execute("INSERT INTO accounts VALUES (1, 'renewed-token')")
        db = init_db(accounts_path=self.path)
        self.assertEqual(self.rows(db), [
            {"account_id": 1, "token": "renewed-token"},
            {"account_id": 2, "token": "initial-two"},
        ])
        db.close()
        self.path.write_text("account_id,token\n1,replaced\n3,new-account\n")
        db = init_db(accounts_path=self.path)
        self.assertEqual(self.rows(db), [
            {"account_id": 1, "token": "renewed-token"},
            {"account_id": 2, "token": "initial-two"},
        ])

    def test_invalid_seed_rolls_back_schema_upgrade_and_account_inserts(self):
        cases = (
            "account_id,token\n1,valid\n2,\n",
            "account_id,token\n1,first\n1,duplicate\n",
            "account_id,token\n",
            'account_id,token\n1,"unterminated',
        )
        for text in cases:
            self.path.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                init_db(accounts_path=self.path)
            with psycopg.connect(get_database_url()) as conn:
                self.assertEqual(conn.execute("SELECT version FROM piezalink_schema").fetchone(), (2,))
                self.assertEqual(conn.execute("SELECT count(*) FROM accounts").fetchone(), (0,))

    def test_missing_seed_does_not_complete_initialization(self):
        with self.assertRaises(FileNotFoundError):
            init_db(accounts_path=self.path.parent / "missing.csv")
        with psycopg.connect(get_database_url()) as conn:
            self.assertEqual(conn.execute("SELECT version FROM piezalink_schema").fetchone(), (2,))

    def test_later_schema_upgrades_do_not_repeat_account_bootstrap(self):
        db = init_db(accounts_path=self.path)
        before = self.rows(db)
        with db.transaction() as conn:
            conn.execute("UPDATE piezalink_schema SET version = 3")
        db.close()
        db = init_db(accounts_path=self.path.parent / "missing.csv")
        self.assertEqual(self.rows(db), before)

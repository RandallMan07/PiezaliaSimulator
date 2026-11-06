"""Configuration priority tests; no running database required."""

import os
import tempfile
import unittest
from contextlib import chdir
from pathlib import Path
from unittest.mock import patch

from piezalink.core.config import get_accounts_path, get_database_url


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_dotenv_is_found_from_child_directory(self):
        (self.root / ".env").write_text("DATABASE_URL=postgresql://localhost/from_file\n")
        child = self.root / "child"
        child.mkdir()
        with patch.dict(os.environ, {}, clear=True), chdir(child):
            self.assertEqual(get_database_url(), "postgresql://localhost/from_file")
            self.assertNotIn("DATABASE_URL", os.environ)

    def test_explicit_dsn_overrides_environment(self):
        with patch.dict(os.environ, {"DATABASE_URL": "environment"}, clear=True):
            self.assertEqual(get_database_url("explicit"), "explicit")

    def test_environment_overrides_dotenv(self):
        (self.root / ".env").write_text("DATABASE_URL=from_file\n")
        with patch.dict(os.environ, {"DATABASE_URL": "environment"}, clear=True), chdir(self.root):
            self.assertEqual(get_database_url(), "environment")

    def test_empty_environment_falls_back_to_dotenv(self):
        (self.root / ".env").write_text("DATABASE_URL=from_file\n")
        with patch.dict(os.environ, {"DATABASE_URL": ""}, clear=True), chdir(self.root):
            self.assertEqual(get_database_url(), "from_file")

    def test_missing_value_has_actionable_error(self):
        with patch.dict(os.environ, {}, clear=True), patch(
            "piezalink.core.config.find_dotenv", return_value="",
        ), self.assertRaisesRegex(ValueError, "DATABASE_URL"):
            get_database_url()


    def test_accounts_path_can_be_explicit(self):
        self.assertEqual(get_accounts_path(self.root / "custom.csv"), self.root / "custom.csv")

    def test_accounts_path_environment_and_relative_dotenv(self):
        (self.root / ".env").write_text("ACCOUNTS_CSV=seeds/accounts.csv\n")
        child = self.root / "child"
        child.mkdir()
        with patch.dict(os.environ, {}, clear=True), chdir(child):
            self.assertEqual(get_accounts_path(), self.root / "seeds/accounts.csv")
        with patch.dict(os.environ, {"ACCOUNTS_CSV": "/custom/accounts.csv"}, clear=True), chdir(child):
            self.assertEqual(get_accounts_path(), Path("/custom/accounts.csv"))

    def test_accounts_file_is_found_above_working_directory(self):
        (self.root / "accounts.csv").write_text("account_id,token\n1,test\n")
        child = self.root / "child"
        child.mkdir()
        with patch.dict(os.environ, {}, clear=True), chdir(child):
            self.assertEqual(get_accounts_path(), self.root / "accounts.csv")

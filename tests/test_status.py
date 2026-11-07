"""Support explanations, CLI routing, and database status snapshots."""

import io
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from piezalink.core.status import account_state, product_status
from piezalink.cli.status import format_product_status
from piezalink.__main__ import main


def sample():
    account = dict(account_id=4, desired=True, listing_id=None, synced_revision=0,
                   last_synced_at=None, last_attempt_at=None, account_checked_at=None,
                   account_error='HTTP 401: expired token', listing_error=None,
                   last_warning=None, operation_action=None, unresolved=False)
    return dict(product_id='00123', title='A long product title ' * 8,
                price=Decimal('19.50'), status='unlisted', listed_manually=False,
                desired_revision=2, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                modified_at=None, accounts=[account])


class StatusTests(unittest.TestCase):
    def test_account_failure_explains_unlisted_product(self):
        product = sample()
        output = format_product_status(product)
        for value in ('00123', '19.50', 'ACCOUNT 4', 'Blocked by account error',
                      'HTTP 401: expired token', 'Never recorded', 'UTC'):
            self.assertIn(value, output)
        self.assertTrue(all(len(line) <= 76 for line in output.splitlines()))

    def test_pending_update_retirement_uncertainty_and_manual(self):
        product = sample()
        account = product['accounts'][0]
        account.update(account_error=None, listing_id='remote-1', synced_revision=2)
        self.assertEqual(account_state(product, account), 'In sync')
        account['synced_revision'] = 1
        self.assertIn('Pending update', account_state(product, account))
        account['desired'] = False
        self.assertIn('Pending retirement', account_state(product, account))
        account['unresolved'] = True
        self.assertIn('Awaiting verification', account_state(product, account))
        product['listed_manually'] = True
        self.assertIn('Manual listing', account_state(product, account))

    def test_latest_sync_does_not_claim_all_accounts_are_synced(self):
        product = sample()
        product['accounts'][0]['last_synced_at'] = datetime(2026, 2, 1, tzinfo=timezone.utc)
        output = format_product_status(product)
        self.assertIn('2026-02-01 00:00:00 UTC', output)
        self.assertIn('confirmation in any account', output)
        self.assertIn('Blocked by account error', output)

    def test_report_sanitizes_terminal_control_characters(self):
        product = sample()
        product['title'] = 'Title\x1b[31m\nInjected'
        self.assertNotIn('\x1b', format_product_status(product))

    def test_cli_preserves_sku_and_returns_failure_for_missing_product(self):
        with patch('piezalink.core.status.product_status', return_value=sample()) as service, redirect_stdout(io.StringIO()):
            self.assertEqual(main(['status', '00123']), 0)
            service.assert_called_once_with('00123')
        with patch('piezalink.core.status.product_status', side_effect=LookupError('Not found')), redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main(['status', 'missing']), 1)
            self.assertIn('Not found', output.getvalue())

    def test_menu_lookup_and_blank_return(self):
        from piezalink.cli.menu import run_menu
        with patch('builtins.input', side_effect=['3', '00123', '3', '', '0']), \
                patch('piezalink.core.status.product_status', return_value=sample()) as service, \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(run_menu(), 0)
            service.assert_called_once_with('00123')
            self.assertIn('Product status', output.getvalue())

    def test_empty_id_rejected_before_opening_database(self):
        with self.assertRaises(ValueError):
            product_status(' ')

    def test_retirement_history_preserves_sync_and_attempt_after_link_cleanup(self):
        product = sample()
        product['status'] = 'sold'
        account = product['accounts'][0]
        confirmed = datetime(2026, 11, 7, 16, 40, 37, tzinfo=timezone.utc)
        account.update(desired=None, account_error=None, operation_action='DELETE',
                       operation_state='confirmed', operation_at=confirmed,
                       confirmed_at=confirmed, attempted_at=confirmed,
                       operation_error=None, operation_listing_id='L5')
        output = format_product_status(product)
        self.assertIn('Retired — removal confirmed by portal', output)
        self.assertIn('2026-11-07 16:40:37 UTC', output)
        self.assertIn('Operation listing ID L5', output)
        self.assertNotIn('Never recorded', output.split('Last sync', 1)[1].splitlines()[0])

    def test_unrelated_account_error_does_not_imply_product_is_blocked(self):
        product = sample()
        account = product['accounts'][0]
        account['desired'] = None
        self.assertEqual(account_state(product, account),
                         'Account error — no product association recorded')

    def test_only_memberships_history_and_account_errors_are_shown(self):
        product = sample()
        rows = []
        for number in (1, 2, 3, 4):
            row = dict(product, **product['accounts'][0])
            row.update(account_id=number, desired=None, account_error=None)
            rows.append(row)
        rows[1]['desired'] = False
        rows[2]['operation_action'] = 'DELETE'
        rows[3]['account_error'] = 'Expired token'
        db = MagicMock()
        conn = db.transaction.return_value.__enter__.return_value
        conn.execute.return_value.fetchall.return_value = rows
        result = product_status('00123', db=db)
        self.assertEqual([a['account_id'] for a in result['accounts']], [2, 3, 4])

    def test_sold_product_with_retained_reference_is_still_pending(self):
        product = sample()
        product['status'] = 'sold'
        account = product['accounts'][0]
        account.update(desired=False, listing_id='still-present')
        self.assertIn('Pending retirement', account_state(product, account))
        account['listing_id'] = None
        self.assertIn('Retirement unverified', account_state(product, account))

"""Sync integration tests require a disposable database AND simulator.

Set PIEZALINK_SYNC_INTEGRATION=1 explicitly: tests reset the simulator and tables.
"""

import csv
import io
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import httpx
import psycopg

from piezalink.core.config import SyncSettings
from piezalink.core.feed_import import import_feed
from piezalink.core.portal import PortalClient, PortalError
from piezalink.core.rovira_db import init_db
from piezalink.core.sync import SyncError, sync_catalogue


class SyncClientTests(unittest.TestCase):
    def test_retry_after_is_respected_and_bounded(self):
        client = PortalClient(SyncSettings(retry_base=0), sleep=lambda seconds: None)
        self.addCleanup(client.close)
        error = PortalError('slow down', status=429, retry_after=5)
        self.assertEqual(client.delay(error, 0, 10), 5)
        self.assertIsNone(client.delay(error, 0, 4))

    def test_read_retries_are_bounded_and_never_follow_redirects(self):
        count = []
        def handler(request):
            count.append(request)
            return httpx.Response(503, json={'error': 'unavailable', 'detail': 'retry'})
        client = PortalClient(SyncSettings(attempts=2, retry_base=0),
                              transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
        self.addCleanup(client.close)
        with self.assertRaises(PortalError):
            client.listings(1, 'token')
        self.assertEqual(len(count), 2)

    def test_sync_command_help_and_interruption(self):
        result = subprocess.run([sys.executable, '-m', 'piezalink', 'sync', '--help'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0)
        from piezalink.__main__ import main
        with patch('piezalink.core.sync.sync_catalogue', side_effect=KeyboardInterrupt), \
                patch('sys.stderr'):
            self.assertEqual(main(['sync']), 130)

    def test_menu_runs_shared_service_and_returns_after_failure(self):
        from piezalink.cli.menu import run_menu
        from piezalink.core.sync import SyncResult
        for response in (SyncResult(run_id=1), SyncError('try later')):
            kwargs = {'side_effect': response} if isinstance(response, Exception) else {'return_value': response}
            with patch('piezalink.core.sync.sync_catalogue', **kwargs) as service, \
                    patch('builtins.input', side_effect=['2', '0']), \
                    patch('sys.stdout', new_callable=io.StringIO) as output:
                self.assertEqual(run_menu(), 0)
            service.assert_called_once()
            self.assertEqual(output.getvalue().count('2. Sync catalogue'), 2)

    def test_sync_settings_validate_retry_limits_and_api_url(self):
        from piezalink.core.config import get_sync_settings
        with patch('piezalink.core.config.find_dotenv', return_value=''):
            for values in ({'SYNC_RETRY_ATTEMPTS': '0'}, {'PORTAL_TIMEOUT_SECONDS': 'nan'},
                           {'PORTAL_BASE_URL': 'http://portal:bad'}, {'PORTAL_BASE_URL': 'ftp://portal'}):
                with self.subTest(values=values), patch.dict(os.environ, values, clear=True), self.assertRaises(ValueError):
                    get_sync_settings()


@unittest.skipUnless(os.environ.get('PIEZALINK_SYNC_INTEGRATION') == '1', 'Requires isolated Postgres and simulator')
class SyncIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = init_db()
        cls.api_url = os.environ.get('PORTAL_BASE_URL', 'http://portal:8080')
        cls.admin = httpx.Client(base_url=cls.api_url, timeout=10)

    @classmethod
    def tearDownClass(cls):
        cls.admin.close()

    def setUp(self):
        with self.db.transaction() as conn:
            conn.execute('TRUNCATE sync_operations, sync_runs, accepted_feeds, catalogue_accounts, catalogue, accounts RESTART IDENTITY CASCADE')
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'feed.csv'
        self.settings = SyncSettings(api_url=self.api_url, retry_base=0, retry_cap=0, attempts=3, timeout=3)
        self.reset()

    def reset(self, *, seeds=(), failures=None, expired=(), title_limit=80, page_size=2):
        accounts = {str(n): {'token': f'token-{n}', 'expired': n in expired} for n in (1, 2)}
        response = self.admin.post('/_admin/reset', json={
            'accounts': accounts, 'seed_listings': list(seeds), 'page_size': page_size,
            'max_title_length': title_limit, 'failures': failures or {},
        })
        response.raise_for_status()
        for n in (1, 2):
            self.db.upsert_account(n, f'token-{n}')

    def feed(self, rows, *, allow=False):
        with self.path.open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['product_id', 'title', 'price', 'store_id'])
            writer.writerows(rows)
        return import_feed(self.path, allow_large_removals=allow)

    def run_sync(self, client=None):
        own = client is None
        client = client or PortalClient(self.settings, sleep=lambda seconds: None)
        try:
            return sync_catalogue(db=self.db, client=client, settings=self.settings)
        finally:
            if own:
                client.close()

    def listings(self):
        return self.admin.get('/_admin/state').json()['listings']

    def calls(self):
        return self.admin.get('/_admin/calls').json()

    def item(self, sku='p'):
        with self.db.transaction() as conn:
            row = conn.execute('SELECT id FROM catalogue WHERE product_id = %s', (sku,)).fetchone()
        return self.db.get_listing(row['id']) if row else None

    def test_requires_an_accepted_feed_before_any_requests(self):
        with self.assertRaises(SyncError):
            self.run_sync()
        self.assertFalse(self.calls())

    def test_adoption_and_idempotency_preserve_manual_ads(self):
        self.reset(seeds=[{'account': 1, 'sku': 'p', 'title': 'Part', 'price': '1.00'},
                          {'account': 1, 'sku': None, 'title': 'Manual', 'price': '2.00'}])
        self.feed([('p', 'Part', '1', '1')])
        before = self.listings()
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.item()['status'], 'listed')
        self.assertEqual(self.item()['accounts'][0]['listing_id'], before[0]['listing_id'])
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.listings(), before)
        self.assertFalse([r for r in self.calls() if r['method'] != 'GET'])

    def test_retire_update_create_order_and_duplicate_cleanup(self):
        self.reset(seeds=[{'account': 1, 'sku': 'old', 'title': 'Old', 'price': '1.00'},
                          {'account': 1, 'sku': 'p', 'title': 'Old title', 'price': '1.00'},
                          {'account': 1, 'sku': 'p', 'title': 'Duplicate', 'price': '1.00'},
                          {'account': 1, 'sku': None, 'title': 'Manual', 'price': '1.00'}])
        self.feed([('p', 'New title', '2', '1'), ('q', 'New', '1', '1')])
        result = self.run_sync()
        self.assertTrue(result.complete)
        self.assertEqual((result.created, result.updated, result.retired), (1, 1, 2))
        writes = [r['method'] for r in self.calls() if r['method'] != 'GET']
        self.assertEqual(writes[0], 'DELETE')
        self.assertEqual(writes[-1], 'POST')
        ads = self.listings()
        self.assertEqual(sum(a['sku'] == 'p' for a in ads), 1)
        self.assertEqual(next(a for a in ads if a['sku'] == 'p')['listing_id'], 'L2')
        self.assertEqual(sum(a['sku'] is None for a in ads), 1)

    def test_full_titles_are_only_truncated_in_publication(self):
        title = 'Long source title ' * 10
        self.feed([('p', title, '1', '1')])
        self.assertEqual(self.run_sync().truncated, 1)
        self.assertEqual(self.item()['title'], title.strip())
        self.assertEqual(self.listings()[0]['title'], title[:80])
        with self.db.transaction() as conn:
            self.assertIsNotNone(conn.execute('SELECT last_warning FROM catalogue_accounts').fetchone()['last_warning'])
        writes = len([r for r in self.calls() if r['method'] != 'GET'])
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(len([r for r in self.calls() if r['method'] != 'GET']), writes)

    def test_expired_account_does_not_block_others_or_repeat_successes(self):
        self.reset(expired=(1,))
        self.feed([('p', 'Part', '1', '1|2')])
        result = self.run_sync()
        self.assertEqual(result.failed_accounts, 1)
        self.assertEqual(self.item()['status'], 'unlisted')
        self.assertEqual([r['account'] for r in self.listings()], [2])
        self.admin.post('/_admin/accounts/1/renew').raise_for_status()
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.item()['status'], 'listed')
        self.assertEqual(sum(r['method'] == 'POST' and r['account'] == 2 for r in self.calls()), 1)

    def test_transient_write_retries_are_bounded_and_other_products_continue(self):
        self.reset(failures={'transient_rules': [{'method': 'POST', 'account': 1, 'sku': 'p', 'times': 10}]})
        self.feed([('p', 'Part', '1', '1'), ('q', 'Other', '1', '1')])
        result = self.run_sync()
        self.assertEqual(result.failed_operations, 1)
        self.assertEqual(sum(r['method'] == 'POST' and r['sku'] == 'p' for r in self.calls()), 3)
        self.assertEqual([r['sku'] for r in self.listings()], ['q'])
        self.assertEqual(self.item('q')['status'], 'listed')
        with self.db.transaction() as conn:
            self.assertIn('503', conn.execute("SELECT last_error FROM catalogue_accounts a JOIN catalogue c ON c.id = a.catalogue_id WHERE product_id = 'p'").fetchone()['last_error'])

    def test_422_is_not_retried(self):
        self.reset(title_limit=3)
        self.feed([('p', 'Too long', '1', '1')])
        result = self.run_sync()
        self.assertEqual(result.failed_operations, 1)
        self.assertEqual(sum(r['method'] == 'POST' for r in self.calls()), 1)

    def test_applied_timeout_is_verified_without_duplicate_post(self):
        self.reset(failures={'accept_then_timeout': [{'method': 'POST', 'account': 1, 'sku': 'p'}]})
        self.feed([('p', 'Part', '1', '1')])
        result = self.run_sync()
        self.assertTrue(result.complete)
        self.assertEqual(len(self.listings()), 1)
        self.assertEqual(sum(r['method'] == 'POST' for r in self.calls()), 1)
        self.assertIsNotNone(self.item()['accounts'][0]['listing_id'])

    def test_import_during_update_cannot_acknowledge_newer_revision(self):
        self.reset(seeds=[{'account': 1, 'sku': 'p', 'title': 'Part', 'price': '1.00'}])
        self.feed([('p', 'Part', '2', '1')])
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.write
        def changed(*args):
            response = original(*args)
            self.feed([('p', 'Part', '3', '1')])
            return response
        client.write = changed
        result = self.run_sync(client)
        self.assertFalse(result.complete)
        self.assertEqual(self.item()['status'], 'modified')
        self.assertEqual(self.listings()[0]['price'], '2.00')
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.listings()[0]['price'], '3.00')

    def test_removal_during_create_keeps_reference_for_next_run(self):
        self.feed([('p', 'Part', '1', '1'), ('q', 'Other', '1', '1')])
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.write
        def removed(action, account, token, listing_id, payload):
            if action == 'POST' and payload['sku'] == 'p':
                self.feed([('q', 'Other', '1', '1')], allow=True)
            return original(action, account, token, listing_id, payload)
        client.write = removed
        self.run_sync(client)
        item = self.item()
        self.assertEqual(item['status'], 'sold')
        self.assertTrue(item['pending_removals'][0]['listing_id'])
        self.assertTrue(self.run_sync().complete)
        self.assertEqual([r['sku'] for r in self.listings()], ['q'])

    def test_discovery_coalesces_changes_and_leaves_new_products_for_next_run(self):
        self.reset(seeds=[{'account': 1, 'sku': 'p', 'title': 'Part', 'price': '1.00'}])
        self.feed([('p', 'Part', '1', '1')])
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.listings
        changed = False
        def discover(account, *args, **kwargs):
            nonlocal changed
            inventory = original(account, *args, **kwargs)
            if not changed:
                changed = True
                self.feed([('p', 'Part', '2', '1')])
                self.feed([('p', 'Part', '3', '1'), ('new', 'New', '1', '1')])
            return inventory
        client.listings = discover
        self.run_sync(client)
        self.assertEqual([r['sku'] for r in self.listings()], ['p'])
        self.assertEqual(self.listings()[0]['price'], '3.00')
        self.assertEqual(sum(r['method'] == 'PUT' for r in self.calls()), 1)
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(len(self.listings()), 2)

    def test_crash_after_create_is_recovered_on_next_run(self):
        self.feed([('p', 'Part', '1', '1')])
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.write
        def crash(*args):
            original(*args)
            raise KeyboardInterrupt
        client.write = crash
        with self.assertRaises(KeyboardInterrupt):
            self.run_sync(client)
        with self.db.transaction() as conn:
            self.assertEqual(conn.execute('SELECT state FROM sync_operations').fetchone()['state'], 'pending')
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(sum(r['method'] == 'POST' for r in self.calls()), 1)

    def test_second_process_fails_before_any_http_request(self):
        self.feed([('p', 'Part', '1', '1')])
        store = self.db.sync_repository()
        with store.exclusive():
            result = subprocess.run([sys.executable, '-m', 'piezalink', 'sync'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Another sync is running', result.stderr)
        self.assertFalse(self.calls())
        self.assertTrue(self.run_sync().complete)

    def test_incomplete_inventory_never_causes_writes(self):
        self.feed([('p', 'Part', '1', '1')])
        writes = []
        def handler(request):
            if request.method != 'GET':
                writes.append(request)
            if request.url.params.get('page') == '1':
                return httpx.Response(200, json={'items': [], 'page': 1, 'next_page': 2})
            return httpx.Response(503, json={'error': 'unavailable', 'detail': 'fail'})
        client = PortalClient(self.settings, transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
        self.addCleanup(client.close)
        self.assertFalse(self.run_sync(client).complete)
        self.assertFalse(writes)
        self.assertIsNone(self.item()['accounts'][0]['listing_id'])

    def test_unknown_request_without_visible_result_is_deferred(self):
        self.feed([('p', 'Part', '1', '1')])
        posts = []
        visible = False
        def handler(request):
            if request.method == 'POST':
                posts.append(request)
                raise httpx.ReadTimeout('request may still execute', request=request)
            account = int(request.url.path.split('/')[2])
            items = [{'account': 1, 'sku': 'p', 'title': 'Part', 'price': '1.00',
                      'listing_id': 'late', 'created_at': '2026-01-01T00:00:00Z'}] if visible and account == 1 else []
            return httpx.Response(200, json={'items': items, 'page': 1, 'next_page': None})
        client = PortalClient(self.settings, transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
        self.addCleanup(client.close)
        result = self.run_sync(client)
        self.assertEqual(result.deferred, 1)
        self.assertEqual(len(posts), 1)
        visible = True
        self.assertTrue(self.run_sync(client).complete)
        self.assertEqual(len(posts), 1)

    def test_simulator_reset_and_reused_ids_do_not_corrupt_adoption(self):
        self.feed([('p', 'Part', '1', '1'), ('q', 'Other', '1', '1')])
        self.assertTrue(self.run_sync().complete)
        self.reset(seeds=[{'account': 1, 'sku': 'q', 'title': 'Other', 'price': '1.00'}])
        self.assertTrue(self.run_sync().complete)
        ads = {r['sku']: r['listing_id'] for r in self.listings()}
        self.assertEqual(self.item('q')['accounts'][0]['listing_id'], ads['q'])
        self.assertEqual(self.item('p')['accounts'][0]['listing_id'], ads['p'])

    def test_source_revision_changes_but_sync_acknowledgements_do_not(self):
        self.feed([('p', 'Part', '1', '1')])
        before = self.item()['desired_revision']
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.item()['desired_revision'], before)
        self.feed([('p', 'Changed', '2', '1|2')])
        self.assertGreater(self.item()['desired_revision'], before)
        after = self.item()['desired_revision']
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(self.item()['desired_revision'], after)

    def test_default_simulation_with_real_injected_failures(self):
        directory = Path(__file__).resolve().parent.parent / 'data'
        if not directory.is_dir():
            self.skipTest('Repository feeds are not mounted')
        self.admin.post('/_admin/reset').raise_for_status()
        with Path('/app/accounts.csv').open(newline='') as stream:
            for row in csv.DictReader(stream):
                self.db.upsert_account(int(row['account_id']), row['token'])
        import_feed(directory / 'feed_dia1.csv')
        result = self.run_sync()
        self.assertEqual(result.failed_accounts, 1)  # Default account 4 is expired.
        actual = {(r['account'], r['sku']): r for r in self.listings() if r['sku'] is not None and r['account'] != 4}
        with directory.joinpath('feed_dia1.csv').open(encoding='utf-8-sig', newline='') as stream:
            expected = {(int(account), row['product_id']): row for row in csv.DictReader(stream)
                        for account in row['store_id'].split('|') if int(account) != 4}
        self.assertEqual(set(actual), set(expected))
        for key, row in actual.items():
            self.assertEqual(row['title'], expected[key]['title'].strip()[:80])
            self.assertEqual(Decimal(row['price']), Decimal(expected[key]['price']))
        count = len([r for r in self.calls() if r['method'] != 'GET'])
        self.run_sync()
        self.assertEqual(len([r for r in self.calls() if r['method'] != 'GET']), count)

    def test_independent_import_process_and_sync_transactions_do_not_deadlock(self):
        import time
        rows = [(f"p{n}", "Part", "1", "1") for n in range(10)]
        self.feed(rows)
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.write
        process = None
        script = """
import csv, sys
from piezalink.core.feed_import import import_feed
for price in range(2, 7):
    with open(sys.argv[1], 'w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['product_id','title','price','store_id'])
        writer.writerows((f'p{n}', 'Part', str(price), '1') for n in range(10))
    import_feed(sys.argv[1])
"""
        def concurrent(*args):
            nonlocal process
            if process is None:
                process = subprocess.Popen([sys.executable, '-c', script, str(self.path)],
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            time.sleep(0.03)
            return original(*args)
        client.write = concurrent
        try:
            self.run_sync(client)
            _, errors = process.communicate(timeout=20)
            self.assertEqual(process.returncode, 0, errors)
            self.assertTrue(self.run_sync().complete)
            self.assertTrue(all(r['price'] == '6.00' for r in self.listings()))
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()

    def test_loss_of_lock_session_stops_writes_and_recovers(self):
        self.feed([('p', 'Part', '1', '1')])
        store = self.db.sync_repository()
        client = PortalClient(self.settings, sleep=lambda seconds: None)
        self.addCleanup(client.close)
        original = client.write
        def lose_lock(*args):
            response = original(*args)
            store.lock_connection.close()
            return response
        client.write = lose_lock
        with patch.object(self.db, 'sync_repository', return_value=store):
            with self.assertRaises(psycopg.OperationalError):
                self.run_sync(client)
        self.assertTrue(self.run_sync().complete)
        self.assertEqual(sum(r['method'] == 'POST' for r in self.calls()), 1)

    def test_applied_put_and_delete_timeouts_are_reconciled(self):
        self.reset(seeds=[{'account': 1, 'sku': 'p', 'title': 'Old', 'price': '1.00'},
                          {'account': 1, 'sku': 'retired', 'title': 'Old', 'price': '1.00'}],
                   failures={'accept_then_timeout': [
                       {'method': 'PUT', 'account': 1, 'sku': 'p'},
                       {'method': 'DELETE', 'account': 1, 'sku': 'retired'},
                   ]})
        self.feed([('p', 'Updated', '2', '1')])
        self.assertTrue(self.run_sync().complete)
        self.assertEqual([(r['sku'], r['title']) for r in self.listings()], [('p', 'Updated')])
        self.assertEqual(sum(r['method'] == 'PUT' for r in self.calls()), 1)
        self.assertEqual(sum(r['method'] == 'DELETE' for r in self.calls()), 1)

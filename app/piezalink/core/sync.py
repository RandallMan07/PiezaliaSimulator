"""One bounded synchronization pass, with durable acknowledgements and recovery."""

from dataclasses import asdict, dataclass

from .config import get_sync_settings
from .portal import PortalClient, PortalError, same_payload
from .rovira_db import init_db


class SyncError(Exception):
    pass


class SyncBusyError(SyncError):
    pass


@dataclass
class SyncResult:
    run_id: int = 0
    created: int = 0
    updated: int = 0
    retired: int = 0
    unchanged: int = 0
    truncated: int = 0
    failed_accounts: int = 0
    failed_operations: int = 0
    deferred: int = 0
    pending: int = 0
    requests: int = 0

    @property
    def complete(self):
        return not (self.failed_accounts or self.failed_operations or self.deferred or self.pending)


def _matches(inventory, sku):
    return sorted((r for r in inventory.values() if r["sku"] == sku),
                  key=lambda r: (r["created_at"], r["listing_id"]))


def _wanted(target):
    return target is not None and not target["listed_manually"] and target["status"] != 'sold' and bool(target["desired"])


def _sync_one(store, client, settings, result, account, sku, inventory, known, progress):
    waited = 0.0
    retries = 0
    wrote_content = False
    completed_revision = None
    warned = False
    # Original ads + bounded retries: continuous imports cannot prolong the job indefinitely.
    limit = len(_matches(inventory, sku)) + settings.attempts + 2
    for _ in range(limit):
        target = store.target(sku, account)
        if target and (target["listed_manually"] or sku not in known):
            return
        if completed_revision is not None and (not target or target["desired_revision"] != completed_revision):
            return
        matches = _matches(inventory, sku)
        desired = _wanted(target)
        warning = "Publishing title truncated to 80 characters" if desired and len(target["title"]) > 80 else None
        if warning and not warned:
            result.truncated += 1
            warned = True
        payload = {"title": target["title"][:80], "price": str(target["price"])} if desired else None
        if not desired:
            if not matches:
                store.absent(sku, account)
                result.unchanged += 1
                return
            action, listing, body = 'DELETE', matches[0], None
        elif matches and same_payload(matches[0], payload):
            store.acknowledge(sku, account, target, matches[0], warning)
            if len(matches) == 1:
                if not wrote_content:
                    result.unchanged += 1
                return
            action, listing, body = 'DELETE', matches[1], None
        else:
            listing = matches[0] if matches else None
            action = 'PUT' if listing else 'POST'
            body = payload if listing else dict(payload, sku=sku)
        operation = store.prepare(
            result.run_id, account, sku, target, action,
            listing["listing_id"] if listing else None, body,
        )
        if operation is None:
            continue
        store.check_lock()
        try:
            response = client.write(action, account, store.token(account), operation["listing_id"], body)
            if response is not None and response["sku"] != sku:
                raise PortalError("Portal returned a different SKU", uncertain=True)
        except PortalError as error:
            store.failure(operation, error, uncertain=error.uncertain)
            if error.status == 401 or error.code == 'account_not_found':
                raise
            if error.code == 'listing_not_found' and action in ('PUT', 'DELETE'):
                inventory.pop(operation["listing_id"], None)
                if action == 'DELETE':
                    store.confirmed(operation)
                    result.retired += 1
                continue
            if error.uncertain:
                progress(f"Account {account}, product {sku}: verifying an uncertain write")
                fresh = client.listings(account, store.token(account), check=store.check_lock, progress=progress)
                inventory.clear()
                inventory.update({r["listing_id"]: r for r in fresh})
                store.recover(account, fresh, result.run_id)
                if store.operation_state(operation["id"]) == 'confirmed':
                    if action == 'DELETE':
                        result.retired += 1
                    else:
                        result.created += action == 'POST'
                        result.updated += action == 'PUT'
                        wrote_content = True
                        completed_revision = target["desired_revision"]
                if store.unresolved(sku, account):
                    # A timed-out request may still be executing: never immediately
                    # repeat a write just because a subsequent read found no result.
                    result.deferred += 1
                    return
                continue
            retries += 1
            if error.status in (429, 503) and retries < settings.attempts:
                delay = client.delay(error, retries - 1, settings.retry_budget - waited)
                if delay is not None:
                    progress(f"Account {account}, product {sku}: retry in {delay:.1f}s")
                    client.sleep(delay)
                    waited += delay
                    continue
            result.failed_operations += 1
            store.account_error(account, error)
            progress(f"Account {account}, product {sku}: {error}")
            return
        store.check_lock()
        store.confirmed(operation, response)
        retries = 0
        if action == 'DELETE':
            inventory.pop(operation["listing_id"], None)
            result.retired += 1
        else:
            inventory[response["listing_id"]] = response
            result.created += action == 'POST'
            result.updated += action == 'PUT'
            wrote_content = True
            completed_revision = target["desired_revision"]
        # A newer feed is left pending, rather than acknowledged by this response.
        if store.signature(store.target(sku, account)) != store.signature(target):
            return
    result.deferred += 1


def sync_catalogue(*, db=None, client=None, settings=None, progress=None):
    """Discover first, reconcile one candidate pass, and leave newer work for cron."""
    settings = settings or get_sync_settings()
    progress = progress or (lambda message: None)
    db = db or init_db()
    store = db.sync_repository()
    own_client = client is None
    client = client or PortalClient(settings)
    result = SyncResult()
    try:
        with store.exclusive():
            result.run_id, accounts, known, pairs = store.start()
            progress(f"Sync {result.run_id} started")
            try:
                inventories = {}
                paused = set()
                for row in accounts:
                    account = row["account_id"]
                    progress(f"Account {account}: discovering listings")
                    try:
                        listings = client.listings(account, store.token(account), check=store.check_lock, progress=progress)
                        store.refresh_references(account, listings)
                        store.recover(account, listings, result.run_id)
                        inventories[account] = {r["listing_id"]: r for r in listings}
                        store.account_error(account)
                    except PortalError as error:
                        paused.add(account)
                        result.failed_accounts += 1
                        store.account_error(account, error)
                        progress(f"Account {account}: skipped ({error})")
                candidates = {(r["account_id"], r["product_id"]) for r in pairs}
                for account, inventory in inventories.items():
                    candidates.update((account, r["sku"]) for r in inventory.values() if r["sku"] is not None)
                jobs = []
                for account, sku in candidates:
                    if account in paused or account not in inventories:
                        continue
                    target = store.target(sku, account)
                    priority = 0 if not _wanted(target) else (1 if _matches(inventories[account], sku) else 2)
                    jobs.append((priority, account, sku))
                for number, (_, account, sku) in enumerate(sorted(jobs), start=1):
                    if account in paused:
                        continue
                    try:
                        _sync_one(store, client, settings, result, account, sku, inventories[account], known, progress)
                    except PortalError as error:
                        paused.add(account)
                        result.failed_accounts += 1
                        store.account_error(account, error)
                        progress(f"Account {account}: remaining work deferred ({error})")
                    if number % 25 == 0:
                        progress(f"Sync {result.run_id}: processed {number}/{len(jobs)} candidates")
                result.pending = store.finalize()
                result.requests = client.requests
                store.finish(result.run_id, 'completed' if result.complete else 'partial', asdict(result))
                return result
            except BaseException as error:
                try:
                    store.finish(result.run_id, 'interrupted', asdict(result), type(error).__name__)
                except Exception:
                    pass  # Preserve the original exception if the database went away.
                raise
    finally:
        if own_client:
            client.close()

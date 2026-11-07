"""Database-only product status for support; no marketplace requests."""

from .rovira_db import init_db


def product_status(product_id, *, db=None):
    product_id = product_id.strip()
    if not product_id:
        raise ValueError('Product ID must not be empty')
    db = db or init_db()
    # One statement gives a consistent snapshot even while import/sync is running.
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT c.*, a.account_id, a.last_sync_at AS account_checked_at, "
            "a.last_sync_error AS account_error, ca.desired, ca.listing_id, "
            "ca.synced_revision, ca.last_synced_at, ca.last_attempt_at, "
            "ca.last_error AS listing_error, ca.last_warning, "
            "op.action AS operation_action, op.state AS operation_state, "
            "op.updated_at AS operation_at, op.last_error AS operation_error, "
            "op.listing_id AS operation_listing_id, "
            "(SELECT max(updated_at) FROM sync_operations h WHERE h.catalogue_id = c.id "
            "AND h.account_id = a.account_id AND h.state = 'confirmed') AS confirmed_at, "
            "(SELECT max(created_at) FROM sync_operations h WHERE h.catalogue_id = c.id "
            "AND h.account_id = a.account_id) AS attempted_at, "
            "EXISTS (SELECT 1 FROM sync_operations pending WHERE pending.catalogue_id = c.id "
            "AND pending.account_id = a.account_id AND pending.state IN ('pending','unknown')) AS unresolved "
            "FROM catalogue c CROSS JOIN accounts a "
            "LEFT JOIN catalogue_accounts ca ON ca.catalogue_id = c.id AND ca.account_id = a.account_id "
            "LEFT JOIN LATERAL (SELECT action, state, updated_at, last_error, listing_id FROM sync_operations "
            "WHERE catalogue_id = c.id AND account_id = a.account_id ORDER BY id DESC LIMIT 1) op ON TRUE "
            "WHERE c.product_id = %s ORDER BY a.account_id", (product_id,),
        ).fetchall()
    if not rows:
        raise LookupError(f'Product {product_id!r} is not in the local catalogue. No current product details are available.')
    fields = ('product_id', 'title', 'price', 'status', 'listed_manually',
              'created_at', 'modified_at', 'desired_revision')
    product = {key: rows[0][key] for key in fields}
    product['accounts'] = [row for row in rows if
                           row['desired'] is not None or row['operation_action'] or row['account_error']]
    return product


def last_confirmation(account):
    dates = [account.get(key) for key in ('last_synced_at', 'confirmed_at') if account.get(key)]
    return max(dates) if dates else None


def last_attempt(account):
    dates = [account.get(key) for key in ('last_attempt_at', 'attempted_at') if account.get(key)]
    return max(dates) if dates else None


def account_state(product, account):
    if product['listed_manually']:
        return 'Manual listing — not managed by sync'
    if account['unresolved']:
        return 'Awaiting verification of an interrupted or uncertain request'
    if account['desired'] is None and not account['operation_action'] and account['account_error']:
        return 'Account error — no product association recorded'
    wanted = account['desired'] and product['status'] != 'sold'
    if not wanted:
        if account['listing_id']:
            return 'Pending retirement — product sold or account removed'
        if account.get('operation_action') == 'DELETE' and account.get('operation_state') == 'confirmed':
            return 'Retired — removal confirmed by portal'
        if account['desired'] is not None and account['account_error']:
            return 'Retirement unverified — blocked by account error'
        return 'Not requested — product sold' if product['status'] == 'sold' else 'Not requested in this account'
    if account['account_error']:
        return 'Blocked by account error'
    if account['listing_error']:
        return 'Pending retry — last listing request failed'
    if not account['listing_id']:
        return 'Pending publication — no confirmed listing'
    if account['synced_revision'] != product['desired_revision']:
        return 'Pending update — catalogue changed since last confirmation'
    return 'In sync'

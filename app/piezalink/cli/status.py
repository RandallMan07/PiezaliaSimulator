"""Readable, fixed-width support report shared by CLI and menu."""

from datetime import datetime, timezone
import textwrap

from piezalink.core.status import account_state, last_confirmation, last_attempt


def _safe(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def _date(value):
    if value is None:
        return 'Never recorded'
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
    return _safe(value)


def format_product_status(product):
    lines = ['PRODUCT STATUS', '=' * 76]
    def field(label, value):
        prefix = f'  {label:<19} '
        lines.extend(textwrap.wrap(_safe(value), width=76, initial_indent=prefix,
                                   subsequent_indent=' ' * len(prefix)) or [prefix])
    field('Title', product['title'])
    field('Product ID / SKU', product['product_id'])
    field('Price', f"{product['price']:.2f}")
    field('Catalogue status', product['status'])
    if product['status'] == 'sold':
        field('Meaning', 'Not for sale in the accepted feed; portal retirement is shown per account')
    field('Managed by', 'Manual — excluded from sync' if product['listed_manually'] else 'Feed / PiezaLink')
    field('Created', _date(product['created_at']))
    field('Last modified', _date(product['modified_at']))
    confirmations = [last_confirmation(a) for a in product['accounts'] if last_confirmation(a)]
    field('Last sync', _date(max(confirmations) if confirmations else None))
    lines.extend(['', '  Last sync is the latest recorded confirmation in any account,',
                  '  including confirmed retirement. Sold is the catalogue state.',
                  '  Database snapshot only; current portal availability is not checked.'])
    for account in product['accounts']:
        lines.extend(['', f"ACCOUNT {account['account_id']}", '-' * 76])
        field('Status', account_state(product, account))
        wanted = bool(account['desired']) and product['status'] != 'sold'
        field('Requested by feed', 'Yes' if wanted else 'No')
        field('Portal listing ID', account['listing_id'] or 'None recorded')
        field('Last sync', _date(last_confirmation(account)))
        field('Last write attempt', _date(last_attempt(account)))
        field('Account sync check', _date(account['account_checked_at']))
        if account['account_error']:
            field('Account error', account['account_error'])
        if account['listing_error']:
            field('Listing error', account['listing_error'])
        if account['last_warning']:
            field('Warning', account['last_warning'])
        if account['operation_action']:
            action = {'POST': 'Publish', 'PUT': 'Update', 'DELETE': 'Retire'}[account['operation_action']]
            field('Last operation', f"{action} / {account['operation_state']}")
            if account.get('operation_listing_id'):
                field('Operation listing ID', account['operation_listing_id'])
            field('Operation recorded', _date(account['operation_at']))
            if account['operation_error']:
                field('Operation error', account['operation_error'])
    return '\n'.join(lines)

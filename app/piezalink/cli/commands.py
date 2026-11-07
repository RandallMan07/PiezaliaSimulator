"""CLI argument parsing; handlers reuse services in piezalink.core."""

import argparse
import sys

from piezalink.core.feed_import import FeedImportError, import_feed


def format_import_result(result):
    summary = (
        f"Imported {result.total} feed products: {result.added} added, "
        f"{result.modified} modified, {result.sold} marked sold, "
        f"{result.removed} removed, {result.unchanged} unchanged."
    )
    notices = []
    if result.skipped_blank_lines:
        notices.append(f"{result.skipped_blank_lines} blank lines skipped")
    if result.merged_duplicates:
        notices.append(f"{result.merged_duplicates} identical duplicates merged")
    if result.reconstructed_prices:
        notices.append(f"{result.reconstructed_prices} decimal-comma prices reconstructed")
    return summary + ("\n" + "; ".join(notices) + "." if notices else "")


def _run_import(args):
    import psycopg

    try:
        result = import_feed(args.feed, allow_large_removals=args.allow_large_removals)
    except (FeedImportError, OSError, ValueError, psycopg.Error) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 1
    print(format_import_result(result))
    return 0


def _register_import(subparsers):
    parser = subparsers.add_parser("import", help="Import a complete catalogue CSV into Postgres")
    parser.add_argument("feed", help="Path to a UTF-8 catalogue CSV")
    parser.add_argument(
        "--allow-large-removals", action="store_true",
        help="Allow more than 20%% of active products to disappear (never allows empty/invalid feeds)",
    )
    parser.set_defaults(handler=_run_import)


def format_sync_result(result):
    return (
        f"Sync {result.run_id}: {result.created} created, {result.updated} updated, "
        f"{result.retired} retired, {result.unchanged} unchanged; "
        f"{result.pending} pending account listings, {result.deferred} deferred candidates, "
        f"{result.failed_accounts} failed accounts, {result.failed_operations} failed operations. "
        f"{result.truncated} publishing titles truncated; {result.requests} HTTP requests."
    )


def _run_sync(args):
    import psycopg
    from piezalink.core.sync import SyncError, sync_catalogue
    try:
        result = sync_catalogue(progress=lambda message: print(message, flush=True))
    except (SyncError, ValueError, psycopg.Error) as error:
        print(f"Sync failed: {error}", file=sys.stderr)
        return 1
    print(format_sync_result(result))
    return 0 if result.complete else 1


def _register_sync(subparsers):
    parser = subparsers.add_parser("sync", help="Reconcile one bounded pass with Piezalia")
    parser.set_defaults(handler=_run_sync)


def _run_status(args):
    import psycopg
    from piezalink.core.status import product_status
    from piezalink.cli.status import format_product_status
    try:
        print(format_product_status(product_status(args.product_id)))
    except (LookupError, ValueError, OSError, psycopg.Error) as error:
        print(f"Status failed: {error}", file=sys.stderr)
        return 1
    return 0


def _register_status(subparsers):
    parser = subparsers.add_parser("status", help="Show product details and synchronization by account")
    parser.add_argument("product_id", help="Exact product ID / SKU (leading zeros are preserved)")
    parser.set_defaults(handler=_run_status)


COMMANDS = (_register_import, _register_sync, _register_status)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="piezalink",
        description="PiezaLink: Rovira catalogue synchronization.",
        epilog="Run without arguments to open the interactive menu.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for register in COMMANDS:
        register(subparsers)
    return parser


def run_commands(argv):
    args = build_parser().parse_args(argv)
    return args.handler(args)

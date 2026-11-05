"""CLI argument parsing; handlers reuse services in piezalink.core."""

import argparse
import sys

from piezalink.core.feed_import import FeedImportError, import_feed


def format_import_result(result):
    return (
        f"Imported {result.total} feed products: {result.added} added, "
        f"{result.modified} modified, {result.sold} marked sold, "
        f"{result.removed} removed, {result.unchanged} unchanged."
    )


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


COMMANDS = (_register_import,)


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

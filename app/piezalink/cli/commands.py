"""CLI registration; command handlers should call services in piezalink.core."""

import argparse


# Each future registrar receives subparsers and configures a command and handler.
COMMANDS = ()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="piezalink",
        description="PiezaLink: Rovira catalog synchronization. No commands added yet.",
        epilog="Run without arguments to open the interactive menu.",
    )
    if COMMANDS:
        subparsers = parser.add_subparsers(dest="command", required=True)
        for register in COMMANDS:
            register(subparsers)
    return parser


def run_commands(argv):
    args = build_parser().parse_args(argv)
    handler = getattr(args, "handler", None)
    return handler(args) if handler is not None else 0

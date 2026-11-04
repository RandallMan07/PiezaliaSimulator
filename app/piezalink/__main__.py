"""Route argument-based invocations to the CLI and bare invocations to the menu."""

import sys
from pathlib import Path

# Allow direct execution with `python piezalink/__main__.py` as well as -m.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from piezalink.cli.commands import run_commands
from piezalink.cli.menu import run_menu


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if args:
            return run_commands(args)
        return run_menu()
    except (KeyboardInterrupt, EOFError):
        print("\nGoodbye!")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

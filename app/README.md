# PiezaLink

Python CLI and interactive terminal scaffold for Rovira’s catalog synchronization.
Business commands (`import`, `sync`, and `status`) will be added individually;
none are implemented yet. The interactive menu currently offers only Exit.

## Install and run

Requires Python 3.11 or newer. From the repository root:

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e ./app
piezalink           # Interactive menu with the existing ASCII banner
piezalink --help    # CLI help, without opening the menu
```

Direct execution works without installation:

```sh
cd app/piezalink
python __main__.py
python __main__.py --help
```

From `app/`, `python -m piezalink` also opens the menu. Arguments select CLI
execution; unknown commands/options report an error and exit with code 2.
Use `0` (Exit), Ctrl+C, or Ctrl+D to leave the menu cleanly.

## Structure and extension

- `piezalink/__main__.py`: shared entry point for direct, module, and installed execution.
- `piezalink/cli/commands.py`: argument parsing and future command registration.
- `piezalink/cli/menu.py`: banner, menu loop, and reusable submenus.
- `piezalink/core/`: shared business services, to be implemented as commands are added.
- `piezalink/src/logo.txt`: packaged ASCII banner.

Future CLI handlers and menu actions must call the same services in `core/`.
Register CLI commands in `COMMANDS` with argparse subparsers to provide
`<command> --help` and option help. Add menu entries using `MenuItem`; a `Menu`
can itself be an action to open a submenu. Submenus offer Back, and completing
an operation returns to the main menu.

## Docker

From the repository root:

```sh
docker build -t piezalink ./app
docker run --rm -it piezalink          # Interactive menu
docker run --rm piezalink --help      # CLI help
```

The image installs the package and uses `ENTRYPOINT ["piezalink"]`, forwarding
container arguments to the CLI. Allocate a terminal with `-it` for the menu.
The existing `portal-sim/` image remains a separate marketplace simulator.

## Decisions

### CLI and interactive routing

Justification: _To be completed._

### Shared business logic and module layout

Justification: _To be completed._

### Standard-library argument parsing and menus

Justification: _To be completed._

### Packaging and Docker entry point

Justification: _To be completed._

## Source consulted

- [The easy (and nice) way to do CLI apps in Python](https://trstringer.com/easy-and-nice-python-cli/)

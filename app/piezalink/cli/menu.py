"""Interactive menus; actions should call services in piezalink.core."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib.resources import files


@dataclass(frozen=True)
class MenuItem:
    label: str
    action: Callable[[], object]


@dataclass(frozen=True)
class Menu:
    title: str
    items: tuple[MenuItem, ...] = ()

    def __call__(self):
        show_menu(self, is_main=False)


def print_logo():
    logo = files("piezalink").joinpath("src/logo.txt").read_text(encoding="utf-8")
    print(f"{logo}\n")


def show_menu(menu, *, is_main=True):
    while True:
        print(f"\n{menu.title}")
        for number, item in enumerate(menu.items, start=1):
            print(f"{number}. {item.label}")
        print("0. Exit" if is_main else "0. Back")
        choice = input("Select an option: ").strip()
        if choice == "0":
            return
        if not choice.isdecimal() or not 1 <= int(choice) <= len(menu.items):
            print("Invalid option. Please try again.")
            continue
        menu.items[int(choice) - 1].action()
        if is_main:
            continue
        # Complete a submenu operation, then return to the main menu.
        return


def import_catalogue():
    import psycopg

    from piezalink.core.feed_import import FeedImportError, LargeRemovalsError, import_feed
    from piezalink.cli.commands import format_import_result

    path = input("Feed CSV path (blank to return): ").strip()
    if not path:
        return
    try:
        try:
            result = import_feed(path)
        except LargeRemovalsError as exc:
            print(exc)
            confirmation = input("I verified this is a complete feed. Continue? [y/N]: ")
            if confirmation.strip().lower() != "y":
                print("Import cancelled; no catalogue changes applied.")
                return
            result = import_feed(path, allow_large_removals=True)
        print(format_import_result(result))
    except (FeedImportError, OSError, ValueError, psycopg.Error) as exc:
        print(f"Import failed: {exc}")


def sync_catalogue():
    import psycopg
    from piezalink.core.sync import SyncError, sync_catalogue as run_sync
    from piezalink.cli.commands import format_sync_result
    try:
        result = run_sync(progress=lambda message: print(message, flush=True))
        print(format_sync_result(result))
    except (SyncError, ValueError, psycopg.Error) as error:
        print(f"Sync failed: {error}")


def run_menu():
    print_logo()
    show_menu(Menu("PiezaLink", (MenuItem("Import catalogue", import_catalogue), MenuItem("Sync catalogue", sync_catalogue))))
    print("Goodbye!")
    return 0

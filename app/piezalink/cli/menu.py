"""Interactive menus; actions should call services in piezalink.core."""

from dataclasses import dataclass
from importlib.resources import files
from typing import Callable


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


def run_menu():
    print_logo()
    show_menu(Menu("PiezaLink"))
    print("Goodbye!")
    return 0

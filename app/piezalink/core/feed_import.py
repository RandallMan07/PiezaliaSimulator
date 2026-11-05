"""Stream a complete CSV feed into the shared catalogue import service."""

import csv
import re
from dataclasses import dataclass


class FeedImportError(Exception):
    """An import could not be completed; no catalogue changes were committed."""


class FeedValidationError(FeedImportError):
    def __init__(self, errors, *, error_count=None):
        self.errors = tuple(errors)
        self.error_count = error_count or len(self.errors)
        message = "Invalid feed:\n" + "\n".join(self.errors)
        if self.error_count > len(self.errors):
            message += f"\nShowing {len(self.errors)} of {self.error_count} errors."
        super().__init__(message)


class ImportBusyError(FeedImportError):
    pass


class LargeRemovalsError(FeedImportError):
    def __init__(self, missing, active):
        self.missing = missing
        self.active = active
        super().__init__(
            f"{missing} of {active} active products ({missing / active:.1%}) disappear. "
            "The limit is 20%; verify the feed is complete before using --allow-large-removals."
        )


@dataclass(frozen=True)
class ImportResult:
    total: int
    added: int = 0
    modified: int = 0
    sold: int = 0
    removed: int = 0
    unchanged: int = 0


def _validated_rows(stream):
    """Yield normalized records with bounded error storage and no DB round trips."""
    from .rovira_db import _price

    reader = csv.reader(stream, strict=True)
    required = {"product_id", "title", "price", "store_id"}
    errors = []
    error_count = 0
    record_no = 1
    try:
        headers = next(reader, None)
        if headers is None or len(headers) != 4 or set(headers) != required:
            raise FeedValidationError([
                "Header must contain product_id,title,price,store_id exactly once (any order)."
            ])
        positions = {name: headers.index(name) for name in required}
        for record_no, fields in enumerate(reader, start=2):
            try:
                if len(fields) != 4:
                    raise ValueError(f"expected 4 fields, received {len(fields)}")
                product_id = fields[positions["product_id"]].strip()
                title = fields[positions["title"]].strip()
                price_text = fields[positions["price"]].strip()
                if not product_id:
                    raise ValueError("product_id must not be empty")
                if not title or len(title) > 80:
                    raise ValueError("title must contain 1 to 80 characters")
                if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", price_text):
                    raise ValueError("price must use a decimal point and at most two decimal places")
                price = _price(price_text)
                account_parts = fields[positions["store_id"]].split("|")
                if any(not re.fullmatch(r"[0-9]+", part.strip()) for part in account_parts):
                    raise ValueError("store_id must contain account IDs separated by |")
                accounts = sorted({int(part.strip()) for part in account_parts})
                if any(not 0 < account <= 2147483647 for account in accounts):
                    raise ValueError("account IDs must be positive PostgreSQL integers")
            except ValueError as exc:
                error_count += 1
                if len(errors) < 50:
                    errors.append(f"Record {record_no}: {exc}")
                continue
            yield record_no, product_id, title, price, accounts
    except csv.Error as exc:
        raise FeedValidationError([f"CSV near line {reader.line_num}: {exc}"]) from exc
    except UnicodeError as exc:
        raise FeedValidationError(["The feed must be UTF-8 encoded."]) from exc
    if errors:
        raise FeedValidationError(errors, error_count=error_count)


def import_feed(path, *, allow_large_removals=False):
    """Import a complete feed atomically; empty or invalid feeds are rejected."""
    from .rovira_db import init_db

    with open(path, encoding="utf-8-sig", newline="") as stream:
        db = init_db()
        return db.import_catalogue(
            _validated_rows(stream), allow_large_removals=allow_large_removals,
        )

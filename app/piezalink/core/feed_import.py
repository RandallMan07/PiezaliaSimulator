"""Stream, normalize, and validate a complete CSV catalogue before reconciliation."""

import csv
import re
from decimal import Decimal
from dataclasses import dataclass, field


class FeedImportError(Exception):
    """An import failed; no catalogue changes were committed."""


class FeedValidationError(FeedImportError):
    def __init__(self, errors, *, error_count=None, skipped_blank_lines=0):
        self.errors = tuple(errors)
        self.error_count = error_count or len(self.errors)
        message = "Invalid feed:\n" + "\n".join(self.errors)
        if self.error_count > len(self.errors):
            message += f"\nShowing {len(self.errors)} of {self.error_count} errors."
        if skipped_blank_lines:
            message += f"\nIgnored {skipped_blank_lines} blank lines."
        super().__init__(message)


@dataclass
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    error_count: int = 0
    skipped_blank_lines: int = 0
    reconstructed_prices: int = 0

    def add(self, message):
        self.error_count += 1
        if len(self.errors) < 50:
            self.errors.append(message)

    def database_errors(self, rows, describe):
        """Include bounded SQL diagnostics while counting all matching errors."""
        for row in rows:
            self.add(describe(row))
        if rows:
            self.error_count += rows[0]["total_errors"] - len(rows)

    def raise_if_errors(self):
        if self.error_count:
            raise FeedValidationError(
                self.errors, error_count=self.error_count,
                skipped_blank_lines=self.skipped_blank_lines,
            )


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
    skipped_blank_lines: int = 0
    merged_duplicates: int = 0
    reconstructed_prices: int = 0


def _normalize_price(text):
    from .rovira_db import _price

    if not re.fullmatch(r"[0-9]+(?:[.,][0-9]+)?", text):
        raise ValueError("price must be numeric, with one decimal point or comma and no currency/thousands separators")
    # _price rejects any precision loss; 9,950 is 9.95, but 9,951 is invalid.
    return _price(text.replace(",", ".")).quantize(Decimal("0.01"))


def _normalize_accounts(text):
    parts = text.split("|")
    if any(not re.fullmatch(r"[0-9]+", part.strip()) for part in parts):
        raise ValueError("store_id must contain account IDs separated by |, without empty segments")
    try:
        accounts = sorted({int(part.strip()) for part in parts})
    except ValueError:
        raise ValueError("account IDs must be positive PostgreSQL integers") from None
    if any(not 0 < account <= 2147483647 for account in accounts):
        raise ValueError("account IDs must be positive PostgreSQL integers")
    return accounts


def _blank(fields):
    # A delimiter-only record (,,,) is malformed data, not a blank line.
    return not fields or (len(fields) == 1 and not fields[0].strip())


class FeedRows:
    """Stream partial and valid records to staging, retaining all diagnostic phases."""

    def __init__(self, stream):
        self.stream = stream
        self.report = ValidationReport()

    def __iter__(self):
        reader = csv.reader(self.stream, strict=True)
        required = {"product_id", "title", "price", "store_id"}
        line = 1
        try:
            while True:
                line = reader.line_num + 1
                headers = next(reader, None)
                if headers is None:
                    self.report.add("The feed has no header or products.")
                    return
                if not _blank(headers):
                    break
                self.report.skipped_blank_lines += 1
            if len(headers) != 4 or set(headers) != required:
                self.report.add("Header must contain product_id,title,price,store_id exactly once (any order).")
                return
            positions = {name: headers.index(name) for name in required}
            while True:
                line = reader.line_num + 1
                fields = next(reader, None)
                if fields is None:
                    return
                if _blank(fields):
                    self.report.skipped_blank_lines += 1
                    continue
                before = self.report.error_count
                price_pos = positions["price"]
                reconstructed = False
                # Only one extra field can be explained by an unquoted decimal comma.
                # Joining at the price column also works with reordered headers.
                if len(fields) == 5 and all(
                    re.fullmatch(r"[0-9]+", text.strip())
                    for text in fields[price_pos:price_pos + 2]
                ):
                    candidate = fields[:price_pos] + [
                        fields[price_pos].strip() + "," + fields[price_pos + 1].strip()
                    ] + fields[price_pos + 2:]
                    try:
                        _normalize_price(candidate[price_pos])
                        _normalize_accounts(candidate[positions["store_id"]].strip())
                    except ValueError:
                        pass
                    else:
                        fields = candidate
                        reconstructed = True
                product_pos = positions["product_id"]
                # A shifted field after a broken price is not a reliable product ID.
                reliable_id = len(fields) == 4 or product_pos < price_pos
                product_id = fields[product_pos].strip() if reliable_id and product_pos < len(fields) else None
                if product_id and "\x00" in product_id:
                    self.report.add(f"Line {line}: product_id contains a NUL character")
                    product_id = None
                label = f"Line {line}, product {product_id!r}" if product_id else f"Line {line}"
                if len(fields) != 4:
                    self.report.add(f"{label}: expected 4 fields, received {len(fields)}; decimal-comma reconstruction was not safe")
                    yield line, product_id, None, None, [], False
                    continue
                if not product_id:
                    self.report.add(f"{label}: product_id must not be empty")
                title = fields[positions["title"]].strip()
                if not title:
                    self.report.add(f"{label}: title must not be empty")
                elif "\x00" in title:
                    self.report.add(f"{label}: title contains a NUL character")
                    title = None
                price = None
                try:
                    price = _normalize_price(fields[price_pos].strip())
                except ValueError as exc:
                    self.report.add(f"{label}: {exc}")
                accounts = []
                try:
                    accounts = _normalize_accounts(fields[positions["store_id"]].strip())
                except ValueError as exc:
                    self.report.add(f"{label}: {exc}")
                valid = self.report.error_count == before
                if valid and reconstructed:
                    self.report.reconstructed_prices += 1
                yield line, product_id, title, price, accounts, valid
        except csv.Error as exc:
            self.report.add(f"CSV near line {reader.line_num}: {exc}")
        except UnicodeError:
            self.report.add(f"Near line {line}: the feed must be UTF-8 encoded.")


def _validated_rows(stream):
    """Parser-only convenience iterator; imports also run database validation."""
    rows = FeedRows(stream)
    for row in rows:
        if row[-1]:
            yield row[:-1]
    rows.report.raise_if_errors()


def import_feed(path, *, allow_large_removals=False):
    """Import a complete feed atomically; any unresolved error rejects the feed."""
    from .rovira_db import init_db

    with open(path, encoding="utf-8-sig", newline="") as stream:
        return init_db().import_catalogue(
            FeedRows(stream), allow_large_removals=allow_large_removals,
        )

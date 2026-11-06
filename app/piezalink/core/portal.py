"""Piezalia HTTP contract, complete discovery, and bounded read retries."""

import random
import time
from datetime import datetime, timezone
from decimal import Decimal
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import httpx


class PortalError(Exception):
    def __init__(self, message, *, status=None, code=None, uncertain=False, retry_after=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.uncertain = uncertain
        self.retry_after = retry_after


def same_payload(listing, payload):
    return listing["title"] == payload["title"] and listing["price"] == payload["price"]


class PortalClient:
    def __init__(self, settings, *, transport=None, sleep=time.sleep):
        self.settings = settings
        self.sleep = sleep
        self.requests = 0
        self.client = httpx.Client(
            base_url=settings.api_url.rstrip("/"), timeout=settings.timeout,
            transport=transport, follow_redirects=False,
        )

    def close(self):
        self.client.close()

    def delay(self, error, attempt, remaining):
        delay = min(self.settings.retry_cap, self.settings.retry_base * 2 ** attempt)
        delay *= random.uniform(0.8, 1.0)
        if error.retry_after is not None:
            delay = max(delay, error.retry_after)
        return delay if delay <= remaining else None

    def _send(self, method, account, token, *, listing_id=None, payload=None, page=None):
        path = f"/accounts/{account}/listings"
        if listing_id is not None:
            path += "/" + quote(listing_id, safe="")
        self.requests += 1
        try:
            response = self.client.request(
                method, path, headers={"Authorization": f"Bearer {token}"},
                json=payload, params={"page": page} if page is not None else None,
            )
        except httpx.RequestError as exc:
            raise PortalError(
                f"Portal request failed ({type(exc).__name__})", uncertain=method != "GET",
            ) from exc
        if response.status_code >= 300:
            try:
                body = response.json()
                code, detail = body.get("error"), body.get("detail", "Portal request failed")
            except (ValueError, AttributeError):
                code, detail = None, "Portal returned an invalid error response"
            retry_after = None
            if value := response.headers.get("Retry-After"):
                try:
                    retry_after = max(0.0, float(value))
                except ValueError:
                    try:
                        retry_after = max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
                    except (TypeError, ValueError):
                        pass
            raise PortalError(
                f"HTTP {response.status_code}: {str(detail)[:1000]}", status=response.status_code,
                code=code, retry_after=retry_after,
                uncertain=method != "GET" and response.status_code >= 500 and response.status_code != 503,
            )
        if method == "DELETE":
            if response.status_code != 204:
                raise PortalError("Invalid delete response", uncertain=True)
            return None
        try:
            body = response.json()
            if method != "GET":
                self._validate_listing(body, account)
                if "sku" in payload and body["sku"] != payload["sku"]:
                    raise ValueError("SKU mismatch")
                if not same_payload(body, payload):
                    raise ValueError("Response payload mismatch")
            return body
        except (ValueError, TypeError, KeyError):
            raise PortalError("Invalid portal response", uncertain=method != "GET") from None

    @staticmethod
    def _validate_listing(item, account):
        from .rovira_db import _price
        if not isinstance(item, dict) or type(item.get("account")) is not int or item["account"] != account:
            raise ValueError("Invalid listing account")
        if not isinstance(item.get("listing_id"), str) or not item["listing_id"]:
            raise ValueError("Invalid listing ID")
        if "sku" not in item:
            raise ValueError("Missing SKU field")
        if item.get("sku") is not None and (not isinstance(item["sku"], str) or not item["sku"].strip()):
            raise ValueError("Invalid SKU")
        if not isinstance(item.get("title"), str) or not isinstance(item.get("created_at"), str):
            raise ValueError("Invalid listing fields")
        created = datetime.fromisoformat(item["created_at"].replace("Z", "+00:00"))
        if created.tzinfo is None:
            raise ValueError("Listing creation timestamp must include a timezone")
        item["price"] = str(_price(item["price"]).quantize(Decimal("0.01")))

    def listings(self, account, token, *, check=lambda: None, progress=lambda message: None):
        inventory, ids, visited = [], set(), set()
        page = 1
        waited = 0.0
        while page is not None:
            if type(page) is not int or page < 1 or page in visited:
                raise PortalError("Invalid or repeated pagination cursor")
            visited.add(page)
            for attempt in range(self.settings.attempts):
                check()
                try:
                    body = self._send("GET", account, token, page=page)
                    break
                except PortalError as error:
                    if error.status not in (None, 429, 503) or attempt + 1 == self.settings.attempts:
                        raise
                    delay = self.delay(error, attempt, self.settings.retry_budget - waited)
                    if delay is None:
                        raise
                    progress(f"Account {account}: discovery retry in {delay:.1f}s")
                    self.sleep(delay)
                    waited += delay
            try:
                if not isinstance(body, dict) or body["page"] != page or not isinstance(body["items"], list):
                    raise ValueError
                for item in body["items"]:
                    self._validate_listing(item, account)
                    if item["listing_id"] in ids:
                        raise ValueError
                    ids.add(item["listing_id"])
                    inventory.append(item)
                next_page = body["next_page"]
                if next_page is not None and (type(next_page) is not int or next_page <= page):
                    raise ValueError
                page = next_page
            except (KeyError, TypeError, ValueError):
                raise PortalError("Incomplete or invalid portal inventory; account writes skipped") from None
        return inventory

    def write(self, action, account, token, listing_id, payload):
        return self._send(action, account, token, listing_id=listing_id, payload=payload)

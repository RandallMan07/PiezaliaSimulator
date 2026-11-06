# Developer's notes

In order to further understand an clarify the proposed problem, I'll write my
conlcusions in this document. I'll use this as a knowledge base therfore, which
will make the developement easier.

## Catalog

> The `<feed>` is the ground truth.

We store the whole title in the catalog, even if it exceeds Piezalia's limit of
80 characters. We will check that limit when publishing, then decide whether to
truncate the published title or alert the user. The source title stays unchanged.

The listings recovered from calling the portal's API will only tell us if incorrect
data is uploaded.

Therefore, we'll update our database with new feeds of the day as follows;

1. If a new item is appear -> add to database with status `unlisted`. Then try
   to list it.
2. If a price/title/store changes -> put it to `modified` and change the field
   `modified_at`, we then know when changed. The sync will make the necessary
   API calls to Piezalia.
3. If a item disappear or it's removed, depending on the status we'll do the
   following; `(a)` If is `listed`, change it to `sold` and retire from the portal.
   `(b)` If it's still `unlisted` just get rid of it from database. (if no error
   shouldn't happen)

## Sold items

I undestand that if a item is sold it's not directly reflected to piezalia, we
have to notice and retire it manually after.

## Feeds

We have two `<feed>` for emulating two days of work.

This makes us realize that the first day we'll take the whole catalog and revise
the current status of the items. First, we'll call `GET /accounts/{account}/
listings` to recover the whole information per account. Then we'll sanitize both
the database and piezalia iterating between the catalog and the gathered listing
by;

1. If product (`sku`/`product_id`) exist in the listing but not in the new feed,
   then we have to update the status to sold and retire the publishing.
2. If a product exist in the catalog, doesn't in the listing and has `"status":
 "unlisted"` we should create a new publish.
3. If a product exists in both sides but the information differs, it should update
   it, reflect it on the database.
4. All other combinations are exceptions and should be carefully treated.

### Manual publishing

There are cases where there are items listed that doesn't have sku, it's `null`.
This ones should be ignored, we can include them in the database with an another
field `listed_manually` true. We can manage the status internally (if it disappear
put it to stauts sold...), but never trigger any API call for it.

## `listing_id`

The API has an extra field called `listing_id`. We need it to update or retire
a publishing in Piezalia. Each account has its own publishing, so we store it
in the relation between the catalog item and the account. It starts as `null`
and we update it after listing the item.

## Accounts

We consider `accounts.csv` pre-existing data. When initializing the database, we
load those accounts in the same transaction as the schema. Existing tokens are
not overwritten, and later startups do not load the file again. The one-time
upgrade also adds any missing accounts to an existing simulation database.

## Importing the feed

We read the CSV into temporary staging tables, then compare the full catalog
using SQL. We ignore blank lines and normalize prices without rounding. Identical normalized
product duplicates can be merged, but conflicting ones must be corrected. If any
row is still incorrect, we reject the whole feed, otherwise a bad row could look
like a sold item. Empty feeds are rejected too. If more than 20%
of active items disappear, we have to verify the feed before allowing it.

The import runs in one transaction. Another import running at the same time
will fail, the next cronjob can try again. Manual items are not changed.
`created_at` stays the same, `modified_at` only changes when the item changes.

If an account is removed from an item, we keep its `listing_id` with
`desired=false` until sync retires it. An absent `modified` item is also put to
`sold`. We only delete an absent `unlisted` item if it has no portal references.
A sold item appearing again is put to `modified` so sync can revise it.

## Sync

Only one sync runs at a time. We capture the candidates once and read the latest
data before each request. If an import changes an item while the request is in
progress, the response only confirms the revision sent. The newer change stays
pending for the next cronjob, without chaining complete syncs.

We list all pages first, adopt existing listings and preserve manual ones. Sold
items and removed accounts are retired first. We keep the oldest listing when
there are duplicates, and retire the extra copies after checking its content.
The publishing title is limited to 80 characters, the source title stays complete.

Requests are recorded before sending them. A timeout can happen after Piezalia
accepted a write, so we check the portal before retrying. Pending creations also
keep their local product/account references when a new feed removes them. Partial
success is kept, and another run can recover after interruption. Short database
transactions leave imports available while the HTTP requests are running.

---

Copyright (c) 2026 Adrià Martín. All Rights Reserved.

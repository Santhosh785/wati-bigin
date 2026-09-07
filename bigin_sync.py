#!/usr/bin/env python3
"""
bigin_sync.py

Mirrors the Bigin CRM Contacts module into MongoDB.

  Full sync  : every contact, every field  ->  campaigns_leads_wati.bigin_contacts
  Delta sync : only records created/edited since the last successful run

The first run is automatically a full sync. Every run after that is a delta,
driven by a high-water mark stored in the `bigin_sync_state` collection, so the
3-hourly timer only pulls what actually changed.

Usage:
    python3 bigin_sync.py              # delta (full, the first time)
    python3 bigin_sync.py --full       # force a complete re-pull
    python3 bigin_sync.py --dry-run    # fetch + report, write nothing

Config comes from .env next to this file:
    ZOHO_CLIENT_ID, ZOHO_CLIENT_SECRET, ZOHO_REFRESH_TOKEN, ZOHO_REGION
    MONGO_URI

Needs pymongo (already installed). Everything else is stdlib.
"""

import argparse
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from pymongo import MongoClient, UpdateOne, uri_parser
from pymongo.errors import PyMongoError

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")

MODULE = "Contacts"
COLLECTION = "bigin_contacts"
STATE_COLLECTION = "bigin_sync_state"
STATE_ID = f"{MODULE.lower()}_sync"

PER_PAGE = 200           # Bigin v2 hard maximum
MAX_PAGES = 10000        # runaway guard
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_RETRIES = 4

# Field types Bigin will not return through the `fields` parameter.
UNREADABLE_TYPES = {"profileimage"}


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_env(path=ENV_PATH):
    """Minimal .env reader — no dependency on python-dotenv."""
    env = dict(os.environ)
    # A serverless deployment gets its configuration from the platform, not
    # from a file in the bundle. Reading one there would mean credentials that
    # silently outlive a rotation — the file is a snapshot of whatever was on
    # the machine that built it — so on Vercel the environment is the only
    # source. Locally nothing changes.
    if os.environ.get("VERCEL"):
        return env
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                # real environment wins over the file
                env.setdefault(k.strip(), v)
    return env


def require(env, *keys):
    missing = [k for k in keys if not env.get(k)]
    if missing:
        sys.exit(f"Missing required config in .env: {', '.join(missing)}")
    return [env[k] for k in keys]


# --------------------------------------------------------------------------- #
# Bigin API
# --------------------------------------------------------------------------- #
class BiginClient:
    def __init__(self, client_id, client_secret, refresh_token, region="in"):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.region = region
        self.accounts = f"https://accounts.zoho.{region}/oauth/v2/token"
        self._token = None
        self._expires_at = 0.0
        self.api_domain = f"https://www.zohoapis.{region}"

    # -- auth ------------------------------------------------------------- #
    def token(self):
        """Cached access token; refreshed a minute before it actually expires."""
        if self._token and time.time() < self._expires_at - 60:
            return self._token
        body = urllib.parse.urlencode({
            "refresh_token": self.refresh_token,
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request(self.accounts, data=body)
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read())
        if "access_token" not in payload:
            raise RuntimeError(f"Token refresh failed: {payload}")
        self._token = payload["access_token"]
        self._expires_at = time.time() + int(payload.get("expires_in", 3600))
        self.api_domain = payload.get("api_domain", self.api_domain)
        return self._token

    # -- transport -------------------------------------------------------- #
    def get(self, path, params=None, headers=None):
        """GET with retry/backoff. Returns (status, parsed_body)."""
        url = f"{self.api_domain}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(MAX_RETRIES):
            hdrs = {"Authorization": f"Zoho-oauthtoken {self.token()}"}
            hdrs.update(headers or {})
            try:
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=60) as resp:
                    raw = resp.read()
                    if resp.status in (204, 304) or not raw:
                        return resp.status, {}
                    return resp.status, json.loads(raw)
            except urllib.error.HTTPError as e:
                # 304 = "nothing changed since If-Modified-Since" — a normal,
                # empty answer for a delta run, not a failure.
                if e.code == 304:
                    return 304, {}
                detail = e.read()[:400].decode("utf-8", "replace")
                if e.code == 401 and attempt < MAX_RETRIES - 1:
                    self._token = None          # force a refresh, retry once
                    continue
                if e.code in RETRY_STATUSES and attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"GET {path} -> HTTP {e.code}: {detail}") from None
            except urllib.error.URLError as e:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"GET {path} -> {e.reason}") from None
        raise RuntimeError(f"GET {path} exhausted retries")

    # -- metadata --------------------------------------------------------- #
    def field_names(self, module=MODULE):
        """Every readable field API name, so 'all fields' stays true as Bigin changes."""
        _, meta = self.get("/bigin/v2/settings/fields", {"module": module})
        names = [
            f["api_name"] for f in meta.get("fields", [])
            if f.get("data_type") not in UNREADABLE_TYPES
        ]
        if not names:
            raise RuntimeError(f"No readable fields returned for module {module}")
        return names

    # -- records ---------------------------------------------------------- #
    def iter_records(self, fields, module=MODULE, modified_since=None):
        """
        Yield record dicts, following Bigin's page-token pagination.

        `page=N` silently caps out at 2000 records, so the token is the only
        correct way to walk a 7k+ module.
        """
        headers = {}
        if modified_since:
            headers["If-Modified-Since"] = modified_since

        params = {"fields": ",".join(fields), "per_page": PER_PAGE}
        token = None
        for _ in range(MAX_PAGES):
            page_params = dict(params)
            if token:
                page_params["page_token"] = token
            status, body = self.get(f"/bigin/v2/{module}", page_params, headers)
            # 204 = nothing modified since the high-water mark
            if status in (204, 304) or not body.get("data"):
                return
            for rec in body["data"]:
                yield rec
            info = body.get("info") or {}
            token = info.get("next_page_token")
            if not info.get("more_records") or not token:
                return
        raise RuntimeError("Pagination exceeded MAX_PAGES — aborting to avoid a loop")

    def deleted_ids(self, module=MODULE, since=None):
        """Record ids deleted in Bigin, so the mirror doesn't keep ghosts."""
        headers = {"If-Modified-Since": since} if since else {}
        out, token = [], None
        for _ in range(MAX_PAGES):
            params = {"type": "all", "per_page": PER_PAGE}
            if token:
                params["page_token"] = token
            status, body = self.get(f"/bigin/v2/{module}/deleted", params, headers)
            if status in (204, 304) or not body.get("data"):
                return out
            out.extend(str(r["id"]) for r in body["data"] if r.get("id"))
            info = body.get("info") or {}
            token = info.get("next_page_token")
            if not info.get("more_records") or not token:
                return out
        return out


# --------------------------------------------------------------------------- #
# time helpers
# --------------------------------------------------------------------------- #
def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def to_iso(moment):
    """Bigin's If-Modified-Since wants an ISO8601 offset timestamp."""
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def parse_bigin_time(value):
    """'2026-08-20T15:05:15+05:30' -> aware datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# mongo
# --------------------------------------------------------------------------- #
def open_db(uri, create_indexes=True):
    client = MongoClient(uri, serverSelectionTimeoutMS=15000, tz_aware=True)
    dbname = uri_parser.parse_uri(uri).get("database")
    if not dbname:
        sys.exit("MONGO_URI has no database name in its path.")
    db = client[dbname]
    if create_indexes:
        db[COLLECTION].create_index("bigin_id", unique=True, name="bigin_id_unique")
        db[COLLECTION].create_index("Modified_Time", name="modified_time")
        db[COLLECTION].create_index("Phone", name="phone")
    return client, db


def shape(rec, synced_at):
    """Bigin record -> Mongo doc. Every field kept verbatim, plus sync metadata."""
    doc = dict(rec)
    doc["bigin_id"] = str(rec.get("id"))
    doc["_syncedAt"] = synced_at
    doc["_source"] = "bigin_api"
    for key in ("Created_Time", "Modified_Time"):
        parsed = parse_bigin_time(rec.get(key))
        if parsed:
            doc[key] = parsed
    doc.pop("id", None)
    return doc


def write_batch(coll, docs):
    """Upsert on bigin_id. $setOnInsert keeps the original first-seen stamp."""
    if not docs:
        return 0, 0
    ops = [
        UpdateOne(
            {"bigin_id": d["bigin_id"]},
            {"$set": d, "$setOnInsert": {"_firstSeenAt": d["_syncedAt"]}},
            upsert=True,
        )
        for d in docs
    ]
    res = coll.bulk_write(ops, ordered=False)
    return len(res.upserted_ids or {}), res.modified_count


def read_state(db):
    return db[STATE_COLLECTION].find_one({"_id": STATE_ID}) or {}


def save_state(db, high_water, stats):
    db[STATE_COLLECTION].update_one(
        {"_id": STATE_ID},
        {"$set": {
            "lastSyncAt": now_utc(),
            "highWaterMark": high_water,
            "lastRun": stats,
        }},
        upsert=True,
    )


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def sync(full=False, dry_run=False, verbose=True):
    env = load_env()
    cid, secret, refresh = require(
        env, "ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    (mongo_uri,) = require(env, "MONGO_URI")
    region = env.get("ZOHO_REGION", "in")

    started = now_utc()

    def say(msg):
        if verbose:
            print(f"[{to_iso(now_utc())}] {msg}", flush=True)

    client_db, db = open_db(mongo_uri, create_indexes=not dry_run)
    try:
        state = read_state(db)
        since = None if full else state.get("highWaterMark")
        mode = "FULL" if not since else "DELTA"
        say(f"{mode} sync of {MODULE}" + (f" since {since}" if since else ""))

        bigin = BiginClient(cid, secret, refresh, region)
        fields = bigin.field_names()
        say(f"{len(fields)} readable fields")

        coll = db[COLLECTION]
        batch, fetched, inserted, updated = [], 0, 0, 0
        newest = parse_bigin_time(since) if since else None

        for rec in bigin.iter_records(fields, modified_since=since):
            fetched += 1
            doc = shape(rec, started)
            mtime = doc.get("Modified_Time")
            if isinstance(mtime, dt.datetime) and (newest is None or mtime > newest):
                newest = mtime
            batch.append(doc)
            if len(batch) >= 500:
                if not dry_run:
                    i, u = write_batch(coll, batch)
                    inserted += i
                    updated += u
                batch = []
                say(f"  … {fetched} fetched")
        if batch and not dry_run:
            i, u = write_batch(coll, batch)
            inserted += i
            updated += u

        # Purge records deleted in Bigin so the mirror can't serve ghosts.
        removed = 0
        try:
            gone = bigin.deleted_ids(since=since)
            if gone and not dry_run:
                removed = coll.delete_many({"bigin_id": {"$in": gone}}).deleted_count
            elif gone:
                removed = coll.count_documents({"bigin_id": {"$in": gone}})
        except RuntimeError as e:
            say(f"  ! deleted-record sweep skipped: {e}")

        total = coll.estimated_document_count()
        stats = {
            "mode": mode, "fetched": fetched, "inserted": inserted,
            "updated": updated, "removed": removed, "collectionTotal": total,
            "durationSec": round((now_utc() - started).total_seconds(), 1),
            "dryRun": dry_run,
        }

        if not dry_run:
            # Advance the mark to the newest Modified_Time actually seen. Falling
            # back to the start time keeps an empty delta run from re-pulling
            # everything next time.
            high_water = to_iso(newest) if newest else (since or to_iso(started))
            save_state(db, high_water, stats)

        say(f"done: fetched={fetched} inserted={inserted} updated={updated} "
            f"removed={removed} total={total} in {stats['durationSec']}s"
            + ("  (DRY RUN — nothing written)" if dry_run else ""))
        return stats
    finally:
        client_db.close()


def main():
    ap = argparse.ArgumentParser(description="Sync Bigin Contacts into MongoDB.")
    ap.add_argument("--full", action="store_true",
                    help="ignore the high-water mark and re-pull everything")
    ap.add_argument("--dry-run", action="store_true",
                    help="fetch and report, but write nothing")
    ap.add_argument("--quiet", action="store_true", help="suppress progress output")
    args = ap.parse_args()
    try:
        sync(full=args.full, dry_run=args.dry_run, verbose=not args.quiet)
    except (RuntimeError, PyMongoError) as e:
        print(f"SYNC FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()

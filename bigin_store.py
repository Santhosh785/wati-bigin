#!/usr/bin/env python3
"""
bigin_store.py

Read side of the Bigin mirror: turns the MongoDB documents written by
`bigin_sync.py` back into the flat (headers, rows) shape the WATI cleaner
already works on.

Kept separate from `app.py` so the web UI still imports and runs when pymongo
is not installed — in that case the UI simply falls back to CSV upload.

Every value is flattened to a string, because the filter/map/generate steps
downstream assume CSV-ish text.
"""

import datetime as dt
import os
import re

# Documents are stored by bigin_sync.py; these are its bookkeeping fields,
# not Bigin data, so they are hidden from the UI's column list.
INTERNAL_FIELDS = {"_id", "_source", "_syncedAt", "_firstSeenAt"}

# Shown first in the column dropdowns; everything else follows alphabetically.
PREFERRED_ORDER = [
    "Full_Name", "First_Name", "Last_Name", "Phone", "Email",
    "Lead_Source1", "CA_Status", "Status", "Potential", "Attempt",
    "Language", "Tag", "Other_City", "Created_Time", "Modified_Time",
]

# Bigin returns some timestamps as raw ISO strings rather than typed dates;
# match them so every date column formats the same way in the UI.
ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

COLLECTION = "bigin_contacts"
STATE_COLLECTION = "bigin_sync_state"
STATE_ID = "contacts_sync"

# Bigin timestamps are stored in UTC; display them in the org's local zone.
DISPLAY_TZ = dt.timezone(dt.timedelta(hours=5, minutes=30))   # IST


class StoreUnavailable(RuntimeError):
    """Raised when Mongo cannot be reached or pymongo is missing."""


def _load_env(path=None):
    """Read .env next to this file; real environment variables win."""
    path = path or os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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
                env.setdefault(k.strip(), v)
    return env


def _connect():
    try:
        from pymongo import MongoClient, uri_parser
        from pymongo.errors import PyMongoError
    except ImportError:
        raise StoreUnavailable(
            "pymongo is not installed — run 'pip install pymongo' to load from Bigin, "
            "or use the CSV upload instead."
        ) from None

    uri = _load_env().get("MONGO_URI")
    if not uri:
        raise StoreUnavailable("MONGO_URI is not set in .env.")
    try:
        client = MongoClient(uri, serverSelectionTimeoutMS=10000, tz_aware=True)
        dbname = uri_parser.parse_uri(uri).get("database")
        if not dbname:
            raise StoreUnavailable("MONGO_URI has no database name in its path.")
        return client, client[dbname]
    except PyMongoError as e:
        raise StoreUnavailable(f"Could not reach MongoDB: {e}") from None


def flatten(value):
    """Any Mongo/BSON value -> a plain string the CSV pipeline can handle."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(DISPLAY_TZ).strftime("%Y-%m-%d %H:%M")
    if isinstance(value, dict):
        # Bigin lookups (Owner, Created_By, Modified_By) -> the human name
        for key in ("name", "Name", "full_name", "id"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(flatten(v) for v in value if v is not None)
    if isinstance(value, str) and ISO_DATETIME.match(value):
        try:
            return flatten(dt.datetime.fromisoformat(value))
        except ValueError:
            return value
    return str(value)


def order_headers(keys):
    """Preferred fields first, then whatever else the mirror happens to hold."""
    keys = set(keys)
    head = [k for k in PREFERRED_ORDER if k in keys]
    tail = sorted(keys - set(head), key=str.lower)
    return head + tail


def load_contacts():
    """
    Return (headers, rows) for every mirrored contact.

    rows are dicts of str -> str, exactly like csv.DictReader produces, so the
    existing filter / map / clean / batch code needs no changes at all.
    """
    client, db = _connect()
    try:
        from pymongo.errors import PyMongoError
        try:
            docs = list(db[COLLECTION].find({}))
        except PyMongoError as e:
            raise StoreUnavailable(f"Read from MongoDB failed: {e}") from None

        if not docs:
            return [], []

        keys = set()
        for d in docs:
            keys.update(k for k in d if k not in INTERNAL_FIELDS)
        headers = order_headers(keys)

        rows = [{h: flatten(d.get(h)) for h in headers} for d in docs]
        return headers, rows
    finally:
        client.close()


def sync_status():
    """
    Freshness info for the UI: how many contacts are mirrored and when the
    cron job last ran. Never raises — the UI shows what it can.
    """
    info = {"available": False, "count": 0, "lastSyncAt": None,
            "lastRun": None, "error": None}
    try:
        client, db = _connect()
    except StoreUnavailable as e:
        info["error"] = str(e)
        return info
    try:
        info["count"] = db[COLLECTION].estimated_document_count()
        state = db[STATE_COLLECTION].find_one({"_id": STATE_ID}) or {}
        last = state.get("lastSyncAt")
        if isinstance(last, dt.datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=dt.timezone.utc)
            info["lastSyncAt"] = last
        info["lastRun"] = state.get("lastRun")
        info["available"] = True
    except Exception as e:                      # noqa: BLE001 - status must never break the page
        info["error"] = str(e)
    finally:
        client.close()
    return info


def humanize_age(moment):
    """'2 hours ago' style label for the sync freshness badge."""
    if not isinstance(moment, dt.datetime):
        return "unknown"
    delta = dt.datetime.now(dt.timezone.utc) - moment.astimezone(dt.timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 0:
        return "just now"
    if secs < 90:
        return "just now"
    mins = secs // 60
    if mins < 60:
        return f"{mins} min ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = hours // 24
    return f"{days} day{'s' if days != 1 else ''} ago"

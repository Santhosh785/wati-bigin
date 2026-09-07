#!/usr/bin/env python3
"""
session_store.py — where a browser's wizard state lives between requests.

The cleaner is a five-step wizard: load rows, filter them, generate the WATI
list, then compose and schedule a send. Every step reads what the previous one
put in the session, so the session *is* the app's working memory.

On a single long-lived process that memory can simply be a dict, and it was.
On Vercel it cannot: each request may be served by a different instance, so a
dict populated by step 1 is empty by step 2. This module keeps the same
one-dict-per-browser shape but persists it, so the wizard works whether one
process serves every request or fifty do.

Two backends, picked once, lazily:

    mongo   MONGO_URI is set and reachable — the session is one gzipped JSON
            blob per browser, expired by a TTL index. This is what Vercel uses.
    memory  no Mongo — a plain dict, exactly the old behaviour, which keeps
            `python3 app.py` working on a laptop with no database.

Why one gzipped blob rather than a document with fields: the session holds the
uploaded rows themselves, and Mongo's 16 MB document limit is the real
constraint. A 20 000-row Bigin load is ~8 MB of JSON and well under 1 MB
gzipped, so compression is what makes a whole working set fit in one atomic
read and one atomic write.

Config via environment:
    MONGO_URI              same database the campaigns use
    SESSION_TTL_SECONDS    idle lifetime of a session   (default 7200)
"""

import gzip
import hashlib
import json
import os
import threading
import time

COLLECTION = "wati_sessions"
TTL_SECONDS = int(os.environ.get("SESSION_TTL_SECONDS", str(2 * 60 * 60)))

# Mongo's hard document limit is 16 MB. Stop well short of it: the blob is not
# the whole document, and a session that has run step 4 carries the generated
# CSVs on top of the rows.
MAX_BLOB_BYTES = 12 * 1024 * 1024

MAX_MEMORY_SESSIONS = 500       # memory backend only; Mongo has the TTL index

_backend = None
_note = ""
_db = None
_client = None
_LOCK = threading.Lock()

_MEM = {}                       # sid -> session dict (memory backend)
# sid -> sha1 of the blob this instance last read or wrote. Lets save() skip
# the write when a request changed nothing, which is most of them — hovering
# over the table and re-rendering a fragment should not cost a Mongo write.
# A cold start starts empty, which only ever costs one redundant write.
_SEEN = {}


class SessionTooLarge(Exception):
    """The working set does not fit in one document even compressed."""


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #
def _mongo_uri():
    try:
        import bigin_store
        return bigin_store._load_env().get("MONGO_URI")
    except Exception:                            # noqa: BLE001
        return os.environ.get("MONGO_URI")


def _init():
    """Pick a backend once. Never raises — memory always works."""
    global _backend, _note, _db, _client
    if _backend:
        return _backend
    with _LOCK:
        if _backend:
            return _backend
        uri = _mongo_uri()
        if uri:
            try:
                from pymongo import MongoClient, uri_parser
                client = MongoClient(uri, serverSelectionTimeoutMS=8000, tz_aware=True)
                dbname = uri_parser.parse_uri(uri).get("database")
                if not dbname:
                    raise ValueError("MONGO_URI has no database name.")
                db = client[dbname]
                db.command("ping")
                # Mongo, not the app, is what expires an abandoned session:
                # there is no long-lived process left to sweep them.
                try:
                    db[COLLECTION].create_index("last_seen", expireAfterSeconds=TTL_SECONDS)
                except Exception:                # noqa: BLE001 — index may exist with another TTL
                    pass
                _client, _db, _backend = client, db, "mongo"
                _note = f"MongoDB · {dbname}.{COLLECTION}"
                return _backend
            except Exception as e:               # noqa: BLE001 — fall back, never fail
                _note = f"MongoDB unavailable ({e}); sessions kept in memory"
        else:
            _note = "MONGO_URI not set; sessions kept in memory"
        _backend = "memory"
        return _backend


def backend():
    """('mongo'|'memory', human-readable note)."""
    _init()
    return _backend, _note


def serverless():
    """True when there is no process to keep a dict alive between requests."""
    return bool(os.environ.get("VERCEL"))


# --------------------------------------------------------------------------- #
# (de)serialisation
# --------------------------------------------------------------------------- #
# A filter's `values` is a set — JSON has no set, so it round-trips as a sorted
# list. Everything else in a session is already JSON-shaped.
def _encode(sess):
    doc = dict(sess)
    doc["filters"] = [{"col": f["col"], "values": sorted(f["values"]), "mode": f["mode"]}
                      for f in sess.get("filters") or []]
    raw = json.dumps(doc, default=str, separators=(",", ":")).encode("utf-8")
    return gzip.compress(raw, 6)


def _decode(blob):
    doc = json.loads(gzip.decompress(blob).decode("utf-8"))
    doc["filters"] = [{"col": f["col"], "values": set(f["values"]), "mode": f["mode"]}
                      for f in doc.get("filters") or []]
    doc.setdefault("files", {})
    doc.setdefault("wati_rows", [])
    return doc


# --------------------------------------------------------------------------- #
# the two calls app.py makes
# --------------------------------------------------------------------------- #
def load(sid):
    """This browser's session, or None if it has expired or never existed."""
    if not sid:
        return None
    if _init() == "memory":
        sess = _MEM.get(sid)
        if sess is not None:
            sess["last_seen"] = time.time()
        return sess
    try:
        doc = _db[COLLECTION].find_one({"_id": sid}, {"blob": 1})
    except Exception as e:                       # noqa: BLE001 — a dead read is a lost session, not a 500
        print(f"[sessions] read failed for {sid[:8]}: {type(e).__name__}: {e}")
        return None
    if not doc or not doc.get("blob"):
        return None
    try:
        blob = bytes(doc["blob"])
        sess = _decode(blob)
    except Exception as e:                       # noqa: BLE001 — corrupt blob: start fresh
        print(f"[sessions] undecodable session {sid[:8]}: {type(e).__name__}: {e}")
        return None
    _SEEN[sid] = hashlib.sha1(blob).hexdigest()
    return sess


def save(sid, sess):
    """
    Persist the session, unless this request changed nothing.

    Called once per request from the handler, after the route has run. Returns
    True when it actually wrote.
    """
    if not sid or sess is None:
        return False
    if _init() == "memory":
        _MEM[sid] = sess
        _evict_memory()
        return True

    blob = _encode(sess)
    digest = hashlib.sha1(blob).hexdigest()
    if _SEEN.get(sid) == digest:
        return False
    if len(blob) > MAX_BLOB_BYTES:
        # The generated CSVs are the one part that can be rebuilt from the rows
        # by pressing Generate again, so they are what gets dropped first.
        trimmed = dict(sess, files={})
        blob = _encode(trimmed)
        digest = hashlib.sha1(blob).hexdigest()
        if len(blob) > MAX_BLOB_BYTES:
            raise SessionTooLarge(
                f"This working set is {len(blob) / 1e6:.1f} MB compressed, over the "
                f"{MAX_BLOB_BYTES / 1e6:.0f} MB a session can hold. Filter the list down, "
                f"or split the source into smaller loads.")
        print(f"[sessions] {sid[:8]} over size — dropped generated files to fit")

    import datetime as _dt
    try:
        _db[COLLECTION].update_one(
            {"_id": sid},
            {"$set": {"blob": blob, "last_seen": _dt.datetime.now(_dt.timezone.utc),
                      "bytes": len(blob)}},
            upsert=True)
    except Exception as e:                       # noqa: BLE001
        print(f"[sessions] write failed for {sid[:8]}: {type(e).__name__}: {e}")
        return False
    _SEEN[sid] = digest
    return True


def forget(sid):
    if not sid:
        return
    if _init() == "memory":
        _MEM.pop(sid, None)
        return
    _SEEN.pop(sid, None)
    try:
        _db[COLLECTION].delete_one({"_id": sid})
    except Exception:                            # noqa: BLE001
        pass


def _evict_memory():
    """Memory backend only: drop idle sessions, then cap the total."""
    now = time.time()
    for k in [k for k, v in _MEM.items() if now - v.get("last_seen", now) > TTL_SECONDS]:
        del _MEM[k]
    if len(_MEM) > MAX_MEMORY_SESSIONS:
        oldest = sorted(_MEM, key=lambda k: _MEM[k].get("last_seen", 0))
        for k in oldest[:len(_MEM) - MAX_MEMORY_SESSIONS]:
            del _MEM[k]

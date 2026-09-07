#!/usr/bin/env python3
"""
campaign_store.py

Where campaigns live between "Schedule" and "Sent".

A scheduled campaign has to outlive the browser session that created it — and
ideally the app process too, so a restart at 2am does not silently drop a 6am
send. So campaigns are written to MongoDB (the same cluster `bigin_sync.py`
already uses) and re-read on startup by `campaign_runner.py`.

Mongo is optional everywhere else in this repo, so it is optional here too:
without pymongo (or without a reachable cluster) the store falls back to a JSON
file at `data/campaigns.json`. Same API either way; `backend()` says which one
is live so the UI can be honest about it.

Document shape (one per campaign):

    campaign_id      uuid hex, the primary key
    name             what the user typed
    template_name    WATI template being sent
    broadcast_name   what WATI shows in its own broadcast list
    status           scheduled | running | paused | completed | cancelled | failed
    created_at       UTC datetime
    scheduled_at     UTC datetime — when the runner should start it
    started_at       UTC datetime or None
    finished_at      UTC datetime or None
    throttle         messages per minute
    total/sent/failed/cursor   progress counters; cursor is the resume point
    delivered/read/replied/undelivered   delivery counters, written by the
                     WATI webhook (see wati_webhook.py) long after the send
    source           'bigin' | 'csv' — where the list came from
    retry_of         campaign_id this one retries, or None
    retry_round      0 for an original send, 1 for its retry, …
    retry_state      None | armed | building | done | none | off | capped
    retry_due_at     UTC datetime — when the failed leads get their second try
    retry_campaign_id  the retry this campaign spawned, once it exists
    recipients       [{name, phone, params, status, error, at,
                       wamid, delivered_at, read_at, replied_at,
                       failed_at, failed_code, failed_detail, failed_bucket,
                       reply_text, reply_count}]
                     — everything from `wamid` onwards is filled in by the
                     webhook, and absent until the first event arrives
    log              [{ts, msg}] — a short human-readable history

Recipients are embedded rather than kept in their own collection: a campaign
here tops out in the low tens of thousands, which is far under Mongo's 16 MB
document ceiling, and it means a campaign is one atomic read or write.
"""

import datetime as dt
import json
import os
import threading
import uuid

DB_COLLECTION = "wati_campaigns"
# Where the JSON fallback writes. A serverless host mounts the deployment
# read-only with only /tmp writable, hence the override — but note what a
# per-instance file means there: it is not shared between instances and does
# not survive one, so on such a host MONGO_URI is not optional. The fallback
# only keeps the app answering while Mongo is down.
DATA_DIR = (os.environ.get("WATI_DATA_DIR")
            or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
JSON_PATH = os.path.join(DATA_DIR, "campaigns.json")

ACTIVE_STATUSES = ("scheduled", "running", "paused")
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

# Serialises JSON-backend writes and read-modify-write races inside this
# process. Across processes it is claim() — a single atomic compare-and-set —
# that keeps two app instances from sending the same campaign twice.
_LOCK = threading.RLock()

IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


class StoreError(RuntimeError):
    pass


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# backend selection
# --------------------------------------------------------------------------- #
_backend = None          # 'mongo' | 'json'
_backend_note = None
_db = None
_client = None


def _mongo_uri():
    try:
        import bigin_store
        return bigin_store._load_env().get("MONGO_URI")
    except Exception:                            # noqa: BLE001
        return os.environ.get("MONGO_URI")


def _init():
    """Pick a backend once, lazily. Never raises — JSON always works."""
    global _backend, _backend_note, _db, _client
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
                    raise StoreError("MONGO_URI has no database name.")
                db = client[dbname]
                db.command("ping")
                db[DB_COLLECTION].create_index("campaign_id", unique=True)
                db[DB_COLLECTION].create_index([("status", 1), ("scheduled_at", 1)])
                # The retry sweep asks "whose wait is up" on every scheduler
                # tick, which is the same shape of question as "what is due to
                # send" and deserves the same index.
                db[DB_COLLECTION].create_index([("retry_state", 1), ("retry_due_at", 1)],
                                               sparse=True)
                # The webhook matches an event back to the recipient it is
                # about — by WhatsApp message id when the send gave us one,
                # by phone number otherwise. Both are lookups into an embedded
                # array, so both need an index or every delivery receipt is a
                # collection scan.
                db[DB_COLLECTION].create_index("recipients.wamid", sparse=True)
                db[DB_COLLECTION].create_index([("recipients.phone", 1), ("created_at", -1)])
                _client, _db, _backend = client, db, "mongo"
                _backend_note = f"MongoDB · {dbname}.{DB_COLLECTION}"
                return _backend
            except Exception as e:               # noqa: BLE001 — fall back, never fail
                _backend_note = f"MongoDB unavailable ({e}); using {JSON_PATH}"
        else:
            _backend_note = f"MONGO_URI not set; using {JSON_PATH}"
        try:
            os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
        except OSError as e:                     # read-only filesystem
            _backend_note = f"{_backend_note}; {os.path.dirname(JSON_PATH)} is not writable ({e})"
        _backend = "json"
        return _backend


def backend():
    """('mongo'|'json', human-readable note) — for the UI's storage badge."""
    _init()
    return _backend, _backend_note


# --------------------------------------------------------------------------- #
# JSON backend helpers
# --------------------------------------------------------------------------- #
def _json_read():
    if not os.path.isfile(JSON_PATH):
        return []
    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return []


def _json_write(docs):
    os.makedirs(os.path.dirname(JSON_PATH), exist_ok=True)
    tmp = JSON_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(docs, f, default=_iso)
    os.replace(tmp, JSON_PATH)      # atomic — a crash mid-write cannot truncate the file


def _iso(value):
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    raise TypeError(f"not JSON serialisable: {type(value)}")


def _revive(doc):
    """JSON round-trip loses datetimes; put them back."""
    for key in ("created_at", "scheduled_at", "started_at", "finished_at", "retry_due_at"):
        v = doc.get(key)
        if isinstance(v, str):
            try:
                doc[key] = dt.datetime.fromisoformat(v)
            except ValueError:
                doc[key] = None
        elif isinstance(v, dt.datetime) and v.tzinfo is None:
            doc[key] = v.replace(tzinfo=dt.timezone.utc)
    return doc


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def create(name, template_name, recipients, scheduled_at=None, throttle=60,
           source=None, broadcast_name=None, dry_run=False,
           group_id=None, batch_index=None, batch_count=None,
           retry_of=None, retry_round=0, retry_base_name=None):
    """
    Persist a new campaign and return its document.

    `recipients` is [{'name', 'phone', 'params'}] with phone already normalised
    to digits-with-country-code. `scheduled_at` None means "start now".

    A split send creates one campaign per batch, all sharing a `group_id`.
    They stay independent — each has its own schedule, progress and controls —
    but the group is what lets the dashboard show them together and stop the
    remaining ones in a single click.

    `retry_of` marks a campaign built from another one's failed leads (see
    `campaign_retry.py`). A retry is an ordinary campaign in every other
    respect — same scheduler, same controls, same delivery reporting — which is
    why resending a week later needs no second sending path.
    """
    _init()
    cid = uuid.uuid4().hex
    when = scheduled_at or now_utc()
    doc = {
        "campaign_id": cid,
        "name": name or "Untitled campaign",
        "template_name": template_name,
        "broadcast_name": broadcast_name or _broadcast_name(name, cid),
        "status": "scheduled",
        "created_at": now_utc(),
        "scheduled_at": when,
        "started_at": None,
        "finished_at": None,
        "throttle": int(throttle),
        "dry_run": bool(dry_run),
        "source": source,
        "group_id": group_id,
        "batch_index": batch_index,
        "batch_count": batch_count,
        # The 7-day retry of failed leads. `retry_state` is the state machine
        # campaign_retry.py walks: None -> armed -> building -> done | none,
        # plus 'off' (turned off by the user) and 'capped' (round limit hit).
        # It is what makes the retry claim atomic across app processes.
        "retry_of": retry_of,
        "retry_round": int(retry_round or 0),
        "retry_base_name": retry_base_name or (name if not retry_of else None),
        "retry_state": None,
        "retry_due_at": None,
        "retry_campaign_id": None,
        "retry_note": None,
        "total": len(recipients),
        "sent": 0,
        "failed": 0,
        "cursor": 0,
        # Delivery counters, filled in later by the WATI webhook rather than by
        # the sender: `sent` only means WATI accepted the message, and what
        # happened to it afterwards arrives minutes or hours later.
        # `undelivered` is Meta refusing a message WATI had already accepted,
        # which is a different fact from `failed` (WATI refused the API call).
        "delivered": 0,
        "read": 0,
        "replied": 0,
        "undelivered": 0,
        "recipients": [
            {
                "name": r.get("name") or "",
                "phone": r["phone"],
                "params": r.get("params") or {},
                "status": "pending",
                "error": None,
                "at": None,
            }
            for r in recipients
        ],
        "log": [{"ts": now_utc().isoformat(),
                 "msg": f"Created with {len(recipients)} recipients, "
                        f"{'scheduled for ' + when.astimezone(IST).strftime('%d %b %Y %H:%M IST') if scheduled_at else 'sending immediately'}"
                        + (" (DRY RUN — nothing will be sent)" if dry_run else "")}],
    }
    with _LOCK:
        if _backend == "mongo":
            _db[DB_COLLECTION].insert_one(dict(doc))
        else:
            docs = _json_read()
            docs.append(json.loads(json.dumps(doc, default=_iso)))
            _json_write(docs)
    return doc


def _broadcast_name(name, cid):
    """WATI shows this in its own broadcast list; keep it unique and traceable."""
    stem = "".join(ch if ch.isalnum() or ch in " _-" else "_" for ch in (name or "campaign"))[:40].strip()
    return f"{stem or 'campaign'}_{cid[:8]}"


def list_group(group_id, include_recipients=False):
    """Every batch of one split send, in send order."""
    if not group_id:
        return []
    _init()
    with _LOCK:
        if _backend == "mongo":
            projection = {"_id": 0}
            if not include_recipients:
                projection["recipients"] = 0
            docs = list(_db[DB_COLLECTION].find({"group_id": group_id}, projection))
        else:
            docs = [d for d in _json_read() if d.get("group_id") == group_id]
            if not include_recipients:
                docs = [{k: v for k, v in d.items() if k != "recipients"} for d in docs]
    docs.sort(key=lambda d: d.get("batch_index") or 0)
    return [_revive(d) for d in docs]


def get(campaign_id):
    _init()
    with _LOCK:
        if _backend == "mongo":
            doc = _db[DB_COLLECTION].find_one({"campaign_id": campaign_id}, {"_id": 0})
            return _revive(doc) if doc else None
        for d in _json_read():
            if d.get("campaign_id") == campaign_id:
                return _revive(d)
    return None


def _group_order(docs):
    """
    Newest first, but with the batches of one split send kept together and in
    send order.

    Sorting purely by creation time scatters a group backwards — batch 6 first,
    batch 1 last — because the batches are created milliseconds apart. So the
    group takes the position of its newest member and its batches read 1, 2,
    3 … inside that slot, which is the order they will actually go out in.
    """
    newest = {}
    for i, d in enumerate(docs):
        gid = d.get("group_id")
        if gid and gid not in newest:
            newest[gid] = i
    return sorted(
        range(len(docs)),
        key=lambda i: (newest.get(docs[i].get("group_id"), i), docs[i].get("batch_index") or 0),
    )


def list_campaigns(limit=100, include_recipients=False):
    """Newest first, batches of one send kept together. Recipient arrays are
    dropped by default — the list page only needs counters, and a hundred
    campaigns' worth of recipients is a lot of bytes to move for a
    progress bar."""
    _init()
    with _LOCK:
        if _backend == "mongo":
            projection = {"_id": 0}
            if not include_recipients:
                projection["recipients"] = 0
            docs = list(_db[DB_COLLECTION].find({}, projection)
                        .sort("created_at", -1).limit(limit))
        else:
            docs = sorted(_json_read(), key=lambda d: d.get("created_at") or "", reverse=True)[:limit]
            if not include_recipients:
                docs = [{k: v for k, v in d.items() if k != "recipients"} for d in docs]
    docs = [_revive(d) for d in docs]
    return [docs[i] for i in _group_order(docs)]


def due_campaigns(now=None):
    """Scheduled campaigns whose time has come, oldest schedule first."""
    now = now or now_utc()
    _init()
    with _LOCK:
        if _backend == "mongo":
            docs = list(_db[DB_COLLECTION].find(
                {"status": "scheduled", "scheduled_at": {"$lte": now}},
                {"_id": 0, "recipients": 0}).sort("scheduled_at", 1))
        else:
            docs = []
            for d in _json_read():
                d = _revive(d)
                if d.get("status") == "scheduled" and d.get("scheduled_at") and d["scheduled_at"] <= now:
                    docs.append({k: v for k, v in d.items() if k != "recipients"})
            docs.sort(key=lambda d: d["scheduled_at"])
    return [_revive(d) for d in docs]


def interrupted_campaigns():
    """Campaigns left mid-flight by a restart — the runner resumes these."""
    _init()
    with _LOCK:
        if _backend == "mongo":
            docs = list(_db[DB_COLLECTION].find({"status": "running"},
                                                {"_id": 0, "recipients": 0}))
        else:
            docs = [{k: v for k, v in _revive(d).items() if k != "recipients"}
                    for d in _json_read() if d.get("status") == "running"]
    return docs


def running_count():
    """
    How many campaigns are mid-send, according to the store.

    Cheap enough for a page header, and the only honest answer where the thing
    doing the sending is a different process from the one rendering the page.
    """
    _init()
    with _LOCK:
        if _backend == "mongo":
            return _db[DB_COLLECTION].count_documents({"status": "running"})
        return sum(1 for d in _json_read() if d.get("status") == "running")


def claim(campaign_id, worker):
    """
    Atomically move a campaign from scheduled/paused to running.

    Returns the claimed document, or None if it was already claimed, finished,
    or deleted. This is what makes it safe for two app processes to point at the
    same database: whichever one wins the compare-and-set sends the campaign,
    and the loser simply does not start a second walk through the same list.
    """
    _init()
    changes = {"status": "running", "claimed_by": worker, "claimed_at": now_utc()}
    with _LOCK:
        if _backend == "mongo":
            from pymongo import ReturnDocument
            doc = _db[DB_COLLECTION].find_one_and_update(
                {"campaign_id": campaign_id, "status": {"$in": ["scheduled", "paused"]}},
                {"$set": changes,
                 "$push": {"log": {"ts": now_utc().isoformat(), "msg": f"Started sending ({worker})"}}},
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER)
            if not doc:
                return None
            if not doc.get("started_at"):
                _db[DB_COLLECTION].update_one({"campaign_id": campaign_id},
                                              {"$set": {"started_at": now_utc()}})
                doc["started_at"] = now_utc()
            return _revive(doc)

        docs = _json_read()
        for d in docs:
            if d.get("campaign_id") == campaign_id and d.get("status") in ("scheduled", "paused"):
                d.update(json.loads(json.dumps(changes, default=_iso)))
                d.setdefault("started_at", None)
                if not d["started_at"]:
                    d["started_at"] = now_utc().isoformat()
                d.setdefault("log", []).append(
                    {"ts": now_utc().isoformat(), "msg": f"Started sending ({worker})"})
                _json_write(docs)
                return _revive(d)
    return None


def retry_due_campaigns(now=None):
    """
    Campaigns whose failed leads have waited long enough — oldest first.

    Only `armed` ones: a retry that is mid-creation ('building'), already made
    ('done'), found nothing ('none') or was turned off ('off') is not due, and
    saying so in the query rather than in the caller is what keeps the sweep
    cheap on a collection with thousands of finished campaigns.

    Recipients are projected away. The sweep re-reads the full document through
    claim_retry() for the one campaign it is about to act on, which is a far
    smaller amount of data across the wire than every failed campaign's
    recipient list on every tick.
    """
    now = now or now_utc()
    _init()
    with _LOCK:
        if _backend == "mongo":
            docs = list(_db[DB_COLLECTION].find(
                {"retry_state": "armed", "retry_due_at": {"$lte": now}},
                {"_id": 0, "recipients": 0}).sort("retry_due_at", 1))
        else:
            docs = []
            for d in _json_read():
                d = _revive(d)
                if d.get("retry_state") == "armed" and d.get("retry_due_at") and d["retry_due_at"] <= now:
                    docs.append({k: v for k, v in d.items() if k != "recipients"})
            docs.sort(key=lambda d: d["retry_due_at"])
    return [_revive(d) for d in docs]


def claim_retry(campaign_id, worker, allowed_states=("armed",)):
    """
    Atomically move a campaign's `retry_state` to 'building' and hand back the
    full document, recipients included.

    Returns None if it was not in one of `allowed_states` — which is how two
    app processes sweeping the same minute avoid both creating a retry of the
    same campaign, for the same reason and by the same mechanism as claim().
    A manual "retry now" passes a wider `allowed_states`, because forcing a
    retry of a campaign that was never armed (one that finished before retries
    existed, say) is a legitimate thing to ask for.

    The caller owns the claim until it writes a terminal state. Every path out
    of campaign_retry.create_retry() writes one, including the failure path.
    """
    _init()
    states = list(allowed_states)
    with _LOCK:
        if _backend == "mongo":
            from pymongo import ReturnDocument
            # `$in` with a null in the list matches a missing field too, which
            # is what an unarmed campaign looks like.
            doc = _db[DB_COLLECTION].find_one_and_update(
                {"campaign_id": campaign_id, "retry_state": {"$in": states}},
                {"$set": {"retry_state": "building", "retry_claimed_by": worker,
                          "retry_claimed_at": now_utc()}},
                projection={"_id": 0},
                return_document=ReturnDocument.AFTER)
            return _revive(doc) if doc else None

        docs = _json_read()
        for d in docs:
            if d.get("campaign_id") == campaign_id and d.get("retry_state") in states:
                d["retry_state"] = "building"
                d["retry_claimed_by"] = worker
                d["retry_claimed_at"] = now_utc().isoformat()
                _json_write(docs)
                return _revive(dict(d))
    return None


def update(campaign_id, changes, log_msg=None):
    """Apply top-level field changes, optionally appending a log line."""
    _init()
    changes = dict(changes)
    with _LOCK:
        if _backend == "mongo":
            ops = {"$set": changes}
            if log_msg:
                ops["$push"] = {"log": {"ts": now_utc().isoformat(), "msg": log_msg}}
            _db[DB_COLLECTION].update_one({"campaign_id": campaign_id}, ops)
        else:
            docs = _json_read()
            for d in docs:
                if d.get("campaign_id") == campaign_id:
                    d.update(json.loads(json.dumps(changes, default=_iso)))
                    if log_msg:
                        d.setdefault("log", []).append({"ts": now_utc().isoformat(), "msg": log_msg})
            _json_write(docs)


def record_results(campaign_id, results, cursor, sent, failed):
    """
    Write back a finished chunk: per-recipient outcomes plus the counters.

    `results` is {index: {'status', 'error', 'at', 'wamid'}} — `wamid` only
    when WATI's send response carried one. Persisting once per chunk
    rather than once per message keeps a 7 000-contact campaign to ~140 writes
    instead of 7 000, while still leaving at most one chunk to re-check after a
    crash — and re-checking is safe because every recipient carries its own
    status.
    """
    _init()
    with _LOCK:
        if _backend == "mongo":
            ops = {"cursor": cursor, "sent": sent, "failed": failed}
            for idx, res in results.items():
                ops[f"recipients.{int(idx)}.status"] = res["status"]
                ops[f"recipients.{int(idx)}.error"] = res.get("error")
                ops[f"recipients.{int(idx)}.at"] = res.get("at")
                # Only when WATI's send response actually carried an id. Most
                # of the time it does not, and the `templateMessageSent`
                # webhook fills it in instead — see wati_webhook.handle().
                if res.get("wamid"):
                    ops[f"recipients.{int(idx)}.wamid"] = res["wamid"]
            _db[DB_COLLECTION].update_one({"campaign_id": campaign_id}, {"$set": ops})
        else:
            docs = _json_read()
            for d in docs:
                if d.get("campaign_id") == campaign_id:
                    for idx, res in results.items():
                        i = int(idx)
                        if 0 <= i < len(d.get("recipients", [])):
                            d["recipients"][i].update(res)
                    d.update(cursor=cursor, sent=sent, failed=failed)
            _json_write(docs)


def delete(campaign_id):
    _init()
    with _LOCK:
        if _backend == "mongo":
            _db[DB_COLLECTION].delete_one({"campaign_id": campaign_id})
        else:
            _json_write([d for d in _json_read() if d.get("campaign_id") != campaign_id])


def status_counts():
    """{'scheduled': 2, 'running': 1, …} for the header badge."""
    counts = {}
    for c in list_campaigns(limit=500):
        counts[c.get("status", "?")] = counts.get(c.get("status", "?"), 0) + 1
    return counts



# --------------------------------------------------------------------------- #
# delivery receipts — what the WATI webhook writes back
# --------------------------------------------------------------------------- #
# A send is only the beginning of a message's life. WATI answers
# sendTemplateMessage with 200 OK and the runner records "sent"; whether the
# phone ever received it, whether anyone read it, and whether Meta refused it
# after the fact all arrive minutes or hours later down the webhook
# (wati_webhook.py). These four functions are how that late news gets attached
# to the recipient it belongs to.
#
# Everything here has to be idempotent, because WATI redelivers: it retries on
# any non-2xx, it fires most events twice (the "_v2" twin), and a response lost
# in flight looks to it exactly like a failure. So the counters are never
# blindly incremented — set_recipient_field_once() only counts a transition
# that it itself performed, in one atomic compare-and-set.

def set_recipient_field_once(campaign_id, index, field, value, counter=None, extra=None):
    """
    Set `recipients[index].field` only if it is not set yet, and count it.

    Returns True when this call is what set it — which is exactly the condition
    for incrementing `counter`, so the same delivery receipt arriving three
    times still moves the campaign's `delivered` count by one. False means
    somebody (or some earlier redelivery) got there first.

    `extra` is applied unconditionally alongside, for the fields that are
    descriptive rather than counted — Meta's error text, the last event name.
    """
    _init()
    path = f"recipients.{int(index)}.{field}"
    with _LOCK:
        if _backend == "mongo":
            sets = {path: value}
            for k, v in (extra or {}).items():
                sets[f"recipients.{int(index)}.{k}"] = v
            ops = {"$set": sets}
            if counter:
                ops["$inc"] = {counter: 1}
            # The filter is the compare-and-set, and it has to be
            # `$exists: False` rather than the obvious `path: None`.
            #
            # `{"recipients.0.delivered_at": None}` looks like "that one field
            # is unset" and is not: on an array field Mongo also traverses the
            # array looking for an element with a literal "0" sub-field, finds
            # none, reads the missing path as null, and matches. The filter
            # therefore matched a recipient whose delivered_at was already a
            # timestamp — so every redelivered receipt won the compare-and-set
            # again and the campaign counted one delivery four times. Caught by
            # `python3 wati_webhook.py --selftest`, which is there for this.
            #
            # `$exists` does not fall for it: the path resolves once the field
            # is written, so the second event matches nothing. (These fields are
            # only ever written with a real value, never an explicit null, which
            # is what makes "exists" and "is set" the same question here.)
            res = _db[DB_COLLECTION].update_one(
                # The second guard is not paranoia: setting `recipients.99.x`
                # on a 10-element array pads it with 89 nulls. Indexes always
                # come from a lookup, so this can only fire if a campaign were
                # edited underneath us — but the cost of being wrong is a
                # corrupted recipient list.
                {"campaign_id": campaign_id, path: {"$exists": False},
                 f"recipients.{int(index)}": {"$exists": True}}, ops)
            if res.modified_count:
                return True
            if extra:                       # lost the race; still record the detail
                _db[DB_COLLECTION].update_one(
                    {"campaign_id": campaign_id},
                    {"$set": {f"recipients.{int(index)}.{k}": v for k, v in extra.items()}})
            return False

        docs = _json_read()
        for d in docs:
            if d.get("campaign_id") != campaign_id:
                continue
            rs = d.get("recipients") or []
            if not 0 <= int(index) < len(rs):
                return False
            r = rs[int(index)]
            first = r.get(field) in (None, "")
            if first:
                r[field] = _jsonable(value)
                if counter:
                    d[counter] = int(d.get(counter) or 0) + 1
            for k, v in (extra or {}).items():
                r[k] = _jsonable(v)
            _json_write(docs)
            return first
    return False


def update_recipient(campaign_id, index, fields):
    """Unconditional field writes on one recipient — no counters, no ordering."""
    _init()
    if not fields:
        return
    with _LOCK:
        if _backend == "mongo":
            _db[DB_COLLECTION].update_one(
                {"campaign_id": campaign_id},
                {"$set": {f"recipients.{int(index)}.{k}": v for k, v in fields.items()}})
            return
        docs = _json_read()
        for d in docs:
            if d.get("campaign_id") == campaign_id:
                rs = d.get("recipients") or []
                if 0 <= int(index) < len(rs):
                    for k, v in fields.items():
                        rs[int(index)][k] = _jsonable(v)
        _json_write(docs)


def bump(campaign_id, counter, by=1):
    """Increment a campaign-level counter. Used for the reply tally, which is
    per message rather than per recipient — one lead may answer three times."""
    _init()
    with _LOCK:
        if _backend == "mongo":
            _db[DB_COLLECTION].update_one({"campaign_id": campaign_id},
                                          {"$inc": {counter: int(by)}})
            return
        docs = _json_read()
        for d in docs:
            if d.get("campaign_id") == campaign_id:
                d[counter] = int(d.get(counter) or 0) + int(by)
        _json_write(docs)


def _jsonable(value):
    return _iso(value) if isinstance(value, dt.datetime) else value


# Only these fields of the campaign come back from a recipient lookup. The
# recipient array is the bulk of a campaign document and the webhook needs one
# element of it, so on Mongo the match, the index and the single element are
# all computed server-side and only that much crosses the wire.
_LOOKUP_FIELDS = {"_id": 0, "campaign_id": 1, "name": 1, "status": 1, "created_at": 1,
                  "started_at": 1, "template_name": 1, "dry_run": 1,
                  "batch_index": 1, "batch_count": 1, "group_id": 1}


def _lookup_pipeline(field, value, limit):
    key = f"$recipients.{field}"
    idx = {"$indexOfArray": [key, value]}
    project = dict(_LOOKUP_FIELDS)
    project["index"] = idx
    project["recipient"] = {"$arrayElemAt": ["$recipients", idx]}
    return [
        {"$match": {f"recipients.{field}": value}},
        {"$sort": {"created_at": -1}},
        {"$limit": int(limit)},
        {"$project": project},
    ]


def _json_lookup(field, value, limit):
    out = []
    for d in sorted(_json_read(), key=lambda x: x.get("created_at") or "", reverse=True):
        for i, r in enumerate(d.get("recipients") or []):
            if r.get(field) == value:
                hit = {k: d.get(k) for k in _LOOKUP_FIELDS if k != "_id"}
                hit.update(index=i, recipient=r)
                out.append(_revive(hit))
                break
        if len(out) >= limit:
            break
    return out


def find_by_wamid(wamid, limit=3):
    """
    Campaigns whose recipient list carries this WhatsApp message id.

    This is the exact match — the id names one specific message we sent — and
    it is what every status event should resolve through when the send or the
    templateMessageSent event gave us an id to store.
    """
    if not wamid:
        return []
    _init()
    with _LOCK:
        if _backend == "mongo":
            return [_revive(d) for d in _db[DB_COLLECTION].aggregate(
                _lookup_pipeline("wamid", str(wamid), limit))]
        return _json_lookup("wamid", str(wamid), limit)


def find_by_phone(phone, limit=8):
    """
    Campaigns that have this number as a recipient, newest first.

    The fallback when no message id matches — which is most of the time, since
    WATI's send response usually carries no id at all. Several campaigns may
    have messaged the same number; picking between them is the caller's job
    (wati_webhook._pick_target), because only it knows when the event happened.
    """
    if not phone:
        return []
    _init()
    with _LOCK:
        if _backend == "mongo":
            return [_revive(d) for d in _db[DB_COLLECTION].aggregate(
                _lookup_pipeline("phone", str(phone), limit))]
        return _json_lookup("phone", str(phone), limit)


def collection(name):
    """
    A sibling Mongo collection in the same database, or None on the JSON
    backend. The webhook keeps its raw event log next to the campaigns rather
    than opening a second connection to the same cluster.
    """
    _init()
    return _db[name] if _backend == "mongo" else None


def data_dir():
    """Where the JSON backend keeps its files — the webhook's fallback log
    lives here too, so there is one directory to gitignore and one to back up."""
    path = os.path.dirname(JSON_PATH)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:                              # read-only filesystem
        pass
    return path


if __name__ == "__main__":
    kind, note = backend()
    print(f"backend: {kind} — {note}")
    for c in list_campaigns(limit=20):
        print(f"  {c['campaign_id'][:8]}  {c['status']:<10} {c['sent']}/{c['total']:<6} {c['name']}")

#!/usr/bin/env python3
"""
wati_webhook.py

The other half of a campaign: what happened *after* the send.

`campaign_runner.py` knows one thing — WATI answered the send with 200 OK. It
does not know whether the phone ever received the message, whether anybody
opened it, whether anybody wrote back, or whether Meta quietly refused it a
few minutes later. None of that comes back on the API call; all of it arrives
on a webhook WATI posts to us, minutes to hours afterwards.

So this module takes those posts and turns them into the numbers the campaigns
dashboard shows:

    sent        WATI accepted it            (the runner already knew this)
    delivered   it reached the handset
    read        the contact opened it       <- "how many leads saw the campaign"
    replied     the contact wrote back
    undelivered Meta refused it after acceptance, with its own reason code

Set it up in WATI: **Settings -> Webhooks -> Add Webhook**, URL

    https://<your-public-host>/webhooks/wati?token=<WATI_WEBHOOK_TOKEN>

and tick the events listed in `WANTED_EVENTS` below. `/webhooks` in this app
shows the exact URL to paste, whether anything has arrived yet, and the raw
feed of the last events — which is the fastest way to tell a working hook from
a mistyped one.

Two things about WATI's webhook that this file is shaped by, both learned the
hard way in the sibling `wati_cleanup` repo:

* **Every event arrives twice.** WATI fires `sentMessageREAD` and
  `sentMessageREAD_v2` for the same read. Only the `_v2` twin carries
  `localMessageId`, so the WhatsApp id (`whatsappMessageId`, the "wamid.…") is
  the only key both halves share — and therefore the only sane dedupe key.
* **`owner: false` means the contact sent it, not us.** WATI still stamps
  those inbound messages with `statusString: "SENT"`, so classifying on the
  status alone counts a lead's reply as one of our own sends.

Storage sits next to the campaigns: Mongo when `campaign_store` has it,
`data/webhook_events.json` (capped, newest kept) when it does not. Every event
is stored whether or not it could be matched to a campaign — an unattributed
reply from someone we never messaged is still worth reading, and an unmatched
pile is the symptom that says attribution is broken.
"""

import datetime as dt
import json
import os
import threading
import time

import campaign_store as store

EVENTS_COLLECTION = "wati_message_events"
JSON_NAME = "webhook_events.json"
JSON_CAP = 5000              # newest kept; the JSON backend is a fallback, not an archive

# How long the raw event feed is kept. Every number the dashboard shows is
# stamped on the campaign itself and is permanent; this is only the raw log
# behind it, which a busy account fills at a few events per message and would
# otherwise grow forever. 0 keeps everything.
RETENTION_DAYS = int(os.environ.get("WATI_EVENT_RETENTION_DAYS", "180"))

# How far back a status event may reach when it has to be matched by phone
# number alone. Longer than any campaign takes to finish, short enough that a
# reply six months later does not attach itself to a campaign nobody remembers.
PHONE_MATCH_DAYS = 45

# What to tick in WATI's webhook settings. Everything else is accepted and
# stored anyway — this list is what the setup page tells the operator to
# enable, not a filter.
WANTED_EVENTS = [
    ("templateMessageSent", "confirms our send and carries the message id — tick this first"),
    ("sentMessageDELIVERED", "reached the handset"),
    ("sentMessageREAD", "the contact opened it"),
    ("sentMessageREPLIED", "the contact answered it"),
    ("sentMessageFAILED", "Meta refused it — carries the error code"),
    ("message", "the reply itself, with its text"),
    ("newContactMessageReceived", "a first-time reply from a number not in WATI yet"),
]

_LOCK = threading.RLock()

# What the webhook prints to the console (systemd journal in production).
#
#   summary   one line per event that actually told us something — the default
#   verbose   every event, plus the raw payload
#   off       nothing; the /webhooks feed is then the only view
#
# The default is deliberately not "off": a campaign's whole point is knowing
# who received it, and watching the lines arrive is how an operator sees that
# happening without keeping a browser tab open. Duplicates are dropped from
# `summary` because WATI fires each event twice and a doubled log reads like
# doubled delivery.
LOG_MODE = (os.environ.get("WATI_WEBHOOK_LOG") or "summary").strip().lower()


# --------------------------------------------------------------------------- #
# Meta's failure codes
# --------------------------------------------------------------------------- #
# Codes are matched as codes, never against the provider's prose: the sentence
# is written for a human reading a log and is free to be reworded, and keying a
# report on its wording would break silently when it is.
#
# `bucket` is what the dashboard groups by; `label` is what it prints; `advice`
# is the one line an operator needs to know what, if anything, to do about it.
META_CODES = {
    "131049": ("meta-blocked", "Meta held it back (healthy-ecosystem limit)",
               "Meta is pacing marketing to this person. Nothing is wrong with the number or "
               "the template — the same message often lands if it is retried in a few days."),
    "130472": ("meta-blocked", "User in an experiment holdout group",
               "Meta excluded this person from marketing delivery for now. Retry later."),
    "131048": ("meta-blocked", "Spam-rate limit hit for this recipient",
               "The account's quality rating is under pressure. Slow down and improve targeting."),
    "131047": ("window-closed", "Outside the 24-hour window",
               "More than 24h since this contact last wrote in, so only templates may be sent — "
               "and this one was refused. Nothing to fix per-contact."),
    "131026": ("undeliverable", "Number cannot receive WhatsApp messages",
               "Not on WhatsApp, or unable to receive. Retrying always fails — drop it from the list."),
    "131050": ("opted-out", "Contact turned marketing messages off",
               "A real opt-out, made inside WhatsApp. Do not message this number again."),
    "132000": ("template-error", "Template parameter count mismatch",
               "The template expects a different number of parameters than were sent. Fix the mapping."),
    "132001": ("template-error", "Template does not exist or is not approved",
               "Wrong name, wrong language, or Meta withdrew approval. Check the template list."),
    "132005": ("template-error", "Template text too long / hydration failed",
               "A parameter value is too long for the template. Shorten it."),
    "132007": ("template-error", "Template format-character policy violation",
               "Parameter contains newlines, tabs or 4+ consecutive spaces. Clean the value."),
    "132012": ("template-error", "Template parameter format mismatch",
               "A parameter does not match the format the template declares."),
    "132015": ("template-error", "Template is paused by Meta",
               "Quality feedback paused this template. Use another until it recovers."),
    "133010": ("account", "Phone number not registered",
               "The sending number is not registered with Meta. This is an account problem, not a contact one."),
    "133004": ("account", "Server temporarily unavailable",
               "Meta-side outage. Retry."),
    "80007": ("account", "Rate limit hit",
              "Sending faster than the account's tier allows. Lower the rate limit on the campaign."),
    "470": ("window-closed", "Message failed to send (24-hour window)",
            "WATI's legacy code for a send outside the customer-care window."),
}

BUCKET_LABEL = {
    "meta-blocked": "Blocked by Meta",
    "window-closed": "Outside the 24h window",
    "undeliverable": "Not reachable on WhatsApp",
    "opted-out": "Opted out of marketing",
    "template-error": "Template problem",
    "account": "Account / rate limit",
    "unknown": "Unclassified",
}


def classify_failure(code, detail=None):
    """
    {'code', 'bucket', 'label', 'advice'} for a failure event.

    An unrecognised code classifies as `unknown` rather than being guessed at
    from the message text — an honest "we do not know what this means" is a
    better thing to show an operator than a confident wrong bucket.
    """
    key = str(code).strip() if code not in (None, "") else ""
    if key in META_CODES:
        bucket, label, advice = META_CODES[key]
        return {"code": key, "bucket": bucket, "label": label, "advice": advice}
    return {"code": key, "bucket": "unknown",
            "label": f"Error {key}" if key else "Refused without a code",
            "advice": (str(detail)[:300] if detail else
                       "WATI reported a failure with no code. The raw event is in the feed on /webhooks.")}


# --------------------------------------------------------------------------- #
# reading WATI's payload
# --------------------------------------------------------------------------- #
def _first(*values):
    for v in values:
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return ""


def phone_of(body):
    """Digits only, no '+' — the same shape campaigns store recipients in."""
    raw = _first(body.get("waId"), body.get("whatsappNumber"), body.get("phone"),
                 body.get("senderPhone"), body.get("customerPhone"))
    return "".join(ch for ch in raw if ch.isdigit())


def event_type_of(body):
    return _first(body.get("eventType"), body.get("type"), body.get("statusString"),
                  body.get("event")) or "unknown"


def wamid_of(body):
    """
    WhatsApp's own id for the message this event is about.

    Deliberately preferred over WATI's `localMessageId`: only the `_v2` twin of
    each event carries the local id, so keying on it gives the two halves of
    one event different identities and the dedupe never collapses them.
    """
    return _first(body.get("whatsappMessageId"), body.get("messageId"), body.get("id"))


def local_id_of(body):
    return _first(body.get("localMessageId"))


def reply_context_of(body):
    """
    On an inbound reply, the id of the message being replied *to* — i.e. of the
    campaign message we sent. The event's own id belongs to the contact's new
    message and matches nothing of ours, so this is the link back.
    """
    return _first(body.get("replyContextId"))


def text_of(body):
    raw = body.get("text") or body.get("messageText")
    if not raw and isinstance(body.get("data"), dict):
        raw = body["data"].get("text")
    return str(raw) if isinstance(raw, (str, int)) else ""


def failed_code_of(body):
    return _first(body.get("failedCode"), body.get("errorCode"), body.get("failed_code"))


def failed_detail_of(body):
    return _first(body.get("failedDetail"), body.get("errorMessage"), body.get("failed_detail"),
                  body.get("eventDescription"))


def normalize_status(body, event_type=None):
    """
    Collapse WATI's many event names onto the handful of states worth counting.

    Matched loosely, and across `eventType`, `statusString` and `status`
    together, because WATI spells the same state differently depending on which
    shape of event it is sending.
    """
    if body.get("owner") is False:
        # The contact sent this one. WATI stamps it "SENT" all the same (their
        # phone did send it), which without this line reads as one of our own
        # sends and quietly inflates the sent count with inbound traffic.
        return "received"

    event_type = event_type or event_type_of(body)
    hay = f"{event_type} {body.get('statusString') or ''} {body.get('status') or ''}".lower()
    if "fail" in hay or "undeliver" in hay:
        return "failed"
    if "repl" in hay:
        return "replied"
    if "read" in hay or "seen" in hay:
        return "read"
    if "deliver" in hay:
        return "delivered"
    if "receiv" in hay:
        return "received"
    if "sent" in hay or "send" in hay:
        return "sent"
    return "unknown"


def is_send_event(event_type):
    """`templateMessageSent` / `sessionMessageSent` and their `_v2` twins — the
    only events carrying the phone number *and* the message id, which is what
    makes them able to teach a recipient the id of the message it received."""
    return "messagesent" in (event_type or "").lower().replace("_", "")


def timestamp_of(body):
    """WATI sends a unix `timestamp`, an ISO `created`, or neither."""
    raw = body.get("timestamp")
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().isdigit()):
        seconds = int(raw)
        if seconds > 10_000_000_000:          # milliseconds
            seconds //= 1000
        try:
            return dt.datetime.fromtimestamp(seconds, dt.timezone.utc)
        except (ValueError, OSError):
            pass
    for key in ("created", "createdAt", "eventTime"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            try:
                parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
    return store.now_utc()


# --------------------------------------------------------------------------- #
# the raw event log
# --------------------------------------------------------------------------- #
_json_path = None


def _json_file():
    global _json_path
    if _json_path is None:
        _json_path = os.path.join(store.data_dir(), JSON_NAME)
    return _json_path


def _events():
    """The Mongo collection, or None when the JSON fallback is in play."""
    coll = store.collection(EVENTS_COLLECTION)
    if coll is not None and not getattr(_events, "_indexed", False):
        try:
            # Dedupe on (message id, normalised status) rather than on the raw
            # event name: WATI reports sent, delivered and read under names
            # that vary by tenant, and the `_v2` twin of every event repeats
            # the same fact. Inbound messages are excluded — a contact who
            # writes three times must keep all three, and each carries its own
            # id anyway.
            coll.create_index(
                [("dedupe_key", 1)], unique=True,
                partialFilterExpression={"dedupe_key": {"$type": "string"}})
            coll.create_index([("received_at", -1)])
            if RETENTION_DAYS > 0:
                coll.create_index("received_at", name="ttl_received_at",
                                  expireAfterSeconds=RETENTION_DAYS * 86400)
            coll.create_index([("campaign_id", 1), ("received_at", -1)])
            coll.create_index([("phone", 1), ("received_at", -1)])
        except Exception as e:                 # noqa: BLE001 — never break ingestion over an index
            # The usual cause is WATI_EVENT_RETENTION_DAYS having been changed:
            # Mongo refuses to redefine a TTL index with a different expiry.
            # Worth a line in the log, not worth refusing events over.
            print(f"[wati/webhook] index setup skipped: {e}")
        _events._indexed = True
    return coll


def _dedupe_key(status, wamid):
    """
    None means "never deduplicate this one".

    Only the delivery states are idempotent: a message is delivered once, and a
    second report of it is genuinely redundant. Inbound messages are not — three
    replies from one lead are three facts.
    """
    if status == "received" or not wamid:
        return None
    return f"{wamid}:{status}"


def _store_event(record):
    """Persist one event. Returns False if it was a duplicate we already had."""
    coll = _events()
    if coll is not None:
        try:
            coll.insert_one(dict(record))
            return True
        except Exception as e:                 # noqa: BLE001
            if "E11000" in str(e):             # the dedupe index doing its job
                return False
            raise
    with _LOCK:
        rows = _read_json()
        key = record.get("dedupe_key")
        if key and any(r.get("dedupe_key") == key for r in rows):
            return False
        rows.append(json.loads(json.dumps(record, default=_iso)))
        _write_json(rows[-JSON_CAP:])
        return True


def _iso(value):
    if isinstance(value, dt.datetime):
        return value.astimezone(dt.timezone.utc).isoformat()
    return str(value)


def _read_json():
    path = _json_file()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (ValueError, OSError):
        return []


def _write_json(rows):
    path = _json_file()
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rows, f, default=_iso)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# attribution — which campaign, which recipient
# --------------------------------------------------------------------------- #
def _pick_target(phone, wamid, reply_to, when):
    """
    Find the recipient row this event is about.

    Tried most-exact first, because a message id names one specific send while
    a phone number names everyone we ever sent to:

    1. the event's own message id — a status event about a message we sent;
    2. `replyContextId` — an inbound reply's own id matches nothing of ours,
       but the id of the message it answers does;
    3. the phone number, taking the most recent campaign that had actually sent
       to this number before the event happened.

    Step 3's "before the event" test is what keeps a delivery receipt for
    yesterday's campaign from being credited to one created this morning, which
    is the usual way a funnel ends up with more reads than sends.
    """
    for candidate_id in (wamid, reply_to):
        if candidate_id:
            hits = store.find_by_wamid(candidate_id)
            if hits:
                return hits[0], "message-id"

    if not phone:
        return None, None

    hits = store.find_by_phone(phone)
    if not hits:
        return None, None

    cutoff = when - dt.timedelta(days=PHONE_MATCH_DAYS)
    best = None
    for hit in hits:
        recipient = hit.get("recipient") or {}
        sent_at = _parse(recipient.get("at")) or hit.get("started_at") or hit.get("created_at")
        # The five minutes is clock skew allowance, not slack: the event's
        # timestamp comes from WATI and the send time from this machine, and a
        # receipt that legitimately arrives a second "before" its own send must
        # not be thrown away over it.
        if not sent_at or sent_at > when + dt.timedelta(minutes=5) or sent_at < cutoff:
            continue
        # "pending" means the walk never reached this recipient, so no message
        # was ever sent to them — nothing about them can be the subject of an
        # event, not a receipt and not a reply.
        if (recipient.get("status") or "pending") == "pending":
            continue
        if best is None or sent_at > best[1]:
            best = (hit, sent_at)
    if best:
        return best[0], "phone"
    return None, None


def _parse(value):
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# applying an event to a campaign
# --------------------------------------------------------------------------- #
# The delivery ladder, in order. A message that reaches one rung has passed
# every rung below it, whether or not Meta bothered to send the receipt for it:
# a read message was delivered, and a message someone answered was both. Meta
# genuinely does drop receipts — read without delivered is common — and a funnel
# where the reply count exceeds the read count is one nobody believes, so the
# lower rungs are filled in rather than left as holes.
LADDER = [("delivered", "delivered_at"), ("read", "read_at"), ("replied", "replied_at")]


def _climb(campaign_id, index, stamp, upto):
    """Mark every rung up to and including `upto`, counting only the rungs this
    call is what set. Returns the rungs that actually moved."""
    changed = []
    for counter, field in LADDER:
        if store.set_recipient_field_once(campaign_id, index, field, stamp, counter=counter):
            changed.append(counter)
        if counter == upto:
            break
    return changed


def _apply(target, status, when, body, wamid, reply_to, failure):
    """
    Move one recipient along the ladder. Returns the transitions this event
    actually caused — empty when it told us nothing new, which is the normal
    outcome for a redelivery.

    Failure sits outside the ladder: it is what Meta says *instead* of
    delivering, and it is counted apart from the send-time `failed` so the two
    causes stay tellable apart — WATI refusing the API call is a different
    problem from Meta refusing the message WATI accepted.
    """
    cid, index = target["campaign_id"], target["index"]
    recipient = target.get("recipient") or {}
    stamp = _iso(when)
    changed = []

    # A recipient's `wamid` is the id of the message WE sent, and only that.
    # An inbound message carries its own id — storing that would point the
    # recipient at the contact's message, so every later receipt for our
    # message would stop matching. What links an inbound message back to us is
    # replyContextId, so on those it is that id, or nothing.
    our_id = reply_to if status == "received" else wamid
    if our_id and not recipient.get("wamid"):
        store.set_recipient_field_once(cid, index, "wamid", our_id)

    if status in ("delivered", "read"):
        changed = _climb(cid, index, stamp, status)

    elif status == "replied":
        changed = _climb(cid, index, stamp, "replied")

    elif status == "received":
        # The contact's own message. `sentMessageREPLIED` is the status stamped
        # on our message; this is the reply itself, and it is the one carrying
        # the words. Either may arrive first, and whichever does is what marks
        # the recipient as having replied.
        fields = {"last_reply_at": stamp}
        body_text = text_of(body)
        if body_text:
            fields["reply_text"] = body_text[:1000]
        store.update_recipient(cid, index, fields)
        store.bump(cid, "reply_messages")     # per message: one lead may answer three times
        changed = _climb(cid, index, stamp, "replied")

    elif status == "failed":
        extra = {"failed_code": failure["code"], "failed_detail": failed_detail_of(body)[:400],
                 "failed_bucket": failure["bucket"], "failed_label": failure["label"]}
        if store.set_recipient_field_once(cid, index, "failed_at", stamp,
                                          counter="undelivered", extra=extra):
            changed.append("undelivered")

    return changed


# --------------------------------------------------------------------------- #
# the console line
# --------------------------------------------------------------------------- #
# Status names are padded to a fixed width so a scrolling log reads as columns:
# the eye finds "failed" in a stream of "delivered" far faster when they start
# in the same place.
_LOG_WIDTH = 9


def _log_event(record, result, body):
    """One line per event. Never raises — logging must not cost an event."""
    if LOG_MODE == "off":
        return
    if LOG_MODE != "verbose" and result.get("duplicate") and not result.get("applied"):
        return                                  # the _v2 twin, or a redelivery
    try:
        when = (record.get("received_at") or store.now_utc()).astimezone(store.IST).strftime("%H:%M:%S")
        status = (record.get("status") or "?")
        phone = record.get("phone") or "—"

        if record.get("campaign_id"):
            who = record.get("recipient_name") or ""
            where = f'"{record.get("campaign_name") or record["campaign_id"][:8]}"'
            what = f'{who + " · " if who else ""}{where} (by {record.get("matched_by")})'
        else:
            what = "unmatched — no campaign here sent to this number"

        extra = ""
        if status == "failed":
            extra = f' · {record.get("failed_code") or "no code"} {record.get("failed_label") or ""}'
        elif record.get("text"):
            extra = f' · "{record["text"][:60]}"'
        applied = f' -> {"+".join(result["applied"])}' if result.get("applied") else ""

        print(f"[wati/webhook] {when} {status:<{_LOG_WIDTH}} {phone:<14} {what}{extra}{applied}",
              flush=True)
        if LOG_MODE == "verbose":
            print(f"[wati/webhook]          payload: {json.dumps(body, default=str)[:800]}", flush=True)
    except Exception as e:                      # noqa: BLE001
        print(f"[wati/webhook] (could not format log line: {type(e).__name__}: {e})", flush=True)


# --------------------------------------------------------------------------- #
# the entry point app.py calls
# --------------------------------------------------------------------------- #
def handle(body):
    """
    Process one webhook payload. Never raises: the caller must answer 200 to
    anything well-formed, because a non-2xx makes WATI retry the same event —
    and an event we cannot attribute is not an error, it is just an event about
    someone no campaign here messaged.

    Returns a small dict describing what was done, which is what the setup page
    and the `--replay` tool print.
    """
    if not isinstance(body, dict):
        return {"ok": False, "reason": "payload was not a JSON object"}

    event_type = event_type_of(body)
    status = normalize_status(body, event_type)
    phone = phone_of(body)
    wamid = wamid_of(body)
    reply_to = reply_context_of(body)
    when = timestamp_of(body)
    failure = classify_failure(failed_code_of(body), failed_detail_of(body)) if status == "failed" else None

    target, how = _pick_target(phone, wamid, reply_to, when)

    # A send confirmation is the one event carrying both the number and the id,
    # so it is what teaches a recipient the id of the message it was sent —
    # every status event afterwards then matches exactly instead of by phone.
    if target and is_send_event(event_type) and wamid:
        if not (target.get("recipient") or {}).get("wamid"):
            store.set_recipient_field_once(target["campaign_id"], target["index"], "wamid", wamid)

    changed = []
    if target and status in ("delivered", "read", "replied", "received", "failed"):
        try:
            changed = _apply(target, status, when, body, wamid, reply_to, failure)
        except Exception as e:                 # noqa: BLE001 — recording beats reporting
            changed = []
            print(f"[wati/webhook] could not apply {status} to "
                  f"{target.get('campaign_id')}: {type(e).__name__}: {e}")

    record = {
        "received_at": store.now_utc(),
        "event_at": when,
        "event_type": event_type,
        "status": status,
        "phone": phone or (target or {}).get("recipient", {}).get("phone") or "",
        "wamid": wamid,
        "local_message_id": local_id_of(body),
        "reply_to_wamid": reply_to,
        "text": text_of(body)[:1000],
        "failed_code": failure["code"] if failure else "",
        "failed_bucket": failure["bucket"] if failure else "",
        "failed_label": failure["label"] if failure else "",
        "failed_detail": failed_detail_of(body)[:400] if status == "failed" else "",
        "campaign_id": (target or {}).get("campaign_id"),
        "campaign_name": (target or {}).get("name"),
        "recipient_index": (target or {}).get("index"),
        "recipient_name": ((target or {}).get("recipient") or {}).get("name"),
        "matched_by": how,
        "applied": changed,
        "dedupe_key": _dedupe_key(status, wamid),
        "payload": body,
    }
    fresh = _store_event(record)
    _health_cache["value"] = None      # the badge must not still say "nothing yet"

    result = {"ok": True, "status": status, "event_type": event_type, "phone": phone,
              "campaign_id": record["campaign_id"], "matched_by": how,
              "applied": changed, "duplicate": not fresh}
    _log_event(record, result, body)
    return result


# --------------------------------------------------------------------------- #
# reading it back
# --------------------------------------------------------------------------- #
def recent_events(limit=50, campaign_id=None, status=None, unmatched_only=False):
    query = {}
    if campaign_id:
        query["campaign_id"] = campaign_id
    if status:
        query["status"] = status
    if unmatched_only:
        query["campaign_id"] = None

    coll = _events()
    if coll is not None:
        rows = list(coll.find(query, {"_id": 0, "payload": 0})
                    .sort("received_at", -1).limit(int(limit)))
    else:
        with _LOCK:
            rows = [r for r in reversed(_read_json())
                    if all(r.get(k) == v for k, v in query.items())][:int(limit)]
    for r in rows:
        r["received_at"] = _parse(r.get("received_at"))
        r["event_at"] = _parse(r.get("event_at"))
    return rows


# health() is asked the same question by every row of the campaigns dashboard —
# "has this webhook ever received anything?" — and that page re-renders itself
# every five seconds. Uncached, a hundred campaigns is two hundred count
# queries every five seconds to answer one question. The window is deliberately
# shorter than the page's own refresh, so nothing on screen is ever stale by
# more than one tick.
_HEALTH_TTL = 4.0
_health_cache = {"at": 0.0, "value": None}


def health(force=False):
    """
    Everything the setup page needs to say whether this is working, without
    ever raising — it is a status badge, and a status badge that 500s is worse
    than no badge at all.
    """
    now = time.monotonic()
    if not force and _health_cache["value"] is not None and now - _health_cache["at"] < _HEALTH_TTL:
        return _health_cache["value"]

    info = {"configured": bool(token()), "backend": store.backend()[0], "total": 0,
            "matched": 0, "unmatched": 0, "last_at": None, "last_status": None,
            "by_status": {}, "error": None}
    try:
        coll = _events()
        if coll is not None:
            info["total"] = coll.count_documents({})
            info["unmatched"] = coll.count_documents({"campaign_id": None})
            for row in coll.aggregate([{"$group": {"_id": "$status", "n": {"$sum": 1}}}]):
                info["by_status"][row["_id"] or "unknown"] = row["n"]
        else:
            with _LOCK:
                rows = _read_json()
            info["total"] = len(rows)
            info["unmatched"] = sum(1 for r in rows if not r.get("campaign_id"))
            for r in rows:
                key = r.get("status") or "unknown"
                info["by_status"][key] = info["by_status"].get(key, 0) + 1
        info["matched"] = info["total"] - info["unmatched"]
        latest = recent_events(limit=1)
        if latest:
            info["last_at"] = latest[0].get("received_at")
            info["last_status"] = latest[0].get("status")
    except Exception as e:                     # noqa: BLE001
        info["error"] = str(e)
    _health_cache.update(at=now, value=info)
    return info


def funnel(campaign):
    """
    The delivery funnel for one campaign document, as counts and percentages.

    Percentages are of `sent`, not of `total`: what fraction of the messages
    that actually went out were read is a real number, while a fraction of a
    list that includes people the send failed for is not.
    """
    sent = int(campaign.get("sent") or 0)
    base = max(1, sent)
    out = {
        "total": int(campaign.get("total") or 0),
        "sent": sent,
        "failed": int(campaign.get("failed") or 0),
        "delivered": int(campaign.get("delivered") or 0),
        "read": int(campaign.get("read") or 0),
        "replied": int(campaign.get("replied") or 0),
        "undelivered": int(campaign.get("undelivered") or 0),
        "reply_messages": int(campaign.get("reply_messages") or 0),
    }
    out["pct"] = {k: round(out[k] * 100 / base) for k in
                  ("delivered", "read", "replied", "undelivered")}
    out["waiting"] = max(0, sent - out["delivered"] - out["undelivered"])
    out["tracked"] = out["delivered"] + out["undelivered"] + out["read"] + out["replied"] > 0
    return out


def failure_breakdown(campaign):
    """[(bucket, label, count, advice, [phones])] for one campaign, worst first —
    the "why did Meta refuse these" table on the detail page."""
    groups = {}
    for r in campaign.get("recipients") or []:
        if not r.get("failed_at"):
            continue
        bucket = r.get("failed_bucket") or "unknown"
        entry = groups.setdefault(bucket, {"bucket": bucket, "label": BUCKET_LABEL.get(bucket, bucket),
                                           "count": 0, "codes": {}, "phones": []})
        entry["count"] += 1
        code = r.get("failed_code") or "—"
        entry["codes"][code] = entry["codes"].get(code, 0) + 1
        if len(entry["phones"]) < 50:
            entry["phones"].append(r.get("phone"))
    for entry in groups.values():
        top = max(entry["codes"], key=entry["codes"].get) if entry["codes"] else ""
        if top and top != "—":
            detail = classify_failure(top)
            entry["advice"] = detail["advice"]
            # One code in the group means the specific reason can be named
            # instead of the bucket it belongs to — "Meta held it back
            # (healthy-ecosystem limit)" is worth more to whoever is reading
            # this than "Blocked by Meta".
            if len(entry["codes"]) == 1:
                entry["label"] = detail["label"]
        else:
            entry["advice"] = ""
        entry["code_summary"] = ", ".join(f"{c} ×{n}" for c, n in
                                          sorted(entry["codes"].items(), key=lambda kv: -kv[1]))
    return sorted(groups.values(), key=lambda g: -g["count"])


def campaign_replies(campaign, limit=200):
    """Recipients who wrote back, newest first, with what they said."""
    out = [r for r in (campaign.get("recipients") or []) if r.get("replied_at")]
    out.sort(key=lambda r: r.get("last_reply_at") or r.get("replied_at") or "", reverse=True)
    return out[:limit]


def token():
    """The shared secret WATI must send back. No default and no generated
    fallback: a webhook that accepts anything is a webhook anyone can use to
    forge delivery numbers, so an unset token disables the endpoint outright
    and the setup page says so in red."""
    return (os.environ.get("WATI_WEBHOOK_TOKEN")
            or _env().get("WATI_WEBHOOK_TOKEN") or "").strip()


def _env():
    try:
        import wati_client
        return wati_client.load_env()
    except Exception:                          # noqa: BLE001
        return {}


def clean_token(value):
    """
    Strip the punctuation a copy-paste picks up: surrounding whitespace and
    surrounding quotes.

    Not cosmetic. WATI's webhook URL was once saved with a trailing `"` on the
    end — the query string arrived as `token=…4830%22` and every event was
    rejected, which presents as total silence with nothing in WATI's own UI to
    explain it. A stray quote cannot turn a wrong token into a right one, so
    forgiving it costs nothing and saves an afternoon.
    """
    text = str(value or "").strip()
    while len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text.strip('"\'').strip()


def check_token(supplied):
    """Constant-time compare, so a wrong token cannot be narrowed down by
    timing the response."""
    import hmac
    expected = clean_token(token())
    if not expected:
        return False
    return hmac.compare_digest(clean_token(supplied), expected)


# --------------------------------------------------------------------------- #
# command line — setup check and self-test
# --------------------------------------------------------------------------- #
def _selftest():
    """
    Drive a synthetic campaign through the whole event sequence.

    Writes to the real store — deliberately, because the point is to prove the
    store's compare-and-set counters behave — but as a dry-run campaign that is
    parked `completed` before the scheduler can see it, and deleted at the end.
    """
    import uuid
    phone = "9199" + uuid.uuid4().hex[:8].translate(str.maketrans("abcdef", "123456"))
    doc = store.create("SELFTEST — delete me", "selftest_template",
                       [{"name": "Test", "phone": phone, "params": {}}], dry_run=True)
    cid = doc["campaign_id"]
    store.update(cid, {"status": "completed", "sent": 1, "finished_at": store.now_utc()})
    store.record_results(cid, {0: {"status": "sent", "error": None,
                                   "at": store.now_utc().isoformat()}}, 1, 1, 0)

    wamid = "wamid.SELFTEST" + uuid.uuid4().hex[:10]
    events = [
        {"eventType": "templateMessageSent", "waId": phone, "whatsappMessageId": wamid,
         "owner": True, "statusString": "SENT"},
        {"eventType": "sentMessageDELIVERED", "whatsappMessageId": wamid, "statusString": "DELIVERED"},
        {"eventType": "sentMessageDELIVERED_v2", "whatsappMessageId": wamid,
         "localMessageId": "guid-1", "statusString": "DELIVERED"},      # the twin — must not double-count
        {"eventType": "sentMessageREAD", "whatsappMessageId": wamid, "statusString": "READ"},
        {"eventType": "message", "waId": phone, "owner": False, "text": "Yes, interested",
         "replyContextId": wamid, "whatsappMessageId": "wamid.REPLY1", "statusString": "SENT"},
    ]
    results = [handle(e) for e in events]
    fresh = store.get(cid)
    f = funnel(fresh)
    ok = (f["delivered"], f["read"], f["replied"]) == (1, 1, 1)
    print(f"  campaign {cid[:8]}  phone {phone}")
    for e, r in zip(events, results):
        print(f"    {e['eventType']:<28} -> {r['status']:<10} matched={r['matched_by'] or '—':<10} "
              f"applied={r['applied']} dup={r['duplicate']}")
    print(f"  funnel: sent={f['sent']} delivered={f['delivered']} read={f['read']} "
          f"replied={f['replied']} undelivered={f['undelivered']}")
    print(f"  reply text: {(fresh['recipients'][0].get('reply_text') or '—')!r}")

    # …and a failure, on a second synthetic recipient, to prove the Meta
    # classification lands on the recipient and not just in the log.
    doc2 = store.create("SELFTEST fail — delete me", "selftest_template",
                        [{"name": "Test2", "phone": phone[:-1] + "7", "params": {}}], dry_run=True)
    cid2 = doc2["campaign_id"]
    store.update(cid2, {"status": "completed", "sent": 1, "finished_at": store.now_utc()})
    store.record_results(cid2, {0: {"status": "sent", "error": None,
                                    "at": store.now_utc().isoformat()}}, 1, 1, 0)
    handle({"eventType": "sentMessageFAILED", "waId": phone[:-1] + "7", "statusString": "FAILED",
            "whatsappMessageId": "wamid.FAILTEST", "failedCode": "131049",
            "failedDetail": "This message was not delivered to maintain healthy ecosystem engagement."})
    fresh2 = store.get(cid2)
    blocked = failure_breakdown(fresh2)
    ok2 = fresh2.get("undelivered") == 1 and blocked and blocked[0]["bucket"] == "meta-blocked"
    print(f"  blocked: undelivered={fresh2.get('undelivered')} "
          f"bucket={blocked[0]['bucket'] if blocked else '—'} "
          f"label={blocked[0]['label'] if blocked else '—'}")

    store.delete(cid)
    store.delete(cid2)
    print(f"\n  {'PASS' if ok and ok2 else 'FAIL'} — test campaigns deleted")
    return 0 if (ok and ok2) else 1


if __name__ == "__main__":
    import sys

    args = sys.argv[1:]
    if args and args[0] == "--selftest":
        raise SystemExit(_selftest())

    if args and args[0] == "--replay":
        # Feed a captured payload back in, e.g. from the raw feed on /webhooks:
        #   python3 wati_webhook.py --replay event.json
        with open(args[1], "r", encoding="utf-8") as f:
            payload = json.load(f)
        for item in (payload if isinstance(payload, list) else [payload]):
            print(json.dumps(handle(item), indent=2, default=str))
        raise SystemExit(0)

    info = health()
    print(f"token configured : {info['configured']}")
    print(f"event store      : {info['backend']}")
    print(f"events           : {info['total']} ({info['matched']} matched, {info['unmatched']} unmatched)")
    print(f"by status        : {info['by_status'] or '—'}")
    print(f"last event       : {info['last_at'] or 'never'} ({info['last_status'] or '—'})")
    if info["error"]:
        print(f"error            : {info['error']}")

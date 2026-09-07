#!/usr/bin/env python3
"""
campaign_runner.py

The scheduler and sender. One daemon thread wakes every POLL_SECONDS, asks
`campaign_store` what is due, and hands each due campaign to a worker thread
that walks its recipient list and calls WATI.

Why a thread and not cron: a campaign is not a single event, it is a long walk
through a recipient list that has to be pausable, resumable and rate-limited.
Keeping it in-process means the UI's Pause button and the sender are looking at
the same state, and the store is the single source of truth for both.

Three properties this is built around:

* **Resumable.** Every recipient carries its own status, and progress is
  persisted after each chunk. A restart mid-campaign re-picks it up at the
  cursor; already-sent recipients are skipped, so nobody is messaged twice.
* **Rate-limited.** A token bucket caps messages per minute (per campaign),
  because WATI throttles and because blasting a marketing template at full tilt
  is how a WhatsApp number gets its quality rating cut.
* **Interruptible.** Pause / cancel are checked between every chunk, so a
  campaign stops within a few seconds of the click rather than at the end.

The same tick also runs the retry sweep (`campaign_retry.py`): a campaign that
finished a week ago gets its failed leads collected into a fresh campaign,
which this scheduler then sends like any other. That is deliberately the whole
integration — a retry is a campaign, so it needs no sending path of its own.

Config via environment:
    WATI_SEND_WORKERS     parallel sends within one campaign (default 5)
    WATI_MAX_CONCURRENT   campaigns running at once           (default 2)
    WATI_POLL_SECONDS     scheduler tick                      (default 15)
    WATI_RETRY_SWEEP_SECONDS  how often to look for due retries (default 300)
"""

import datetime as dt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import campaign_retry as retry
import campaign_store as store
import wati_client

POLL_SECONDS = int(os.environ.get("WATI_POLL_SECONDS", "15"))
SEND_WORKERS = max(1, int(os.environ.get("WATI_SEND_WORKERS", "5")))
MAX_CONCURRENT = max(1, int(os.environ.get("WATI_MAX_CONCURRENT", "2")))
CHUNK_MAX = 50                  # recipients per persisted progress step
CHUNK_SECONDS = 20              # …but small enough that a chunk lasts ~this long
# The retry sweep answers a question about days, so asking it every 15 seconds
# alongside the send scheduler would be thousands of pointless queries a day.
RETRY_SWEEP_SECONDS = max(30, int(os.environ.get("WATI_RETRY_SWEEP_SECONDS", "300")))


def chunk_size(throttle):
    """
    Pause and cancel are checked between chunks, so the chunk has to be short
    in *time*, not just in count: at 30 messages/minute a 50-wide chunk would
    leave the user waiting 100 seconds after clicking Pause. Sizing the chunk to
    about CHUNK_SECONDS of work keeps the button responsive at any rate, while
    still batching enough to keep progress writes cheap.
    """
    return max(5, min(CHUNK_MAX, int(max(1, throttle) * CHUNK_SECONDS / 60)))

# Identifies this process in a campaign's log, so a claim is traceable when
# more than one app process shares the database.
WORKER_ID = f"{os.uname().nodename}:{os.getpid()}"

_started = False
_start_lock = threading.Lock()
_active = {}                    # campaign_id -> thread
_active_lock = threading.Lock()
_last_retry_sweep = 0.0


# --------------------------------------------------------------------------- #
# rate limiting
# --------------------------------------------------------------------------- #
class RateLimiter:
    """Token bucket: at most `per_minute` acquisitions in any rolling minute."""

    def __init__(self, per_minute):
        self.interval = 60.0 / max(1, per_minute)
        self.lock = threading.Lock()
        self.next_at = time.monotonic()

    def acquire(self):
        with self.lock:
            now = time.monotonic()
            wait = max(0.0, self.next_at - now)
            self.next_at = max(now, self.next_at) + self.interval
        if wait:
            time.sleep(wait)


# --------------------------------------------------------------------------- #
# scheduler
# --------------------------------------------------------------------------- #
def start():
    """Idempotent — app.py calls this on boot."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        threading.Thread(target=_scheduler_loop, name="wati-scheduler", daemon=True).start()


def _scheduler_loop():
    # A campaign left 'running' by a restart is not running any more; the
    # scheduler adopts it on the first tick and resumes from its cursor.
    try:
        for c in store.interrupted_campaigns():
            store.update(c["campaign_id"], {"status": "scheduled"},
                         log_msg="Adopted after an app restart — resuming from the last saved position")
    except Exception as e:                       # noqa: BLE001 — a bad tick must not kill the thread
        print(f"[wati-scheduler] startup scan failed: {e}")

    while True:
        try:
            _tick()
        except Exception as e:                   # noqa: BLE001
            print(f"[wati-scheduler] tick failed: {e}")
        time.sleep(POLL_SECONDS)


def _retry_sweep():
    """
    Hand due retries to campaign_retry, at most every RETRY_SWEEP_SECONDS.

    The sweep only *creates* campaigns; the rest of this tick then picks them
    up and sends them, so a retry that comes due starts moving within one poll
    interval of being built.
    """
    global _last_retry_sweep
    if not retry.enabled():
        return
    now = time.monotonic()
    if now - _last_retry_sweep < RETRY_SWEEP_SECONDS:
        return
    _last_retry_sweep = now
    retry.sweep(worker=WORKER_ID)               # never raises


def _tick():
    _retry_sweep()
    with _active_lock:
        for cid in [c for c, t in _active.items() if not t.is_alive()]:
            del _active[cid]
        free = MAX_CONCURRENT - len(_active)
        running = set(_active)
    if free <= 0:
        return
    for c in store.due_campaigns():
        if free <= 0:
            break
        cid = c["campaign_id"]
        if cid in running:
            continue
        if _launch(cid):
            free -= 1


def _launch(campaign_id):
    """
    Claim a campaign and give it a thread. False if someone else got there
    first — the claim is atomic in the store, so a second app process pointed at
    the same database cannot start a duplicate walk through the same list.
    """
    with _active_lock:
        if campaign_id in _active and _active[campaign_id].is_alive():
            return False
        if store.claim(campaign_id, WORKER_ID) is None:
            return False
        t = threading.Thread(target=_run_campaign, args=(campaign_id,),
                             name=f"wati-campaign-{campaign_id[:8]}", daemon=True)
        _active[campaign_id] = t
        t.start()
        return True


# --------------------------------------------------------------------------- #
# the send walk
# --------------------------------------------------------------------------- #
def _run_campaign(campaign_id):
    doc = store.get(campaign_id)
    if not doc:
        return
    recipients = doc.get("recipients") or []
    template = doc["template_name"]
    broadcast = doc.get("broadcast_name") or template
    dry_run = bool(doc.get("dry_run"))
    throttle = doc.get("throttle") or 60
    limiter = RateLimiter(throttle)
    chunk = chunk_size(throttle)

    sent = int(doc.get("sent") or 0)
    failed = int(doc.get("failed") or 0)
    cursor = int(doc.get("cursor") or 0)

    # A recipient already marked sent is never retried, even if the cursor is
    # behind it — the per-recipient status, not the cursor, is what makes a
    # resume safe.
    pending = [i for i in range(cursor, len(recipients))
               if (recipients[i].get("status") or "pending") == "pending"]

    stopped_as = None
    try:
        for start_i in range(0, len(pending), chunk):
            state = store.get(campaign_id) or {}
            if state.get("status") == "paused":
                stopped_as = "paused"
                break
            if state.get("status") == "cancelled":
                stopped_as = "cancelled"
                break

            batch = pending[start_i:start_i + chunk]
            results = {}

            def one(idx):
                r = recipients[idx]
                limiter.acquire()
                stamp = store.now_utc().isoformat()
                if dry_run:
                    return idx, {"status": "dry-run", "error": None, "at": stamp}
                try:
                    payload = wati_client.send_template(
                        phone=r["phone"],
                        template_name=template,
                        broadcast_name=broadcast,
                        params=r.get("params") or {},
                    )
                    # WATI sometimes echoes the WhatsApp message id here and
                    # sometimes does not. When it does, storing it makes every
                    # later delivery receipt an exact match instead of a guess
                    # by phone number; when it does not, the templateMessageSent
                    # webhook backfills it (wati_webhook.py).
                    wamid, _local = wati_client.message_ids(payload)
                    return idx, {"status": "sent", "error": None, "at": stamp,
                                 "wamid": wamid}
                except wati_client.WatiError as e:
                    return idx, {"status": "failed", "error": str(e)[:400], "at": stamp}
                except Exception as e:            # noqa: BLE001 — one bad number never stops the walk
                    return idx, {"status": "failed", "error": f"{type(e).__name__}: {e}"[:400], "at": stamp}

            with ThreadPoolExecutor(max_workers=SEND_WORKERS) as pool:
                for idx, res in pool.map(one, batch):
                    results[idx] = res
                    recipients[idx].update(res)
                    if res["status"] == "failed":
                        failed += 1
                    else:
                        sent += 1

            cursor = max(cursor, max(batch) + 1)
            store.record_results(campaign_id, results, cursor, sent, failed)

        if stopped_as == "paused":
            store.update(campaign_id, {}, log_msg=f"Paused at {sent + failed}/{len(recipients)}")
        elif stopped_as == "cancelled":
            store.update(campaign_id, {"finished_at": store.now_utc()},
                         log_msg=f"Cancelled at {sent + failed}/{len(recipients)}")
        else:
            store.update(campaign_id,
                         {"status": "completed", "finished_at": store.now_utc()},
                         log_msg=f"Finished — {sent} sent, {failed} failed")
            _arm_retry(campaign_id)
    except Exception as e:                        # noqa: BLE001
        store.update(campaign_id, {"status": "failed", "finished_at": store.now_utc()},
                     log_msg=f"Aborted: {type(e).__name__}: {e}")
        # An abort still sent real messages, and some of them still failed, so
        # its failed leads deserve the same second chance a clean finish gets.
        _arm_retry(campaign_id)
    finally:
        with _active_lock:
            _active.pop(campaign_id, None)


def _arm_retry(campaign_id):
    """
    Promise to look at this campaign's failures in a week.

    Note what is *not* done here: the failed leads are not collected. At this
    moment WATI has only told us which sends it refused; Meta's own refusals
    arrive down the webhook for hours afterwards, so a list built now would
    miss most of what a retry exists to catch. campaign_retry reads the list
    when the wait is up instead.
    """
    try:
        retry.arm(campaign_id)
    except Exception as e:                        # noqa: BLE001 — a finished send stays finished
        print(f"[wati-retry] could not arm {campaign_id[:8]}: {type(e).__name__}: {e}")


# --------------------------------------------------------------------------- #
# controls the UI calls
# --------------------------------------------------------------------------- #
def plan_batches(total, batch_size, gap_minutes, start_utc):
    """
    Work out the shape of a split send: [(index, size, when_utc), …], 1-based.

    Batch 1 goes at `start_utc` and each later one `gap_minutes` after the
    previous, so scheduling ten batches an hour apart is one decision rather
    than ten. Sizes are exact — the last batch carries the remainder.
    """
    batch_size = max(1, int(batch_size))
    gap = dt.timedelta(minutes=max(0, int(gap_minutes)))
    out = []
    index = 0
    for offset in range(0, total, batch_size):
        size = min(batch_size, total - offset)
        out.append((index + 1, size, start_utc + gap * index))
        index += 1
    return out


def cancel_group(group_id):
    """
    Cancel every batch of a split send that has not finished.

    Batches already sent are left alone — those messages are gone and saying
    otherwise would be a lie. A batch mid-flight stops at its current chunk,
    the same as cancelling it on its own.
    """
    docs = store.list_group(group_id)
    if not docs:
        return "No batches found for that group."
    stopped = 0
    for d in docs:
        if d.get("status") not in store.TERMINAL_STATUSES:
            store.update(d["campaign_id"],
                         {"status": "cancelled", "finished_at": store.now_utc()},
                         log_msg="Cancelled with the rest of its batch group")
            stopped += 1
    if not stopped:
        return "Every batch in that group has already finished."
    return None


def send_now(campaign_id):
    """Ignore the schedule and start immediately."""
    doc = store.get(campaign_id)
    if not doc:
        return "Campaign not found."
    if doc["status"] in store.TERMINAL_STATUSES:
        return f"Campaign is already {doc['status']}."
    store.update(campaign_id, {"scheduled_at": store.now_utc(), "status": "scheduled"},
                 log_msg="Send-now requested")
    _launch(campaign_id)
    return None


def pause(campaign_id):
    doc = store.get(campaign_id)
    if not doc or doc["status"] not in ("running", "scheduled"):
        return "Only a running or scheduled campaign can be paused."
    store.update(campaign_id, {"status": "paused"}, log_msg="Pause requested")
    return None


def resume(campaign_id):
    doc = store.get(campaign_id)
    if not doc or doc["status"] != "paused":
        return "Only a paused campaign can be resumed."
    store.update(campaign_id, {"status": "scheduled", "scheduled_at": store.now_utc()},
                 log_msg="Resumed")
    _launch(campaign_id)
    return None


def cancel(campaign_id):
    doc = store.get(campaign_id)
    if not doc:
        return "Campaign not found."
    if doc["status"] in store.TERMINAL_STATUSES:
        return f"Campaign is already {doc['status']}."
    store.update(campaign_id, {"status": "cancelled", "finished_at": store.now_utc()},
                 log_msg="Cancelled by user")
    return None


def retry_now(campaign_id):
    """
    Build this campaign's retry immediately instead of waiting out the week.

    Returns an error string, or None with the retry started. Forcing it early
    is allowed on any finished campaign — including one that was never armed,
    such as anything that finished before retries existed — because the wait is
    there to let delivery receipts land, not to forbid sending sooner.
    """
    doc = store.get(campaign_id)
    if not doc:
        return "Campaign not found."
    if doc.get("status") not in store.TERMINAL_STATUSES:
        return "Wait until the campaign has finished before retrying its failures."
    if doc.get("dry_run"):
        return "A dry run sent nothing, so there is nothing to retry."
    try:
        child, note = retry.create_retry(campaign_id, worker=WORKER_ID, manual=True)
    except Exception as e:                        # noqa: BLE001
        return f"Could not build the retry: {type(e).__name__}: {e}"
    if not child:
        return note or "Nothing to retry."
    _launch(child["campaign_id"])                 # rather than waiting for the next tick
    return None


def retry_off(campaign_id):
    """Cancel the pending automatic retry without touching what was sent."""
    return retry.disarm(campaign_id)


def reschedule(campaign_id, when_utc):
    doc = store.get(campaign_id)
    if not doc:
        return "Campaign not found."
    if doc["status"] in store.TERMINAL_STATUSES:
        return f"Campaign is already {doc['status']}."
    store.update(campaign_id, {"scheduled_at": when_utc, "status": "scheduled"},
                 log_msg=f"Rescheduled for {when_utc.astimezone(store.IST).strftime('%d %b %Y %H:%M IST')}")
    return None


def test_send(numbers, template_name, params_by_number=None, broadcast_name="test_send"):
    """
    Synchronous send to a handful of numbers, so the user can see the real
    message on a real phone before committing a whole list. Returns
    [{'phone', 'ok', 'error'}] and never raises.
    """
    out = []
    params_by_number = params_by_number or {}
    for phone in numbers[:5]:
        try:
            wati_client.send_template(
                phone=phone,
                template_name=template_name,
                broadcast_name=broadcast_name,
                params=params_by_number.get(phone) or {},
            )
            out.append({"phone": phone, "ok": True, "error": None})
        except Exception as e:                    # noqa: BLE001
            out.append({"phone": phone, "ok": False, "error": str(e)[:300]})
    return out


def is_running():
    return _started


def active_count():
    with _active_lock:
        return sum(1 for t in _active.values() if t.is_alive())


def parse_ist(value):
    """'2026-08-21T09:30' from a datetime-local input -> aware UTC datetime."""
    if not value:
        return None
    try:
        naive = dt.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if naive.tzinfo is None:
        naive = naive.replace(tzinfo=store.IST)
    return naive.astimezone(dt.timezone.utc)


if __name__ == "__main__":
    start()
    print(f"scheduler running · poll {POLL_SECONDS}s · {SEND_WORKERS} workers · "
          f"{MAX_CONCURRENT} concurrent campaigns")
    while True:
        time.sleep(60)

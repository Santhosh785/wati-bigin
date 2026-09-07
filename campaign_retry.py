#!/usr/bin/env python3
"""
campaign_retry.py

The second chance: a campaign's failed leads, sent again a week later.

A send has two ways of failing, and both of them land here:

* **Refused at send.** WATI answered the API call with an error, so the message
  never reached Meta at all. `campaign_runner` marks the recipient `failed` on
  the spot and the campaign's `failed` counter moves.
* **Refused after acceptance.** WATI said 200 OK, the runner recorded "sent",
  and Meta then declined to deliver it — a `failed` webhook arriving minutes or
  hours later with a reason code. `wati_webhook` writes `failed_at` and a
  `failed_bucket` onto the recipient and the campaign's `undelivered` counter
  moves.

The second kind is the reason this module waits rather than retrying at the
moment a campaign finishes: **at completion the failure list is not yet
known.** Delivery receipts trickle in for hours afterwards, so a retry list
snapshotted when the last message went out would miss most of the failures it
exists to catch. Waiting the full RETRY_AFTER_DAYS and *then* reading the
recipient list means every receipt has long since landed.

Waiting is also the point on Meta's side. The single largest bucket of refusals
on a marketing template is 131049 — "Meta held it back (healthy-ecosystem
limit)" — whose own guidance is that the same message often lands if it is
retried in a few days. A week is comfortably inside "often lands" and outside
"you are hammering a person who did not answer".

How a retry is actually sent: it is an ordinary campaign. `create_retry()`
builds a new document whose recipient list is the parent's retryable failures,
schedules it for now, and lets the existing scheduler pick it up. Nothing here
sends anything — which means retries pause, resume, rate-limit, log, report
delivery and appear on the dashboard exactly like any other campaign, for free.

Who is *not* retried matters as much as who is. Three buckets are excluded on
principle and never retried automatically:

    opted-out       a real opt-out made inside WhatsApp. Messaging again is a
                    policy violation, not an optimisation.
    undeliverable   the number cannot receive WhatsApp at all. A retry fails
                    identically, every time, and costs quality rating.
    template-error  the template, not the contact, is broken. Resending the
                    same broken template gets the same rejection.

Config via environment:
    WATI_RETRY_ENABLED      arm retries on completion    (default 1)
    WATI_RETRY_AFTER_DAYS   how long to wait             (default 7)
    WATI_RETRY_MAX_ROUNDS   retries of retries           (default 1)
    WATI_RETRY_THROTTLE     messages/min, else parent's  (default: parent's)
"""

import datetime as dt
import os

import campaign_store as store

try:
    import wati_webhook
except Exception:                                # noqa: BLE001 — the labels degrade, the logic does not
    wati_webhook = None

ENABLED = os.environ.get("WATI_RETRY_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
AFTER_DAYS = max(0, int(os.environ.get("WATI_RETRY_AFTER_DAYS", "7")))
MAX_ROUNDS = max(0, int(os.environ.get("WATI_RETRY_MAX_ROUNDS", "1")))
THROTTLE_OVERRIDE = os.environ.get("WATI_RETRY_THROTTLE")

# Only a campaign that ran to its end is armed. A cancelled one was stopped by
# a person, and quietly resurrecting a slice of it a week later is the opposite
# of what that click meant.
ARMABLE_STATUSES = ("completed", "failed")

# Buckets `wati_webhook.classify_failure` can produce, split by whether sending
# the same message again in a week is a sane thing to do. `send-error` is this
# module's own bucket for a WATI-side refusal, which carries no Meta code.
RETRYABLE_BUCKETS = {"meta-blocked", "window-closed", "account", "unknown", "send-error"}
NEVER_RETRY_BUCKETS = {
    "opted-out": "Opted out of marketing — messaging again is a policy violation",
    "undeliverable": "Not reachable on WhatsApp — a retry fails identically",
    "template-error": "The template is at fault, not the contact — fix it and send fresh",
}


def enabled():
    return ENABLED and AFTER_DAYS >= 0 and MAX_ROUNDS > 0


def delay():
    return dt.timedelta(days=AFTER_DAYS)


# --------------------------------------------------------------------------- #
# who gets a second message
# --------------------------------------------------------------------------- #
def classify_recipient(r):
    """
    ('retry' | 'skip', bucket, human reason) for one recipient.

    Returns bucket `''` for anybody who did not fail — the overwhelming
    majority — so callers can drop them before doing any more work.
    """
    delivered = r.get("delivered_at") or r.get("read_at") or r.get("replied_at")
    status = (r.get("status") or "pending").lower()

    if status == "failed":
        # WATI refused the API call, so Meta never saw it and there is no code
        # to classify. Almost always transient — a 5xx, a timeout, a rate
        # limit — which is exactly the case a retry is for.
        if delivered:
            return "skip", "send-error", "Marked failed but a receipt arrived — already delivered"
        return "retry", "send-error", "WATI refused the send"

    if not r.get("failed_at"):
        return "skip", "", ""

    # Meta refused it after WATI accepted it. `failed_bucket` is written by
    # wati_webhook.classify_failure from Meta's own reason code.
    bucket = r.get("failed_bucket") or "unknown"
    if delivered:
        # Both a failure and a receipt on one recipient means the webhook saw
        # contradictory events. Believe the receipt: a message that reached a
        # handset must not be sent twice.
        return "skip", bucket, "A delivery receipt arrived as well — not resending"
    if bucket in NEVER_RETRY_BUCKETS:
        return "skip", bucket, NEVER_RETRY_BUCKETS[bucket]
    if bucket in RETRYABLE_BUCKETS:
        label = (r.get("failed_label")
                 or (wati_webhook.BUCKET_LABEL.get(bucket, bucket) if wati_webhook else bucket))
        return "retry", bucket, label
    return "skip", bucket, f"Unhandled failure bucket “{bucket}”"


def plan_for(campaign):
    """
    What a retry of `campaign` would actually send.

    {'recipients': [{name, phone, params}], 'skipped': [{reason, count}],
     'failed_total': n, 'duplicates': n}

    Recipients are deduplicated by phone number: a list built from two Bigin
    filters can carry the same person twice, and one person collecting two
    copies of a retry is precisely the sort of thing a retry mechanism must not
    do.
    """
    recipients, skipped, seen = [], {}, set()
    failed_total = duplicates = 0

    for r in campaign.get("recipients") or []:
        verdict, bucket, reason = classify_recipient(r)
        if not bucket:
            continue
        failed_total += 1
        if verdict == "skip":
            skipped[reason] = skipped.get(reason, 0) + 1
            continue
        phone = str(r.get("phone") or "").strip()
        if not phone:
            skipped["No phone number on the record"] = skipped.get("No phone number on the record", 0) + 1
            continue
        if phone in seen:
            duplicates += 1
            continue
        seen.add(phone)
        recipients.append({"name": r.get("name") or "", "phone": phone,
                           "params": r.get("params") or {}})

    return {
        "recipients": recipients,
        "skipped": sorted(({"reason": k, "count": v} for k, v in skipped.items()),
                          key=lambda s: -s["count"]),
        "failed_total": failed_total,
        "duplicates": duplicates,
    }


# --------------------------------------------------------------------------- #
# arming — done when a campaign finishes
# --------------------------------------------------------------------------- #
def arm(campaign_id, campaign=None):
    """
    Mark a just-finished campaign as due for a retry in RETRY_AFTER_DAYS.

    Deliberately does not look at the failure list: at this moment it is
    incomplete, because Meta's refusals arrive down the webhook for hours
    afterwards. Arming is a promise to *look* later, and the sweep is free to
    find nothing.

    Returns the due datetime, or None if this campaign is not a candidate.
    """
    c = campaign or store.get(campaign_id)
    if not c or not enabled():
        return None
    if c.get("dry_run"):
        return None                              # nothing was sent, so nothing failed
    if c.get("status") not in ARMABLE_STATUSES:
        return None
    if c.get("retry_state") in ("armed", "building", "done", "off"):
        return None
    if int(c.get("retry_round") or 0) >= MAX_ROUNDS:
        store.update(campaign_id, {"retry_state": "capped"},
                     log_msg=f"No further retry — round limit of {MAX_ROUNDS} reached")
        return None

    due = store.now_utc() + delay()
    store.update(campaign_id, {"retry_state": "armed", "retry_due_at": due},
                 log_msg=f"Failed leads will be retried on "
                         f"{due.astimezone(store.IST).strftime('%d %b %Y, %H:%M IST')} "
                         f"({AFTER_DAYS} days) — the exact list is read then, once "
                         f"every delivery receipt has arrived")
    return due


def disarm(campaign_id):
    """Turn off the pending automatic retry. Returns an error string or None."""
    c = store.get(campaign_id)
    if not c:
        return "Campaign not found."
    if c.get("retry_state") == "done":
        return "The retry has already been created."
    if c.get("retry_state") == "building":
        return "The retry is being created right now."
    store.update(campaign_id, {"retry_state": "off", "retry_due_at": None},
                 log_msg="Automatic retry of failed leads turned off")
    return None


# --------------------------------------------------------------------------- #
# the sweep — done when the wait is up
# --------------------------------------------------------------------------- #
def create_retry(parent_id, worker="manual", manual=False):
    """
    Build and schedule the retry campaign for `parent_id`.

    Returns (child_doc, note). `child_doc` is None when nothing was created,
    and `note` then says why in a sentence fit to show a user.

    The claim is atomic in the store, so two app processes sweeping the same
    minute cannot both create a retry of the same campaign — the same guarantee,
    and the same reason for it, as claiming a campaign to send it.
    """
    allowed = ("armed", None, "none", "off", "capped") if manual else ("armed",)
    parent = store.claim_retry(parent_id, worker, allowed)
    if not parent:
        current = (store.get(parent_id) or {}).get("retry_state")
        if current == "done":
            return None, "A retry of this campaign has already been created."
        if current == "building":
            return None, "A retry of this campaign is being created right now."
        return None, "This campaign is not waiting for a retry."

    try:
        plan = plan_for(parent)
    except Exception as e:                       # noqa: BLE001 — never strand the claim
        store.update(parent_id, {"retry_state": "armed"},
                     log_msg=f"Retry planning failed, will try again: {type(e).__name__}: {e}")
        raise

    if not plan["recipients"]:
        note = ("Nothing to retry — no failures." if not plan["failed_total"]
                else f"Nothing to retry — all {plan['failed_total']:,} failures are in buckets "
                     f"that must not be resent.")
        store.update(parent_id, {"retry_state": "none", "retry_due_at": None,
                                 "retry_note": note}, log_msg=note)
        return None, note

    rnd = int(parent.get("retry_round") or 0) + 1
    base = parent.get("retry_base_name") or parent.get("name") or "Campaign"
    throttle = int(THROTTLE_OVERRIDE) if THROTTLE_OVERRIDE else int(parent.get("throttle") or 60)

    child = store.create(
        name=f"{base} — retry {rnd}",
        template_name=parent.get("template_name"),
        recipients=plan["recipients"],
        scheduled_at=store.now_utc(),
        throttle=throttle,
        source=parent.get("source"),
        dry_run=bool(parent.get("dry_run")),
        retry_of=parent_id,
        retry_round=rnd,
        retry_base_name=base,
    )

    skipped_total = sum(s["count"] for s in plan["skipped"]) + plan["duplicates"]
    note = (f"Retry {rnd} created with {len(plan['recipients']):,} of "
            f"{plan['failed_total']:,} failed leads"
            + (f" ({skipped_total:,} not retryable)" if skipped_total else "") + ".")
    store.update(parent_id,
                 {"retry_state": "done", "retry_due_at": None,
                  "retry_campaign_id": child["campaign_id"], "retry_note": note},
                 log_msg=note + f" Campaign {child['campaign_id'][:8]}.")
    store.update(child["campaign_id"], {},
                 log_msg=f"Retry {rnd} of “{esc_plain(parent.get('name'))}” "
                         f"({parent_id[:8]}) — these leads failed there "
                         f"{'' if manual else f'{AFTER_DAYS} days ago'}".rstrip())
    return child, None


def esc_plain(value):
    """Log lines are plain text; keep a stray quote from making them unreadable."""
    return str(value or "").replace("\n", " ")[:80]


def sweep(now=None, worker="sweeper"):
    """
    Create the retries whose wait is up. Called from the scheduler tick.

    Never raises: one unhappy campaign must not stop the others, and must not
    kill the scheduler thread that calls this.
    """
    if not enabled():
        return []
    made = []
    try:
        due = store.retry_due_campaigns(now or store.now_utc())
    except Exception as e:                       # noqa: BLE001
        print(f"[wati-retry] could not read due retries: {e}")
        return []
    for c in due:
        cid = c.get("campaign_id")
        try:
            child, note = create_retry(cid, worker=worker)
            if child:
                made.append(child)
                print(f"[wati-retry] {cid[:8]} -> {child['campaign_id'][:8]} "
                      f"({child['total']} leads)")
            elif note:
                print(f"[wati-retry] {cid[:8]}: {note}")
        except Exception as e:                   # noqa: BLE001
            print(f"[wati-retry] {cid[:8]} failed: {type(e).__name__}: {e}")
    return made


# --------------------------------------------------------------------------- #
# what the UI asks
# --------------------------------------------------------------------------- #
def summary(campaign):
    """
    Everything the detail page needs about this campaign's retry, in one walk
    of the recipient list.

    {'state', 'due_at', 'note', 'child_id', 'parent_id', 'round',
     'eligible', 'failed_total', 'skipped', 'can_retry_now', 'reason'}
    """
    state = campaign.get("retry_state") or ("off" if not enabled() else None)
    plan = plan_for(campaign)
    terminal = campaign.get("status") in store.TERMINAL_STATUSES
    return {
        "state": state,
        "due_at": campaign.get("retry_due_at"),
        "note": campaign.get("retry_note"),
        "child_id": campaign.get("retry_campaign_id"),
        "parent_id": campaign.get("retry_of"),
        "round": int(campaign.get("retry_round") or 0),
        "eligible": len(plan["recipients"]),
        "failed_total": plan["failed_total"],
        "duplicates": plan["duplicates"],
        "skipped": plan["skipped"],
        "after_days": AFTER_DAYS,
        # A retry can be forced early — the wait exists so the failure list is
        # complete, not because sending sooner is forbidden.
        "can_retry_now": bool(terminal and plan["recipients"]
                              and state not in ("done", "building")
                              and not campaign.get("dry_run")),
    }


def _selftest():
    """
    Prove the three things that can go quietly wrong: the bucket policy, the
    claim, and the shape of the campaign that gets built.

    Writes to the real store, like `wati_webhook.py --selftest`, because the
    claim is the point and an in-memory fake would not exercise it. Nothing
    live is ever sent: the campaign that spawns a retry is a dry run, so the
    retry it spawns is a dry run too, and both are deleted at the end.
    """
    ok = True

    # --- 1. the policy, which is pure and needs no store ------------------- #
    cases = [
        ({"status": "failed"}, "retry", "WATI refused the send"),
        ({"status": "sent", "failed_at": "x", "failed_bucket": "meta-blocked"}, "retry", "131049 pacing"),
        ({"status": "sent", "failed_at": "x", "failed_bucket": "opted-out"}, "skip", "opt-out"),
        ({"status": "sent", "failed_at": "x", "failed_bucket": "undeliverable"}, "skip", "not on WhatsApp"),
        ({"status": "sent", "failed_at": "x", "failed_bucket": "template-error"}, "skip", "template broken"),
        ({"status": "sent", "failed_at": "x", "failed_bucket": "meta-blocked",
          "delivered_at": "y"}, "skip", "receipt contradicts the failure"),
        ({"status": "sent", "delivered_at": "y"}, "skip", "never failed"),
    ]
    print("  policy")
    for r, want, why in cases:
        got, bucket, _reason = classify_recipient(r)
        hit = got == want
        ok = ok and hit
        print(f"    {'ok ' if hit else 'FAIL'} {why:<34} -> {got}{'' if bucket else ' (not a failure)'}")

    # --- 2. arming, on a live campaign parked as finished ------------------ #
    live = store.create("SELFTEST arm — delete me", "selftest_template",
                        [{"name": "T", "phone": "919900000001", "params": {}}])
    store.update(live["campaign_id"], {"status": "completed", "sent": 1,
                                       "finished_at": store.now_utc()})
    due = arm(live["campaign_id"])
    armed = store.get(live["campaign_id"])
    days = round((due - store.now_utc()).total_seconds() / 86400, 2) if due else None
    hit = bool(due) and armed.get("retry_state") == "armed" and abs((days or 0) - AFTER_DAYS) < 0.01
    ok = ok and hit
    print(f"\n  arm: {'ok ' if hit else 'FAIL'} state={armed.get('retry_state')} in {days} days")
    hit = arm(live["campaign_id"]) is None          # arming twice must not move the date
    ok = ok and hit
    print(f"    {'ok ' if hit else 'FAIL'} re-arming an armed campaign is a no-op")
    store.delete(live["campaign_id"])

    # --- 3. the sweep, on a dry run so nothing can be sent ----------------- #
    people = [{"name": f"T{i}", "phone": f"91990000{i:04d}", "params": {"1": f"T{i}"}}
              for i in range(6)]
    parent = store.create("SELFTEST retry — delete me", "selftest_template",
                          people, dry_run=True)
    pid = parent["campaign_id"]
    buckets = [None, "meta-blocked", "opted-out", "undeliverable", "template-error", "meta-blocked"]
    for i, bucket in enumerate(buckets):
        if bucket is None:                          # refused by WATI at send time
            store.update_recipient(pid, i, {"status": "failed", "error": "HTTP 502"})
        else:
            store.update_recipient(pid, i, {"status": "sent", "failed_at": "2026-01-01T00:00:00+00:00",
                                            "failed_bucket": bucket, "failed_code": "131049"})
    store.update(pid, {"status": "completed", "finished_at": store.now_utc(),
                       "retry_state": "armed", "retry_due_at": store.now_utc() - dt.timedelta(days=1)})

    made = sweep(worker="selftest")
    child = made[0] if made else None
    fresh = store.get(pid)
    # 3 retryable: the send-error, and the two meta-blocked.
    hit = bool(child) and child["total"] == 3 and fresh.get("retry_state") == "done"
    ok = ok and hit
    print(f"\n  sweep: {'ok ' if hit else 'FAIL'} {len(made)} campaign(s), "
          f"{child['total'] if child else 0} of 5 failures retried "
          f"(expected 3 — opt-out, dead number and template fault held back)")

    if child:
        hit = child.get("retry_of") == pid and child.get("retry_round") == 1 and child.get("dry_run")
        ok = ok and hit
        print(f"    {'ok ' if hit else 'FAIL'} child links back to its parent, round 1, dry run")
        hit = sorted(r["phone"] for r in child["recipients"]) == \
            ["919900000000", "919900000001", "919900000005"]
        ok = ok and hit
        print(f"    {'ok ' if hit else 'FAIL'} exactly the retryable numbers carried over")
        hit = child["recipients"][0].get("params") == {"1": "T0"}
        ok = ok and hit
        print(f"    {'ok ' if hit else 'FAIL'} template parameters carried over")

        # A second sweep must find nothing — this is the guard against two app
        # processes each building a retry of the same campaign.
        again = sweep(worker="selftest")
        hit = not again and create_retry(pid, manual=True)[0] is None
        ok = ok and hit
        print(f"    {'ok ' if hit else 'FAIL'} sweeping again creates nothing")

        store.delete(child["campaign_id"])
    store.delete(pid)

    # --- 4. the round cap, on a live campaign (a dry run declines earlier,
    #        for a different and equally correct reason) --------------------- #
    deep = store.create("SELFTEST cap — delete me", "selftest_template",
                        [{"name": "T", "phone": "919900000009", "params": {}}],
                        retry_of="0" * 32, retry_round=MAX_ROUNDS)
    store.update(deep["campaign_id"], {"status": "completed", "finished_at": store.now_utc()})
    hit = arm(deep["campaign_id"]) is None and \
        (store.get(deep["campaign_id"]) or {}).get("retry_state") == "capped"
    ok = ok and hit
    print(f"\n  cap: {'ok ' if hit else 'FAIL'} a round-{MAX_ROUNDS} campaign is not retried again")
    store.delete(deep["campaign_id"])

    dry = store.create("SELFTEST dry — delete me", "selftest_template",
                       [{"name": "T", "phone": "919900000010", "params": {}}], dry_run=True)
    store.update(dry["campaign_id"], {"status": "completed", "finished_at": store.now_utc()})
    hit = arm(dry["campaign_id"]) is None
    ok = ok and hit
    print(f"    {'ok ' if hit else 'FAIL'} a dry run is never armed — it sent nothing to fail")
    store.delete(dry["campaign_id"])

    print(f"\n  {'PASS' if ok else 'FAIL'} — test campaigns deleted")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    if "--selftest" in sys.argv:
        raise SystemExit(_selftest())

    if "--sweep" in sys.argv:
        made = sweep()
        print(f"{len(made)} retry campaign(s) created")
        raise SystemExit(0)

    print(f"retry: {'on' if enabled() else 'OFF'} · after {AFTER_DAYS} days · "
          f"max {MAX_ROUNDS} round(s)")
    for c in store.list_campaigns(limit=25, include_recipients=True):
        s = summary(c)
        if not s["failed_total"] and not s["state"]:
            continue
        due = s["due_at"].astimezone(store.IST).strftime("%d %b %H:%M") if s["due_at"] else "—"
        print(f"  {c['campaign_id'][:8]}  {str(s['state'] or '-'):<9} due {due:<13} "
              f"{s['eligible']:>5} retryable of {s['failed_total']:>5} failed   {c.get('name')}")

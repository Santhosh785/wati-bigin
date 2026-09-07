#!/usr/bin/env python3
"""
cron_jobs.py — the background work, as HTTP endpoints a scheduler can call.

On a server this work is a daemon thread (campaign_runner) and a systemd timer
(bigin_sync). Neither exists on a serverless host, so each becomes a URL that
Vercel Cron hits on a schedule. The jobs themselves are unchanged — every one
of them is a call into the same module the server uses.

    send    every minute — claim what is due and send for up to a budget of
            seconds, then hand back. campaign_runner.tick_once does the work.
    retry   hourly — collect failed leads from campaigns whose wait is up into
            fresh campaigns, which `send` then sends like any other.
    bigin   every 3 hours — mirror Bigin contacts into MongoDB, the same run
            deploy/bigin_sync_cron.sh does on a VM.

Authentication
--------------
These endpoints start real sends and cost real money, so they are not open.
Vercel Cron sends `Authorization: Bearer $CRON_SECRET`, and that is the check.
A `?key=` query parameter is accepted too, for an external scheduler that
cannot set headers — worth knowing that this puts the secret in URLs and so in
request logs, so prefer the header where you have the choice.

Without CRON_SECRET set the endpoints refuse to run at all. An open endpoint
here is an open "message everyone in the database" button.

Config via environment:
    CRON_SECRET           shared secret; required
    WATI_TICK_BUDGET      seconds one send tick may spend  (default 45)
"""

import json
import os
import time
import traceback
from http.server import BaseHTTPRequestHandler

# How long a send tick may run. Keep it comfortably under the function's
# maxDuration: a tick that is killed rather than allowed to return leaves a
# campaign claimed with nobody sending it, and it then waits out the claim
# lease before anyone picks it up.
TICK_BUDGET = int(os.environ.get("WATI_TICK_BUDGET", "45"))


class Unauthorized(Exception):
    pass


def _authorize(headers, query):
    secret = (os.environ.get("CRON_SECRET") or "").strip()
    if not secret:
        raise Unauthorized(
            "CRON_SECRET is not set. These endpoints start real sends, so they stay "
            "closed until it is. Set it in the project's environment variables and redeploy.")
    import hmac
    presented = ""
    auth = (headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        presented = auth[7:].strip()
    elif query.get("key"):
        presented = query["key"][0]
    if not presented or not hmac.compare_digest(presented, secret):
        raise Unauthorized("Bad or missing cron credentials.")


# --------------------------------------------------------------------------- #
# the jobs
# --------------------------------------------------------------------------- #
def job_send(query):
    import campaign_runner
    budget = int(query.get("budget", [TICK_BUDGET])[0])
    return campaign_runner.tick_once(budget_seconds=budget)


def job_retry(query):
    import campaign_runner
    return campaign_runner.retry_sweep_once()


def job_bigin(query):
    import bigin_sync
    full = (query.get("full", ["0"])[0] or "").lower() in ("1", "true", "yes")
    return bigin_sync.sync(full=full, dry_run=False, verbose=True)


JOBS = {"send": job_send, "retry": job_retry, "bigin": job_bigin}


def run(job, query):
    """Run one job, returning (status, payload). Never raises."""
    started = time.time()
    fn = JOBS.get(job)
    if fn is None:
        return 404, {"ok": False, "job": job, "error": "no such job"}
    try:
        result = fn(query or {})
        payload = {"ok": True, "job": job, "took": round(time.time() - started, 1),
                   "result": result}
        print(f"[cron:{job}] {json.dumps(payload, default=str)}")
        return 200, payload
    except Exception as e:                        # noqa: BLE001 — a failed job returns 500, it does not crash
        traceback.print_exc()
        return 500, {"ok": False, "job": job, "took": round(time.time() - started, 1),
                     "error": f"{type(e).__name__}: {e}"}


def make_handler(job):
    """
    Build the BaseHTTPRequestHandler Vercel expects, for one job.

    GET is what Vercel Cron sends; POST is allowed too so the same URL can be
    triggered by hand or by an external scheduler.
    """
    import urllib.parse

    class handler(BaseHTTPRequestHandler):
        server_version = "waticron/1.0"

        def _run(self):
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                _authorize(self.headers, query)
            except Unauthorized as e:
                return self._json(403, {"ok": False, "job": job, "error": str(e)})
            status, payload = run(job, query)
            return self._json(status, payload)

        def _json(self, status, payload):
            body = json.dumps(payload, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        do_GET = _run
        do_POST = _run

        def log_message(self, *args):
            """The secret can arrive in the query string; keep it out of the log."""

    return handler


if __name__ == "__main__":
    # Local check: python3 cron_jobs.py send
    import sys
    name = sys.argv[1] if len(sys.argv) > 1 else "send"
    code, out = run(name, {})
    print(code, json.dumps(out, indent=2, default=str))

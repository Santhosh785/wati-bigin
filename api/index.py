"""
api/index.py — the whole web app, as one Vercel function.

`vercel.json` rewrites every path that is not a file or another function here,
so this single handler serves the cleaner UI, the htmx fragments, the campaign
dashboard and the WATI delivery webhook, exactly as `python3 app.py` does.

Vercel's Python runtime looks for a module-level `handler` that subclasses
BaseHTTPRequestHandler and feeds each request to it — which is precisely what
app.Handler already is, so there is no adapter here and no second copy of the
routing table. What made that possible is sessions moving out of the process
(session_store.py); the request handling itself never needed changing.

Not served from here: the campaign scheduler. A function stops at the end of
its response, so the send walk is driven by api/cron/send.py instead.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import vercel_boot  # noqa: F401,E402  — must precede any app import

from app import Handler  # noqa: E402


class handler(Handler):
    """
    The name Vercel looks for, and it has to be a literal class statement.

    `handler = Handler` would be the obvious way to write this and it does not
    work: the platform decides whether a .py file under api/ is a function by
    reading the source for a `handler` class (or an `app`), not by importing
    it. An alias assignment is invisible to that, the file is treated as a
    static asset, and the deployment comes up serving Python source as text
    with no function behind it — which fails in exactly the way that looks like
    a routing problem.
    """

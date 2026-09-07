#!/usr/bin/env python3
"""
vercel_boot.py — the handful of adjustments a function needs that a server does not.

Imported first by every entrypoint under api/. Importing it is the whole API;
it has no functions to call.
"""

import os
import sys

# stdout is a pipe in a function, so Python block-buffers it and the log lines
# from a short invocation are lost when the instance freezes. Costs nothing at
# this volume and is the difference between a debuggable cron and a silent one.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

# The JSON fallbacks write next to the code, which is read-only here. Point
# them at the one writable directory a function has. It is per-instance and
# temporary — see campaign_store.DATA_DIR — so it is a cushion for a Mongo
# outage, not a backend, and MONGO_URI is not optional on this host.
if os.environ.get("VERCEL") and not os.environ.get("WATI_DATA_DIR"):
    os.environ["WATI_DATA_DIR"] = "/tmp/wati-data"

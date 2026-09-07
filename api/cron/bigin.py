"""api/cron/bigin.py — Vercel Cron entrypoint. The job itself lives in cron_jobs.py."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import vercel_boot  # noqa: F401,E402

from cron_jobs import make_handler  # noqa: E402


# A literal `class handler` statement, not `handler = make_handler(...)`:
# Vercel decides this file is a function by reading the source for one, and an
# alias assignment is invisible to it. See api/index.py.
class handler(make_handler("bigin")):
    pass

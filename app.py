#!/usr/bin/env python3
"""
app.py  —  htmx + Tailwind web UI for the Bigin -> WATI CSV cleaner.

Standard library only. Reuses the cleaning logic from wati_cleanup.py and
serves HTML fragments that htmx swaps into the page.

State is per-browser (cookie session), so many people can use it at once
without clobbering each other. Sessions live in `session_store` — MongoDB when
MONGO_URI is set (required on a serverless host, where no process outlives a
request), otherwise a dict in this process.

Config via environment:
    HOST   bind address   (default 127.0.0.1  — keep this behind a reverse proxy)
    PORT   bind port      (default 8000)

Run:
    python3 app.py
"""

import csv
import datetime as dt
import html
import io
import json
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from wati_cleanup import (
    WATI_HEADER,
    NAME_HINTS,
    LAST_NAME_HINTS,
    PHONE_HINTS,
    DEFAULT_COUNTRY_CODE,
    clean_phone,
    suggest_column,
)

# The Bigin mirror is optional: without pymongo (or without a reachable Mongo)
# the app still runs and falls back to CSV upload.
import session_store
import ui

try:
    import bigin_store
except ImportError:
    bigin_store = None

# The WATI sender is optional in the same way: if these fail to import, the
# cleaner still cleans and step 5 simply does not appear.
try:
    import campaign_runner
    import campaign_store
    import campaign_ui
    import wati_client
    import wati_webhook
except ImportError as _e:                       # pragma: no cover - defensive
    campaign_runner = campaign_store = campaign_ui = wati_client = wati_webhook = None
    print(f"WATI sending disabled: {_e}")

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8000"))

MAX_BODY = 50 * 1024 * 1024   # 50 MB upload cap
# Session lifetime and capacity now belong to session_store — it owns the
# storage, so it owns the eviction (a Mongo TTL index, or the dict sweep).

esc = html.escape

# --------------------------------------------------------------------------- #
# per-browser sessions
# --------------------------------------------------------------------------- #
# Sessions used to be a dict in this process, guarded by a lock. They live in
# `session_store` now, because on a serverless host the process that serves
# step 2 is not the one that served step 1 — see that module. With no
# MONGO_URI it still is a dict, so nothing changes when you run this file
# directly. The lock went with them: this module no longer shares mutable
# state between threads.


def new_session():
    return {
        "headers": [],
        "rows": [],
        "filters": [],     # list of {"col", "values": set, "mode"}
        "suppress": [],    # manually entered numbers to remove (cleaned, 10-digit)
        "total_in": 0,
        "source": None,    # "bigin" | "csv" — where the loaded rows came from
        "files": {},       # filename -> csv text (for download)
        "wati_rows": [],   # last generated recipient list, reused by step 5
        "last_seen": time.time(),
    }


# --------------------------------------------------------------------------- #
# core helpers
# --------------------------------------------------------------------------- #
def filtered_rows(sess):
    rows = sess["rows"]
    for f in sess["filters"]:
        col, vals, mode = f["col"], f["values"], f["mode"]
        keep = mode == "keep"
        rows = [r for r in rows if (((r.get(col) or "").strip() in vals) == keep)]
    return rows


def build_wati_rows(rows, name_cols, phone_col, country, suppress=None):
    """
    Like wati_cleanup.build_wati_rows, but also drops numbers in `suppress`
    (a manual do-not-contact list). Returns (good, skipped).
    """
    suppress = suppress or set()
    good, skipped, seen = [], [], set()
    for r in rows:
        name = " ".join((r.get(c) or "").strip() for c in name_cols).strip()
        phone = clean_phone(r.get(phone_col))
        if not phone:
            skipped.append({**r, "_skip_reason": "invalid/missing phone"})
            continue
        if phone in suppress:
            skipped.append({**r, "_skip_reason": "suppressed (manual)"})
            continue
        if phone in seen:
            skipped.append({**r, "_skip_reason": "duplicate phone"})
            continue
        seen.add(phone)
        good.append({
            "Name": name if name else "Contact",
            "CountryCode": country,
            "Phone": phone,
            "AllowCampaign": "True",
            "AllowSMS": "True",
            # A reference (not a copy) back to the Bigin row, so step 5 can fill
            # template parameters from any field. to_csv() writes only
            # WATI_HEADER columns, so this never reaches the CSV.
            "_src": r,
        })
    return good, skipped


def resolve_params(tpl_params, row, form):
    """
    Fill one recipient's template parameters from the mapping the user chose.

    A blank field falls back to the static box next to it, so a template that
    greets people by name does not go out with a hole in it when Bigin happens
    to have no name for that contact.
    """
    out = {}
    for p in tpl_params:
        src = (form.get(f"param_src_{p}") or ["__static__"])[0]
        static = (form.get(f"param_val_{p}") or [""])[0].strip()
        if src == "__name__":
            value = (row.get("Name") or "").strip()
        elif src == "__phone__":
            value = (row.get("Phone") or "").strip()
        elif src.startswith("col:"):
            value = str((row.get("_src") or {}).get(src[4:]) or "").strip()
        else:
            value = static
        out[p] = value or static
    return out


def build_recipients(sess, tpl, form):
    """The cleaned rows turned into what campaign_store.create() wants."""
    tpl_params = tpl["params"] if tpl else []
    recipients = []
    for row in sess.get("wati_rows") or []:
        phone = wati_client.normalize_number(row.get("CountryCode") or DEFAULT_COUNTRY_CODE,
                                             row.get("Phone") or "")
        if not phone:
            continue
        recipients.append({
            "name": row.get("Name") or "",
            "phone": phone,
            "params": resolve_params(tpl_params, row, form),
        })
    return recipients


def parse_multipart(body, boundary):
    """Minimal multipart/form-data parser. Returns {name: bytes-or-str}."""
    result = {}
    delim = b"--" + boundary
    for part in body.split(delim):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        head, data = part.split(b"\r\n\r\n", 1)
        head_txt = head.decode("utf-8", "replace")
        m = re.search(r'name="([^"]*)"', head_txt)
        if not m:
            continue
        name = m.group(1)
        is_file = "filename=" in head_txt
        result[name] = data if is_file else data.decode("utf-8", "replace").strip()
    return result


def load_from_bytes(sess, raw):
    text = raw.decode("utf-8-sig", "replace")
    reader = csv.DictReader(io.StringIO(text))
    headers = [h for h in (reader.fieldnames or []) if h is not None]
    rows = [dict(r) for r in reader]
    sess.update(headers=headers, rows=rows, filters=[], suppress=[],
                total_in=len(rows), files={}, wati_rows=[], source="csv")


def load_from_store(sess):
    """Fill the session from the MongoDB Bigin mirror. Returns a status string."""
    if bigin_store is None:
        raise RuntimeError("bigin_store.py is missing — CSV upload is the only source.")
    headers, rows = bigin_store.load_contacts()
    sess.update(headers=headers, rows=rows, filters=[], suppress=[],
                total_in=len(rows), files={}, wati_rows=[], source="bigin")
    return f"{len(rows)} contacts loaded from Bigin"


def to_csv(fields, records):
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    w.writerows(records)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# HTML fragments
# --------------------------------------------------------------------------- #
def options(headers, selected=None, none_label=None):
    out = []
    if none_label is not None:
        out.append(f'<option value="">{esc(none_label)}</option>')
    for h in headers:
        sel = " selected" if h == selected else ""
        out.append(f'<option value="{esc(h)}"{sel}>{esc(h)}</option>')
    return "".join(out)


def workspace_fragment(sess):
    """Steps 2-4, rendered from current session. Swapped into #workspace."""
    headers = sess["headers"]
    if not headers:
        return ""
    name_s = suggest_column(headers, NAME_HINTS)
    last_s = suggest_column(headers, LAST_NAME_HINTS)
    phone_s = suggest_column(headers, PHONE_HINTS)

    filter_body = f"""
      <div class="flex flex-wrap items-end gap-3">
        <div class="min-w-[220px] max-w-sm flex-1">
          <label class="{ui.LABEL}">Field</label>
          <select name="col" class="{ui.FIELD}"
                  hx-get="/filter-values" hx-target="#value-picker" hx-swap="innerHTML" hx-trigger="change">
            {options(headers)}
          </select>
        </div>
        <button class="{ui.BTN_GHOST}" hx-get="/filter-values" hx-include="previous select"
                hx-target="#value-picker" hx-swap="innerHTML">Load values</button>
      </div>
      <div id="value-picker" class="mt-4"></div>
      <div id="active-filters" class="mt-5">{active_filters_fragment(sess)}</div>
    """

    map_body = f"""
    <form hx-post="/generate" hx-target="#result" hx-swap="innerHTML" class="space-y-5">
      <div class="grid gap-4 sm:grid-cols-2 xl:grid-cols-5">
        <div>
          <label class="{ui.LABEL}">Name column</label>
          <select name="name_col" class="{ui.FIELD}">{options(headers, name_s)}</select>
        </div>
        <div>
          <label class="{ui.LABEL}">Append (last name)</label>
          <select name="name_col2" class="{ui.FIELD}">{options(headers, last_s, none_label="— none —")}</select>
        </div>
        <div>
          <label class="{ui.LABEL}">Phone column</label>
          <select name="phone_col" class="{ui.FIELD}">{options(headers, phone_s)}</select>
        </div>
        <div>
          <label class="{ui.LABEL}">Country code</label>
          <input name="country" value="{DEFAULT_COUNTRY_CODE}" class="{ui.FIELD_MONO}">
        </div>
        <div>
          <label class="{ui.LABEL}">Batch size</label>
          <select name="size" class="{ui.FIELD}">
            <option value="100">100</option>
            <option value="250" selected>250</option>
            <option value="500">500</option>
          </select>
        </div>
      </div>

      <div class="{ui.WELL}">
        <label class="{ui.LABEL}">Do-not-contact — type a number and add</label>
        <div class="flex flex-wrap gap-2" hx-on:htmx:after-request="this.querySelector('input[name=number]').value=''">
          <input name="number" placeholder="9876543210 or +91 98765 43210"
                 class="{ui.FIELD_MONO} min-w-[220px] flex-1"
                 hx-post="/suppress-add" hx-include="this" hx-target="#suppress-chips" hx-swap="innerHTML"
                 hx-trigger="keyup[key=='Enter']">
          <button type="button" class="{ui.BTN_GHOST}" hx-post="/suppress-add" hx-include="previous input"
                  hx-target="#suppress-chips" hx-swap="innerHTML">Add</button>
        </div>
        <div id="suppress-chips" class="mt-3">{suppress_chips_html(sess)}</div>
      </div>

      <div class="flex flex-wrap items-center gap-4">
        <button class="{ui.BTN_GO}">Generate WATI files</button>
        <p class="text-xs text-slate-500">
          Name · CountryCode · Phone · AllowCampaign · AllowSMS
        </p>
      </div>
    </form>

    <div id="result"></div>
    """

    return (ui.section("02", "Filter contacts",
                       meta=ui.tag(f"{len(filtered_rows(sess)):,} matching", "ink"),
                       body=filter_body)
            + ui.section("03", "Map columns &amp; generate",
                         meta='<span class="text-xs text-slate-500">Phones are cleaned to 10-digit Indian '
                              'numbers and de-duplicated.</span>',
                         body=map_body))


def suppress_chips_html(sess, error=None):
    """The manually-added numbers, as removable chips."""
    err = f'<p class="mb-2 text-xs text-rose-400">{esc(error)}</p>' if error else ""
    items = sess["suppress"]
    if not items:
        return err + '<p class="text-xs text-slate-500">No numbers added yet.</p>'
    chips = []
    for i, n in enumerate(items):
        chips.append(
            f'<span class="inline-flex items-center gap-2 border border-slate-700 bg-slate-900 px-2.5 py-1 '
            f'font-mono text-xs text-slate-100">{esc(n)}'
            f'<button type="button" class="text-slate-500 transition-colors hover:text-rose-400" title="remove"'
            f' hx-post="/suppress-remove?i={i}" hx-target="#suppress-chips" hx-swap="innerHTML">&times;</button>'
            f'</span>')
    return err + '<div class="flex flex-wrap gap-2">' + "".join(chips) + "</div>"


def filter_values_fragment(sess, col):
    rows = filtered_rows(sess)
    counts = {}
    for r in rows:
        v = (r.get(col) or "").strip()
        counts[v] = counts.get(v, 0) + 1
    distinct = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))

    boxes = []
    for v, c in distinct[:500]:
        shown = esc(v) if v != "" else '<span class="text-slate-500">(blank)</span>'
        boxes.append(f"""
          <label class="flex cursor-pointer items-center gap-2.5 border-b border-dashed border-slate-700 px-2 py-1.5
                        transition-colors last:border-0 hover:bg-slate-700/50">
            <input type="checkbox" name="values" value="{esc(v)}">
            <span class="min-w-0 flex-1 truncate text-sm">{shown}</span>
            <span class="font-mono text-xs text-slate-500">{c:,}</span>
          </label>""")

    more = "" if len(distinct) <= 500 else (
        f'<p class="border-t border-slate-700 p-2 text-xs text-amber-400">Showing the first 500 of {len(distinct):,} values.</p>')

    return f"""
    <form hx-post="/add-filter" hx-target="#workspace" hx-swap="innerHTML" class="{ui.PANEL}">
      <input type="hidden" name="col" value="{esc(col)}">
      <div class="mb-3 flex flex-wrap items-center gap-3">
        <select name="mode" class="{ui.FIELD} w-auto">
          <option value="keep">Keep matching rows</option>
          <option value="remove">Remove matching rows</option>
        </select>
        <span class="text-xs text-slate-500">
          {esc(col)} · {len(distinct):,} distinct
        </span>
      </div>
      <div class="max-h-60 overflow-auto border border-slate-700 bg-slate-900">
        {''.join(boxes)}{more}
      </div>
      <button class="{ui.BTN} mt-4">Apply filter</button>
    </form>
    """


def active_filters_fragment(sess):
    if not sess["filters"]:
        return '<p class="text-xs text-slate-500">No filters applied — the whole list is in play.</p>'
    rows = []
    for i, f in enumerate(sess["filters"]):
        keep = f["mode"] == "keep"
        rows.append(f"""
          <div class="flex items-center gap-3 border-b border-dashed border-slate-700 py-2 last:border-0">
            <span class="min-w-0 flex-1 text-sm">
              {ui.tag("Keep" if keep else "Remove", "go" if keep else "signal")}
              <span class="ml-2 text-slate-300">{esc(f['col'])}</span>
              <span class="text-slate-500">is</span>
              <span class="font-mono text-xs">{esc(", ".join((v if v != "" else "(blank)") for v in f["values"]))}</span>
            </span>
            <button class="{ui.BTN_XS}" hx-post="/remove-filter?i={i}"
                    hx-target="#workspace" hx-swap="innerHTML">remove</button>
          </div>""")
    return "".join(rows)


def result_fragment(sess, name_cols, phone_col, country, size, suppress=None):
    suppress = suppress or set()
    good, skipped = build_wati_rows(filtered_rows(sess), name_cols, phone_col, country, suppress)
    n_suppressed = sum(1 for s in skipped if s.get("_skip_reason") == "suppressed (manual)")

    # Step 5 sends exactly this list — not a re-derived one — so what the user
    # confirms is what goes out.
    sess["wati_rows"] = good

    sess["files"] = {}
    batches = []
    for i in range(0, len(good), size):
        chunk = good[i:i + size]
        fname = f"wati_batch_{i // size + 1}.csv"
        sess["files"][fname] = to_csv(WATI_HEADER, chunk)
        batches.append((fname, len(chunk)))
    if skipped:
        sess["files"]["skipped.csv"] = to_csv(list(skipped[0].keys()), skipped)

    links = []
    for fname, n in batches:
        links.append(
            f'<a href="/download/{fname}" download class="flex items-center justify-between gap-4 border-b '
            f'border-dashed border-slate-700 py-1.5 transition-colors last:border-0 hover:text-rose-400">'
            f'<span class="font-mono text-xs">{fname}</span>'
            f'<span class="font-mono text-xs text-slate-500">{n:,}</span></a>')
    if skipped:
        links.append(
            f'<a href="/download/skipped.csv" download class="flex items-center justify-between gap-4 border-b '
            f'border-dashed border-slate-700 py-1.5 transition-colors last:border-0 hover:text-rose-400">'
            f'<span class="font-mono text-xs text-amber-400">skipped.csv</span>'
            f'<span class="font-mono text-xs text-amber-400">{len(skipped):,}</span></a>')
    if not batches:
        links.append('<p class="text-xs text-slate-500">No valid contacts to write.</p>')

    head = "".join(
        f'<th class="border-b border-slate-600/20 px-3 py-2 text-left font-mono text-xs font-medium '
        f' text-slate-500">{h}</th>' for h in WATI_HEADER)
    body = ""
    for r in good[:50]:
        body += ('<tr class="border-b border-dashed border-slate-700 last:border-0">' + "".join(
            f'<td class="px-3 py-1.5 font-mono text-xs">{esc(str(r[h]))}</td>' for h in WATI_HEADER) + "</tr>")
    if not good:
        body = ('<tr><td colspan="5" class="px-3 py-4 text-center text-xs text-slate-500">'
                'nothing to preview</td></tr>')

    result_body = f"""
      <div class="grid gap-8 lg:grid-cols-[minmax(0,320px)_minmax(0,240px)_minmax(0,1fr)]">
        <div>
          <p class="{ui.LABEL}">Tally</p>
          {ui.stat("Loaded from " + ("Bigin" if sess.get("source") == "bigin" else "CSV"), f"{sess['total_in']:,}")}
          {ui.stat("After filtering", f"{len(filtered_rows(sess)):,}")}
          {ui.stat("Suppressed", f"{n_suppressed:,}", "text-amber-400")}
          {ui.stat("Skipped total", f"{len(skipped):,}", "text-amber-400")}
          {ui.stat("Valid contacts", f"{len(good):,}", "text-emerald-400")}
          {ui.stat("Batch files", f"{len(batches):,}")}
        </div>
        <div>
          <p class="{ui.LABEL}">Files</p>
          {''.join(links)}
        </div>
        <div class="min-w-0">
          <p class="{ui.LABEL}">Preview — first 50</p>
          <div class="max-h-[22rem] overflow-auto border border-slate-700 bg-slate-900">
            <table class="w-full"><thead class="sticky top-0 bg-slate-900/60"><tr>{head}</tr></thead>
            <tbody>{body}</tbody></table>
          </div>
        </div>
      </div>
    """

    return (ui.section("04", "Download", meta=ui.tag(f"{len(good):,} ready", "go"),
                       body=result_body, tone="emerald")
            + (campaign_ui.send_fragment(sess) if campaign_ui else ""))


def source_fragment(sess=None, error=None, notice=None):
    """STEP 1 — pick a source. Bigin mirror first, CSV upload as a fallback."""
    if bigin_store is None:
        status = {"available": False, "count": 0, "lastSyncAt": None,
                  "error": "bigin_store.py not found."}
    else:
        status = bigin_store.sync_status()

    if status["available"]:
        age = bigin_store.humanize_age(status["lastSyncAt"]) if status["lastSyncAt"] else "never"
        meta = (ui.tag(f"{status['count']:,} mirrored", "go") +
                ui.tag(f"synced {esc(age)}", "quiet"))
        action = (f'<button class="{ui.BTN}" hx-post="/load-bigin" hx-target="#workspace" '
                  f'hx-swap="innerHTML" hx-indicator="#bigin-spin">Load contacts from Bigin</button>')
        hint = ui.note("Mirrored from Bigin by the cron sync every three hours — no export needed.")
    else:
        meta = ui.tag("unavailable", "signal")
        action = (f'<button disabled class="{ui.BTN} cursor-not-allowed border-slate-700 bg-slate-600 '
                  f'text-slate-500 hover:bg-slate-600">Load contacts from Bigin</button>')
        hint = ui.note(esc(status.get("error") or "Mongo unreachable."), "signal")

    msg = ""
    if error:
        msg = '<div class="mt-4">' + ui.banner(esc(error), "signal") + "</div>"
    elif notice:
        msg = '<div class="mt-4">' + ui.banner(esc(notice), "go") + "</div>"

    body = f"""
      <div class="flex flex-wrap items-center gap-3">
        {action}
        <span id="bigin-spin" class="htmx-indicator text-xs text-slate-500">
          loading…
        </span>
        <button class="{ui.BTN_GHOST}" hx-get="/source" hx-target="#source" hx-swap="innerHTML"
                title="Re-check the mirror">Refresh</button>
      </div>
      {hint}
      {msg}

      <details class="mt-6 border-t border-dashed border-slate-700 pt-4">
        <summary class="cursor-pointer text-xs text-slate-500
                        transition-colors hover:text-slate-100">Or upload a CSV instead</summary>
        <form hx-post="/upload" hx-target="#workspace" hx-swap="innerHTML" hx-encoding="multipart/form-data"
              class="mt-4 flex flex-wrap items-center gap-3">
          <input type="file" name="file" accept=".csv" required
                 class="max-w-full text-xs text-slate-300 file:mr-3 file:border file:border-slate-600 file:bg-slate-700
                        file:px-3 file:py-1.5 file:font-mono file:text-xs file:uppercase file:text-white hover:file:bg-slate-600">
          <button class="{ui.BTN_GHOST}">Load CSV</button>
          <span class="htmx-indicator text-xs text-slate-500">loading…</span>
        </form>
        {ui.note("For a one-off list from somewhere other than Bigin.")}
      </details>
    """
    return ui.section("01", "Load contacts", meta=meta, body=body)


def wati_status_line():
    """
    The connection line under the masthead title: endpoint, template count, and
    whether anything is sending right now.

    Reads through campaign_ui's five-minute template cache rather than calling
    wati_client.status(), so rendering a page costs no WATI round-trip once the
    first one has warmed the cache.
    """
    def line(text, tone="text-slate-400"):
        return f'<p class="mt-3 text-xs {tone}">{text}</p>'

    if campaign_ui is None:
        return line("WATI modules failed to load — sending is disabled, the cleaner still works.",
                    "text-amber-400")

    items, err = campaign_ui.templates()
    if err and not items:
        return line(f"WATI not reachable — {esc(err)}", "text-rose-400")
    try:
        endpoint = wati_client.config()[0]
    except wati_client.WatiUnavailable as e:
        return line(f"WATI not configured — {esc(e)}", "text-rose-400")

    active = campaign_runner.active_count() if campaign_runner else 0
    busy = (f' · <span class="text-emerald-300">{active} campaign'
            f'{"s" if active != 1 else ""} sending now</span>') if active else ""
    stale = ' · <span class="text-amber-400">template list may be stale</span>' if err else ""
    return line(f'WATI connected · <span class="font-mono">{esc(endpoint)}</span> · '
                f'{len(items)} approved templates{busy}{stale}')


def cleaner_page():
    nav = (ui.nav_link("Build a list", current=True) + ui.nav_link("Campaigns", "/campaigns")
           + ui.nav_link("Delivery webhook", "/webhooks"))
    body = f"""
    {ui.masthead("Bigin &rarr; WATI", "Clean a list, then send or schedule it on WhatsApp — batch by batch.",
                 nav, wati_status_line())}
    <main class="mx-auto w-full max-w-[1400px] space-y-9 px-6 py-9">
      <div id="source">{source_fragment()}</div>
      <div id="workspace" class="space-y-9"></div>
    </main>
    """
    return ui.page("Bigin → WATI campaign desk", "", body)


def webhooks_page(body_html):
    nav = (ui.nav_link("Build a list", "/") + ui.nav_link("Campaigns", "/campaigns")
           + ui.nav_link("Delivery webhook", current=True))
    body = f"""
    {ui.masthead("Delivery webhook",
                 "What happened after the send — delivered, read, replied, refused by Meta.",
                 nav, wati_status_line())}
    <main class="mx-auto w-full max-w-[1400px] px-6 py-9">{body_html}</main>
    """
    return ui.page("WATI delivery webhook", "", body)


def campaigns_page(body_html):
    nav = (ui.nav_link("Build a list", "/") + ui.nav_link("Campaigns", current=True)
           + ui.nav_link("Delivery webhook", "/webhooks"))
    body = f"""
    {ui.masthead("Campaigns", "Everything sent or scheduled from here, with live progress.",
                 nav, wati_status_line())}
    <main class="mx-auto w-full max-w-[1400px] px-6 py-9">{body_html}</main>
    """
    return ui.page("WATI campaigns", "", body)


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "watihttpd/1.0"

    def get_session(self):
        """
        Return this browser's session, creating one (and queuing a Set-Cookie)
        if needed.

        Loaded at most once per request and cached on the handler: routes call
        this freely, and every call has to hand back the *same* dict or a
        mutation made by one call would be invisible to the next. `_persist`
        writes it back when the route is done.
        """
        if getattr(self, "_sess", None) is not None:
            return self._sess
        sid = None
        for part in self.headers.get("Cookie", "").split(";"):
            part = part.strip()
            if part.startswith("sid="):
                sid = part[4:]
        sess = session_store.load(sid) if sid else None
        if sess is None:
            sid = secrets.token_hex(16)
            sess = new_session()
            self._new_sid = sid
        else:
            self._new_sid = None
        sess["last_seen"] = time.time()
        self._sid, self._sess = sid, sess
        return sess

    def _persist(self):
        """
        Write the session back, once, on the way out of a request.

        Errors are logged rather than raised: this runs while a response is
        being assembled, and failing to store the session is not a reason to
        replace a working page with a 500. It is worth shouting about in the
        log though — the next request will silently see stale state.
        """
        sid, sess = getattr(self, "_sid", None), getattr(self, "_sess", None)
        if not sid or sess is None:
            return
        try:
            session_store.save(sid, sess)
        except session_store.SessionTooLarge as e:
            print(f"[sessions] {sid[:8]} NOT SAVED: {e}")
        except Exception as e:                    # noqa: BLE001
            print(f"[sessions] {sid[:8]} NOT SAVED: {type(e).__name__}: {e}")
        finally:
            self._sid = self._sess = None

    def _send(self, body, ctype="text/html; charset=utf-8", status=200, extra=None):
        # Before the response, never after. A serverless host may freeze the
        # instance the moment the last byte goes out, so a write queued after
        # this point is a write that sometimes does not happen — and the
        # symptom is a wizard that loses a step under load and nowhere else.
        self._persist()
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if getattr(self, "_new_sid", None):
            self.send_header("Set-Cookie", f"sid={self._new_sid}; Path=/; HttpOnly; SameSite=Lax")
            self._new_sid = None
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        if length > MAX_BODY:
            return None
        return self.rfile.read(length) if length else b""

    def _json(self, payload, status=200):
        return self._send(json.dumps(payload, default=str), ctype="application/json", status=status)

    def _public_base(self):
        """
        The externally visible origin, for printing the webhook URL to paste
        into WATI.

        This process listens on 127.0.0.1 behind a reverse proxy, so its own
        host and port are not the address WATI will call. The proxy's
        `X-Forwarded-*` headers are the only thing that knows the real one —
        and if they are absent (someone hitting the app directly) the page says
        so rather than printing a localhost URL that could never work.
        """
        env_base = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        if env_base:
            return env_base
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
        proto = (self.headers.get("X-Forwarded-Proto") or "https").split(",")[0].strip()
        if not host or host.startswith(("127.0.0.1", "localhost")):
            return ""
        return f"{proto}://{host}"

    # `_send` persists the session on the way out, which covers every route.
    # These two are the backstop for a route that returns without responding,
    # and for one that raises: the work already done should still be kept.
    def do_GET(self):
        try:
            return self._route_get()
        finally:
            self._persist()

    def do_POST(self):
        try:
            return self._route_post()
        finally:
            self._persist()

    def _route_get(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        if path == "/":
            self.get_session()  # ensures a cookie is set on first visit
            return self._send(cleaner_page())

        if path == "/health":
            return self._send("ok", ctype="text/plain")

        # The webhook answers before the session cookie logic below: WATI is
        # not a browser, and handing it a Set-Cookie on every one of thousands
        # of delivery receipts would be noise at best.
        if path == "/webhooks/wati":
            # WATI's dashboard pings the URL with a GET when you save it, and
            # some setups will not accept a webhook that 404s. Answering here
            # also gives the operator a one-click way to check the token.
            supplied = (qs.get("token") or [""])[0] or self.headers.get("X-Webhook-Token", "")
            if wati_webhook is None:
                return self._json({"ok": False, "error": "webhook module not loaded"}, 503)
            if not wati_webhook.token():
                return self._json({"ok": False, "error": "WATI_WEBHOOK_TOKEN is not set on the server"}, 503)
            if not wati_webhook.check_token(supplied):
                print(f"[wati/webhook] verify rejected: bad token (length={len(str(supplied))}, "
                      f"expected {len(wati_webhook.token())})")
                return self._json({"ok": False, "error": "invalid or missing token"}, 401)
            print("[wati/webhook] verify ok — endpoint reachable and token accepted")
            return self._json({"ok": True, "message": "WATI webhook endpoint is live. POST events here."})

        sess = self.get_session()

        if path == "/source":
            return self._send(source_fragment(sess))

        # ------------------------------------------------------------------ #
        # WATI: composer fragments
        # ------------------------------------------------------------------ #
        if path.startswith("/wati/"):
            if campaign_ui is None:
                return self._send('<p class="text-sm text-rose-400">WATI sending is disabled on this install.</p>')

            if path == "/wati/compose":
                return self._send(campaign_ui.send_fragment(sess))

            if path == "/wati/refresh":
                campaign_ui.templates(force=True)
                return self._send(campaign_ui.send_fragment(sess))

            if path == "/wati/template":
                name = (qs.get("template") or [""])[0]
                return self._send(campaign_ui.template_detail_fragment(sess, name))

            if path == "/wati/when":
                return self._send(campaign_ui.when_fragment((qs.get("when") or ["now"])[0]))

            if path == "/wati/batch-preview":
                return self._send(self._batch_preview(sess, qs))

        # ------------------------------------------------------------------ #
        # WATI: campaigns dashboard
        # ------------------------------------------------------------------ #
        if path == "/campaigns":
            if campaign_ui is None:
                return self._send("WATI sending is disabled.", status=503, ctype="text/plain")
            return self._send(campaigns_page(campaign_ui.campaigns_table()))

        if path == "/campaigns/list":
            if campaign_ui is None:
                return self._send("")
            return self._send(campaign_ui.campaigns_table())

        if path == "/campaigns/detail":
            if campaign_ui is None:
                return self._send("WATI sending is disabled.", status=503, ctype="text/plain")
            cid = (qs.get("id") or [""])[0]
            return self._send(campaigns_page(campaign_ui.campaign_detail(cid)))

        if path == "/campaigns/export":
            if campaign_store is None:
                return self._send("Not found", status=404, ctype="text/plain")
            cid = (qs.get("id") or [""])[0]
            doc = campaign_store.get(cid)
            if not doc:
                return self._send("Not found", status=404, ctype="text/plain")
            # Everything from "Delivered" rightwards comes from the webhook,
            # so those columns are blank until receipts arrive — which is
            # itself worth seeing in the export.
            columns = ["Phone", "Name", "Status", "At", "Error",
                       "Delivered", "Read", "Replied", "Reply", "Blocked reason", "Meta code"]
            records = [{"Phone": r["phone"], "Name": r.get("name") or "",
                        "Status": r.get("status") or "", "At": r.get("at") or "",
                        "Error": r.get("error") or "",
                        "Delivered": r.get("delivered_at") or "",
                        "Read": r.get("read_at") or "",
                        "Replied": r.get("last_reply_at") or r.get("replied_at") or "",
                        "Reply": r.get("reply_text") or "",
                        "Blocked reason": r.get("failed_label") or "",
                        "Meta code": r.get("failed_code") or ""}
                       for r in (doc.get("recipients") or [])]
            csv_text = to_csv(columns, records)
            fname = f"campaign_{cid[:8]}_results.csv"
            return self._send(csv_text, ctype="text/csv; charset=utf-8",
                              extra={"Content-Disposition": f'attachment; filename="{fname}"'})

        if path == "/webhooks":
            if wati_webhook is None:
                return self._send("Webhook receiving is disabled.", status=503, ctype="text/plain")
            return self._send(webhooks_page(campaign_ui.webhook_setup(self._public_base())))

        if path == "/webhooks/feed":
            if wati_webhook is None:
                return self._send("")
            return self._send(campaign_ui.webhook_feed(
                unmatched_only=(qs.get("filter") or [""])[0] == "unmatched"))

        if path == "/filter-values":
            col = (qs.get("col") or [""])[0]
            if col not in sess["headers"]:
                return self._send("")
            return self._send(filter_values_fragment(sess, col))

        if path.startswith("/download/"):
            name = urllib.parse.unquote(path[len("/download/"):])
            if name in sess["files"]:
                return self._send(sess["files"][name], ctype="text/csv; charset=utf-8",
                                  extra={"Content-Disposition": f'attachment; filename="{name}"'})
            return self._send("Not found", status=404, ctype="text/plain")

        return self._send("Not found", status=404, ctype="text/plain")

    def _route_post(self):
        parsed = urllib.parse.urlparse(self.path)
        path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)

        # Before get_session(), for the same reason as the GET above: WATI is
        # not a browser and must not be issued a session cookie per receipt.
        if path == "/webhooks/wati":
            return self._webhook(qs)

        sess = self.get_session()
        body = self._body()
        if body is None:
            return self._send('<p class="text-sm text-rose-400">File too large (max 50 MB).</p>', status=413)
        ctype = self.headers.get("Content-Type", "")

        if path == "/load-bigin":
            if bigin_store is None:
                return self._send('<p class="text-sm text-rose-400">Bigin mirror unavailable — '
                                  'pymongo is not installed. Use the CSV upload.</p>')
            try:
                load_from_store(sess)
            except (bigin_store.StoreUnavailable, RuntimeError) as e:
                return self._send(f'<p class="text-sm text-rose-400">{esc(str(e))}</p>')
            if not sess["headers"]:
                return self._send('<p class="text-sm text-rose-400">The Bigin mirror is empty — '
                                  'run <code>python3 bigin_sync.py --full</code> first.</p>')
            return self._send(workspace_fragment(sess))

        if path == "/upload":
            m = re.search(r"boundary=(.+)", ctype)
            if not m:
                return self._send('<p class="text-sm text-rose-400">Bad upload.</p>')
            fields = parse_multipart(body, m.group(1).strip().strip('"').encode())
            raw = fields.get("file")
            if not raw:
                return self._send('<p class="text-sm text-rose-400">No file received.</p>')
            load_from_bytes(sess, raw if isinstance(raw, bytes) else raw.encode())
            if not sess["headers"]:
                return self._send('<p class="text-sm text-rose-400">The file appears to be empty.</p>')
            return self._send(workspace_fragment(sess))

        form = urllib.parse.parse_qs(body.decode("utf-8", "replace"), keep_blank_values=True)

        if path == "/add-filter":
            col = (form.get("col") or [""])[0]
            mode = (form.get("mode") or ["keep"])[0]
            values = set(form.get("values") or [])
            if col in sess["headers"] and values:
                sess["filters"].append({"col": col, "values": values, "mode": mode})
            return self._send(workspace_fragment(sess))

        if path == "/suppress-add":
            raw = (form.get("number") or [""])[0]
            cleaned = clean_phone(raw)
            if not cleaned:
                return self._send(suppress_chips_html(sess,
                    error=f"'{raw.strip()}' is not a valid 10-digit Indian number." if raw.strip()
                    else "Enter a number first."))
            if cleaned in sess["suppress"]:
                return self._send(suppress_chips_html(sess, error=f"{cleaned} is already in the list."))
            sess["suppress"].append(cleaned)
            return self._send(suppress_chips_html(sess))

        if path == "/suppress-remove":
            i = int((qs.get("i") or ["-1"])[0])
            if 0 <= i < len(sess["suppress"]):
                sess["suppress"].pop(i)
            return self._send(suppress_chips_html(sess))

        if path == "/remove-filter":
            i = int((qs.get("i") or ["-1"])[0])
            if 0 <= i < len(sess["filters"]):
                sess["filters"].pop(i)
            return self._send(workspace_fragment(sess))

        if path == "/generate":
            name_col = (form.get("name_col") or [""])[0]
            name_col2 = (form.get("name_col2") or [""])[0]
            phone_col = (form.get("phone_col") or [""])[0]
            country = (form.get("country") or [DEFAULT_COUNTRY_CODE])[0].strip() or DEFAULT_COUNTRY_CODE
            try:
                size = max(1, int((form.get("size") or ["250"])[0]))
            except ValueError:
                size = 250
            name_cols = [name_col] + ([name_col2] if name_col2 and name_col2 != name_col else [])
            suppress = set(sess["suppress"])
            return self._send(result_fragment(sess, name_cols, phone_col, country, size, suppress))

        # ------------------------------------------------------------------ #
        # WATI: test send, create, and the dashboard's row actions
        # ------------------------------------------------------------------ #
        if path == "/wati/test-send":
            if campaign_ui is None:
                return self._send('<p class="text-sm text-rose-400">WATI sending is disabled.</p>')
            return self._send(self._test_send(sess, form))

        if path == "/wati/create":
            if campaign_ui is None:
                return self._send('<p class="text-sm text-rose-400">WATI sending is disabled.</p>')
            return self._send(self._create_campaign(sess, form))

        if path.startswith("/campaigns/"):
            if campaign_ui is None:
                return self._send("")
            action = path[len("/campaigns/"):]
            cid = (qs.get("id") or [""])[0]

            # Group actions answer with whichever fragment the caller is showing:
            # the composer's confirmation, or the dashboard list.
            if action == "cancel-group":
                err = campaign_runner.cancel_group((qs.get("group") or [""])[0])
                if self.headers.get("HX-Target") == "wati-send":
                    return self._send(campaign_ui.send_fragment(
                        sess, error=err,
                        message=None if err else "Remaining batches cancelled."))
                return self._send(campaign_ui.campaigns_table(
                    notice=None if err else "Remaining batches cancelled.", error=err))

            err = None
            if action == "send-now":
                err = campaign_runner.send_now(cid)
                notice = "Sending started."
            elif action == "pause":
                err = campaign_runner.pause(cid)
                notice = "Pausing — the current batch finishes first."
            elif action == "resume":
                err = campaign_runner.resume(cid)
                notice = "Resumed from where it stopped."
            elif action == "cancel":
                err = campaign_runner.cancel(cid)
                notice = "Cancelled. Already-sent messages cannot be recalled."
            elif action == "retry-now":
                err = campaign_runner.retry_now(cid)
                notice = "Retry created — the failed leads are being sent again."
            elif action == "retry-off":
                err = campaign_runner.retry_off(cid)
                notice = "Automatic retry turned off."
            elif action == "delete":
                doc = campaign_store.get(cid)
                if doc and doc.get("status") in campaign_store.ACTIVE_STATUSES:
                    err, notice = "Cancel the campaign before deleting it.", None
                else:
                    campaign_store.delete(cid)
                    err, notice = None, "Campaign deleted."
            else:
                err, notice = "Unknown action.", None

            # The retry buttons live on the detail page, so they get the detail
            # page back — swapping in the dashboard list would throw away the
            # view the user was reading.
            if self.headers.get("HX-Target") == "campaign-detail":
                return self._send(campaign_ui.campaign_detail(
                    cid, notice=None if err else notice, error=err))
            return self._send(campaign_ui.campaigns_table(notice=None if err else notice, error=err))

        return self._send("Not found", status=404, ctype="text/plain")

    # ----------------------------------------------------------------------- #
    # The WATI webhook
    # ----------------------------------------------------------------------- #
    def _webhook(self, qs):
        """
        POST /webhooks/wati?token=… — one delivery receipt, read, reply or
        failure from WATI.

        Two rules shape this handler:

        * **Authenticate before anything else is done with the payload.**
          Without the check, anyone who found the URL could post fabricated
          delivery numbers into the dashboard.
        * **Answer 200 to anything we managed to read.** WATI retries on a
          non-2xx, so returning 500 because an event mentioned a campaign we
          do not have would turn one unmatched event into an endless stream of
          them. An event we cannot attribute is not an error; it is stored
          unattributed and shown on /webhooks, which is how a broken
          attribution rule becomes visible instead of silent.
        """
        if wati_webhook is None:
            return self._json({"ok": False, "error": "webhook module not loaded"}, 503)

        supplied = (qs.get("token") or [""])[0] or self.headers.get("X-Webhook-Token", "")
        if not wati_webhook.check_token(supplied):
            # Never log the value itself — only its length, so a misconfigured
            # webhook is visible in the log without a near-miss secret being
            # written to disk.
            # Length only, never the value: a misconfigured webhook has to be
            # visible in the log without a near-miss secret being written to
            # disk. The length is the diagnostic — one character over is a
            # paste artifact (a stray quote, a trailing slash), and far off is
            # the wrong token entirely.
            print(f"[wati/webhook] rejected: bad token (length={len(str(supplied))}, "
                  f"expected {len(wati_webhook.token())})")
            return self._json({"ok": False, "error": "invalid or missing token"}, 401)

        raw = self._body()
        if raw is None:
            return self._json({"ok": False, "error": "payload too large"}, 413)

        ctype = (self.headers.get("Content-Type") or "").lower()
        try:
            if "application/x-www-form-urlencoded" in ctype:
                form = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
                payload = {k: v[0] for k, v in form.items()}
            else:
                payload = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError:
            print(f"[wati/webhook] unreadable body ({len(raw)} bytes, {ctype or 'no content-type'})")
            return self._json({"ok": False, "error": "body was not JSON"}, 400)

        # Some tenants post a single event, some post an array of them.
        events = payload if isinstance(payload, list) else [payload]
        results = []
        for event in events[:200]:
            try:
                results.append(wati_webhook.handle(event))
            except Exception as e:                # noqa: BLE001 — see the docstring
                print(f"[wati/webhook] handler error: {type(e).__name__}: {e}")
                results.append({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return self._json({"ok": True, "received": len(results), "results": results})

    # ----------------------------------------------------------------------- #
    # WATI helpers — kept off the routing table so do_POST stays readable
    # ----------------------------------------------------------------------- #
    def _batch_preview(self, sess, form):
        """Re-render the schedule whenever a delivery control changes."""
        total = len(sess.get("wati_rows") or [])
        split, batch_size, gap, start, err = self._delivery_options(form)
        return campaign_ui.batch_preview_fragment(total, split, batch_size, gap, start, error=err)

    @staticmethod
    def _delivery_options(form):
        """
        Read the delivery controls out of a form or query string.

        Shared by the preview and the create handler so the schedule the user
        sees is computed by the same code that acts on it — the two cannot drift.
        Returns (split, batch_size, gap_minutes, start_utc_or_None, error).
        """
        def num(key, default, low, high):
            try:
                return max(low, min(high, int((form.get(key) or [str(default)])[0])))
            except ValueError:
                return default

        split = (form.get("split") or ["batches"])[0]
        batch_size = num("batch_size", 250, 1, 100000)
        gap = num("gap", 60, 0, 43200)

        start, err = None, None
        if (form.get("when") or ["now"])[0] == "later":
            raw = (form.get("scheduled_at") or [""])[0]
            start = campaign_runner.parse_ist(raw)
            if start is None:
                err = "That date and time could not be read."
            elif start < campaign_store.now_utc() - dt.timedelta(minutes=2):
                err = "That time is in the past. Pick a future time, or choose “Start now”."
        return split, batch_size, gap, start, err

    def _test_send(self, sess, form):
        rows = sess.get("wati_rows") or []
        template = (form.get("template") or [""])[0]
        if not template:
            return campaign_ui.test_result_fragment(None, error="Pick a template first.")
        tpl = campaign_ui.find_template(template)
        if not tpl:
            return campaign_ui.test_result_fragment(None, error=f"Template “{template}” is no longer approved.")

        country = (rows[0].get("CountryCode") if rows else None) or DEFAULT_COUNTRY_CODE
        raw = (form.get("test_numbers") or [""])[0]
        numbers, bad = [], []
        for chunk in re.split(r"[,\s;]+", raw):
            if not chunk.strip():
                continue
            cleaned = clean_phone(chunk)
            if cleaned:
                numbers.append(wati_client.normalize_number(country, cleaned))
            else:
                bad.append(chunk.strip())
        if not numbers:
            return campaign_ui.test_result_fragment(
                None, error=f"No valid numbers in “{esc(raw)}”." if raw.strip() else "Enter at least one number.")

        # Real values from the first recipient, so the test shows what a real
        # contact will actually receive rather than placeholder text.
        sample = resolve_params(tpl["params"], rows[0], form) if rows else {}
        results = campaign_runner.test_send(numbers, template,
                                            params_by_number={n: sample for n in numbers},
                                            broadcast_name=f"test_{template}"[:60])
        html_out = campaign_ui.test_result_fragment(results)
        if bad:
            html_out += (f'<p class="mt-1 text-xs text-amber-400">Ignored (not valid): '
                         f'{esc(", ".join(bad))}</p>')
        return html_out

    def _create_campaign(self, sess, form):
        rows = sess.get("wati_rows") or []
        if not rows:
            return campaign_ui.list_gone_fragment(sess)

        def bad(msg):
            return campaign_ui.send_fragment(sess, error=msg)

        if (form.get("confirm") or [""])[0] != "yes":
            return bad("Tick the confirmation box — this messages real people.")

        template = (form.get("template") or [""])[0]
        if not template:
            return bad("Pick an approved WATI template.")
        tpl = campaign_ui.find_template(template)
        if not tpl:
            return bad(f"Template “{template}” is not in the approved list any more.")

        name = (form.get("name") or [""])[0].strip()
        if not name:
            return bad("Give the campaign a name so you can find it later.")

        split, batch_size, gap, scheduled_at, err = self._delivery_options(form)
        if err:
            return bad(err)

        try:
            throttle = max(1, min(1000, int((form.get("throttle") or ["60"])[0])))
        except ValueError:
            throttle = 60
        dry_run = (form.get("dry_run") or ["0"])[0] == "1"

        recipients = build_recipients(sess, tpl, form)
        if not recipients:
            return bad("No valid phone numbers in the generated list.")

        start = scheduled_at or campaign_store.now_utc()
        if split != "batches" or batch_size >= len(recipients):
            plan = [(1, len(recipients), start)]
            group_id = None
        else:
            plan = campaign_runner.plan_batches(len(recipients), batch_size, gap, start)
            group_id = secrets.token_hex(8)

        # One campaign per batch. Created oldest-first so that if creation fails
        # partway, what exists is a valid prefix of the plan rather than holes
        # in the middle — and the user is told exactly how far it got.
        docs, offset = [], 0
        for index, size, when in plan:
            slice_ = recipients[offset:offset + size]
            offset += size
            label = name if group_id is None else f"{name} — batch {index}/{len(plan)}"
            try:
                docs.append(campaign_store.create(
                    name=label, template_name=template, recipients=slice_,
                    scheduled_at=when, throttle=throttle,
                    source=sess.get("source"), dry_run=dry_run,
                    group_id=group_id, batch_index=index, batch_count=len(plan),
                ))
            except Exception as e:                # noqa: BLE001
                if not docs:
                    return bad(f"Could not save the campaign: {e}")
                return campaign_ui.created_fragment(
                    sess, docs, group_id,
                    warning=f"Only {len(docs)} of {len(plan)} batches were saved — {e}")

        # A first batch due now starts this instant rather than waiting for the
        # next scheduler tick, so the dashboard shows movement immediately.
        if scheduled_at is None:
            campaign_runner.send_now(docs[0]["campaign_id"])
            docs[0] = campaign_store.get(docs[0]["campaign_id"]) or docs[0]
        return campaign_ui.created_fragment(sess, docs, group_id)

    def log_message(self, *args):
        """
        No per-request access log. Two consequences worth knowing:

        * The webhook token lives in the query string, so this is also what
          keeps the shared secret out of the journal. Do not "helpfully" turn
          access logging back on without redacting `token=` first.
        * A webhook event therefore prints exactly one line, and
          `wati_webhook._log_event` is what prints it — a line naming the
          contact, the campaign and what changed, rather than a URL.
        """


if __name__ == "__main__":
    # Line-buffer stdout. Run under systemd or nohup it is a pipe, not a
    # terminal, so Python block-buffers it: every log line — the webhook's
    # per-event lines, a rejected token, a scheduler error — sits in an 8 KB
    # buffer instead of reaching the journal, and a quiet service looks like a
    # dead one. Costs nothing at this volume.
    import sys as _sys
    for _stream in (_sys.stdout, _sys.stderr):
        try:
            _stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):     # not a text stream; nothing to do
            pass

    # The scheduler has to be up before the first request: a campaign scheduled
    # yesterday for 06:00 today is due the moment this process starts.
    if campaign_runner is not None:
        campaign_runner.start()
        kind, note = campaign_store.backend()
        print(f"WATI scheduler running · campaigns stored in {note}")

    print(f"Bigin -> WATI cleaner listening on http://{HOST}:{PORT}")
    print("Press Ctrl+C to stop.")
    try:
        ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")

#!/usr/bin/env python3
"""
campaign_ui.py

Every HTML fragment for the WATI half of the app: the "Send / schedule" step
that appears under the generated batches, the Campaigns dashboard, and the
delivery-webhook page that says what became of the messages after they left.

Kept out of `app.py` for the same reason `bigin_store.py` is — that file owns
the cleaner, this one owns the sender, and neither imports the other. `app.py`
imports this module and wires the routes; nothing here knows about HTTP.

Standard library only, same as the rest.
"""

import datetime as dt
import html
import time

import campaign_retry
import campaign_runner
import campaign_store as store
import ui
import wati_client
import wati_webhook

def esc(value):
    """html.escape, but tolerant: WATI hands back ints and None where the docs
    promise strings, and a template preview must not 500 over one of them."""
    return html.escape("" if value is None else str(value))


# Fetching 133 templates takes ~1s; the picker is re-rendered on every param
# change, so the list is cached briefly rather than re-fetched each time.
_TEMPLATE_CACHE = {"at": 0.0, "items": None, "error": None}
TEMPLATE_TTL = 300


def templates(force=False):
    """(items, error) — never raises, so a WATI outage degrades to a message."""
    now = time.time()
    if not force and _TEMPLATE_CACHE["items"] is not None and now - _TEMPLATE_CACHE["at"] < TEMPLATE_TTL:
        return _TEMPLATE_CACHE["items"], _TEMPLATE_CACHE["error"]
    try:
        items = wati_client.list_templates(approved_only=True)
        _TEMPLATE_CACHE.update(at=now, items=items, error=None)
    except (wati_client.WatiError, wati_client.WatiUnavailable) as e:
        _TEMPLATE_CACHE.update(at=now, items=_TEMPLATE_CACHE["items"] or [], error=str(e))
    return _TEMPLATE_CACHE["items"], _TEMPLATE_CACHE["error"]


def find_template(name):
    items, _ = templates()
    for t in items:
        if t["name"] == name:
            return t
    return None


# --------------------------------------------------------------------------- #
# small shared bits
# --------------------------------------------------------------------------- #
STATUS_STYLE = {                        # status -> (text colour, pill border + background)
    "scheduled": ("text-sky-300", "border-sky-700 bg-sky-500/10"),
    "running":   ("text-emerald-300", "border-emerald-700 bg-emerald-500/10"),
    "paused":    ("text-amber-300", "border-amber-700 bg-amber-500/10"),
    "completed": ("text-slate-300", "border-slate-600 bg-slate-700/40"),
    "cancelled": ("text-slate-400", "border-slate-700 bg-slate-800"),
    "failed":    ("text-rose-300", "border-rose-800 bg-rose-500/10"),
}


def status_pill(status):
    text, box = STATUS_STYLE.get(status, ("text-slate-300", "border-slate-600 bg-slate-800"))
    return f'<span class="rounded-full border px-2.5 py-0.5 text-xs {text} {box}">{esc(status)}</span>' 


def ist(moment, fmt="%d %b %Y, %H:%M"):
    if not isinstance(moment, dt.datetime):
        return "—"
    return moment.astimezone(store.IST).strftime(fmt) + " IST"


def _select(name, choices, selected=None, extra_class="", attrs=""):
    opts = []
    for value, label in choices:
        sel = " selected" if value == selected else ""
        opts.append(f'<option value="{esc(value)}"{sel}>{esc(label)}</option>')
    return (f'<select name="{esc(name)}" {attrs} class="{ui.FIELD} {extra_class}">'
            f'{"".join(opts)}</select>')


def default_schedule_value():
    """datetime-local default: an hour from now, in IST, rounded to :00."""
    when = dt.datetime.now(store.IST) + dt.timedelta(hours=1)
    return when.replace(minute=0, second=0, microsecond=0).strftime("%Y-%m-%dT%H:%M")


# --------------------------------------------------------------------------- #
# STEP 5 — send / schedule
# --------------------------------------------------------------------------- #
def list_gone_fragment(sess=None):
    """
    Step 5 with nothing to send. Two very different reasons land here and they
    must not share a message:

    * the session still holds contacts, but filtering and suppression left no
      valid phone numbers — the user's own doing, and fixable one step up;
    * the session holds nothing at all, because the app restarted or the
      two-hour idle timeout evicted it.

    Telling someone their session expired when really their filter was too
    tight sends them looking in the wrong place.
    """
    has_rows = bool((sess or {}).get("rows"))
    if has_rows:
        return ui.section("05", "Send or schedule", anchor="wati-send",
                          meta=ui.tag("nothing to send", "wait"), tone="violet", body=f"""
          {ui.banner("No valid contacts came out of step 3 — every row was filtered out, suppressed, or had an unusable phone number.", "wait")}
          <p class="mt-4 text-sm text-slate-300">Loosen a filter in step 2, or check the phone column mapping
            in step 3, then generate again.</p>
        """)
    return ui.section("05", "Send or schedule", anchor="wati-send", meta=ui.tag("no list", "wait"), tone="violet", body=f"""
      {ui.banner("This browser session no longer has a generated list — the app restarted, or the session timed out.", "wait")}
      <p class="mt-4 text-sm text-slate-300">Load your contacts and press <b>Generate WATI files</b> again.
        Campaigns already created are unaffected and are still running or scheduled on the
        <a href="/campaigns" class="{ui.LINK}">campaigns page</a>.</p>
    """)


def send_fragment(sess, message=None, error=None):
    """
    The campaign composer.

    Requires sess['wati_rows'] — the cleaned, de-duplicated recipients from the
    Generate step. Sending is deliberately gated behind an explicit checkbox and
    a recipient count set large enough to register, because this is the one
    button in the app that cannot be undone.
    """
    rows = sess.get("wati_rows") or []
    if not rows:
        return list_gone_fragment(sess)

    items, terr = templates()
    kind, note_text = store.backend()

    if terr and not items:
        return ui.section("05", "Send or schedule", anchor="wati-send",
                          meta=ui.tag("wati unreachable", "wait"), tone="violet", body=f"""
          {ui.banner(esc(terr), "signal")}
          <p class="mt-4 text-sm text-slate-300">Check WATI_API_URL and WATI_TOKEN in .env, then
            <button class="{ui.LINK}" hx-get="/wati/refresh" hx-target="#wati-send"
                    hx-swap="outerHTML">retry</button>.</p>
        """)

    choices = [("", "— choose a template —")] + [
        (t["name"], f'{t["name"]}   {t["category"].lower()}' + (f'   {len(t["params"])} param' if t["params"] else ""))
        for t in items]

    msg = ""
    if error:
        msg = '<div class="mt-5">' + ui.banner(esc(error), "signal") + "</div>"
    elif message:
        msg = '<div class="mt-5">' + ui.banner(message, "go") + "</div>"

    body = f"""
      <form hx-post="/wati/create" hx-target="#wati-send" hx-swap="outerHTML" class="space-y-7">

        <div class="grid gap-5 lg:grid-cols-2">
          <div>
            <label class="{ui.LABEL}">Campaign name — yours, for the dashboard</label>
            <input name="name" required placeholder="CA Inter reactivation — August" class="{ui.FIELD}">
          </div>
          <div>
            <label class="{ui.LABEL}">
              Approved WATI template
              <button type="button" class="ml-2 normal-case tracking-normal {ui.LINK}"
                      hx-get="/wati/refresh" hx-target="#wati-send" hx-swap="outerHTML">refresh</button>
            </label>
            {_select("template", choices, extra_class="font-mono text-xs",
                     attrs='hx-get="/wati/template" hx-target="#tpl-detail" hx-swap="innerHTML" hx-trigger="change" required')}
          </div>
        </div>

        <div id="tpl-detail"></div>

        <!-- Delivery. Any change in here bubbles up to this element, which
             re-renders the schedule preview — one handler instead of one per
             control. -->
        <div hx-get="/wati/batch-preview" hx-include="closest form"
             hx-target="#batch-preview" hx-swap="innerHTML" hx-trigger="change"
             class="{ui.PANEL} space-y-5">
          <p class="{ui.LABEL} mb-0">Delivery</p>

          <div class="grid gap-5 sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <label class="{ui.LABEL}">Send as</label>
              {_select("split", [("batches", "Batches — staggered"), ("one", "One campaign — all at once")],
                       selected="batches")}
              {ui.note("Batches let you stop after any one of them.")}
            </div>
            <div>
              <label class="{ui.LABEL}">Contacts per batch</label>
              {_select("batch_size", [("100", "100"), ("250", "250"), ("500", "500"), ("1000", "1000")],
                       selected="250")}
            </div>
            <div>
              <label class="{ui.LABEL}">Gap between batches</label>
              {_select("gap", [("15", "15 minutes"), ("30", "30 minutes"), ("60", "1 hour"),
                               ("120", "2 hours"), ("240", "4 hours"), ("720", "12 hours"),
                               ("1440", "1 day")], selected="60")}
            </div>
            <div>
              <label class="{ui.LABEL}">Mode</label>
              {_select("dry_run", [("0", "Live — really send"), ("1", "Dry run — log only")], selected="0")}
              {ui.note("Dry run walks the list without calling WATI.")}
            </div>
          </div>

          <div class="grid gap-5 border-t border-dashed border-slate-700 pt-5 sm:grid-cols-2 xl:grid-cols-4">
            <div>
              <label class="{ui.LABEL}">First batch</label>
              {_select("when", [("now", "Start now"), ("later", "Start later")],
                       attrs='hx-get="/wati/when" hx-target="#when-detail" hx-swap="innerHTML" hx-trigger="change"')}
            </div>
            <div id="when-detail"></div>
            <div>
              <label class="{ui.LABEL}">Rate limit — within a batch</label>
              {_select("throttle", [("30", "30 / min — gentle"), ("60", "60 / min — default"),
                                    ("120", "120 / min"), ("300", "300 / min — fast")], selected="60")}
              {ui.note("Protects your number's quality rating.")}
            </div>
          </div>
        </div>

        <div id="batch-preview">{batch_preview_fragment(len(rows), "batches", 250, 60, None)}</div>

        <details class="{ui.WELL}">
          <summary class="cursor-pointer text-xs text-slate-300
                          transition-colors hover:text-slate-100">
            Test it on your own phone first — recommended
          </summary>
          <div class="mt-4 flex flex-wrap items-end gap-3">
            <div class="min-w-[260px] flex-1">
              <label class="{ui.LABEL}">Up to 5 numbers, comma separated</label>
              <input name="test_numbers" placeholder="9876543210, +91 98765 43211" class="{ui.FIELD_MONO}">
            </div>
            <button type="button" class="{ui.BTN_GHOST}" hx-post="/wati/test-send" hx-include="closest form"
                    hx-target="#test-result" hx-swap="innerHTML" hx-indicator="#test-spin">Send test</button>
            <span id="test-spin" class="htmx-indicator font-mono text-xs uppercase text-slate-500">sending…</span>
          </div>
          <div id="test-result" class="mt-3"></div>
          {ui.note("Test messages use the first recipient's real values, so you see what a contact will see.")}
        </details>

        <label class="flex items-start gap-2 rounded-lg border border-amber-800 bg-amber-500/5 p-3 text-sm">
          <input type="checkbox" name="confirm" value="yes" required class="mt-0.5 accent-amber-500">
          <span class="text-amber-200">
            I understand this will message <b>{len(rows):,}</b> people on WhatsApp and cannot be undone
            once it starts.
          </span>
        </label>

        <div class="flex flex-wrap items-center gap-4">
          <button class="{ui.BTN_SIGNAL}">Create campaign</button>
          <span class="htmx-indicator text-xs text-slate-500">working…</span>
          <a href="/campaigns" class="{ui.LINK} text-sm">Open the campaigns dashboard &rarr;</a>
        </div>
      </form>
      {msg}
    """

    meta = (ui.tag(f"{len(rows):,} recipients", "signal")
            + ui.tag(f"stored in {esc(kind)}", "quiet"))
    return ui.section("05", "Send or schedule", anchor="wati-send", meta=meta, body=body, tone="violet")


def when_fragment(mode):
    """The start-time input, shown only when 'Start later' is picked."""
    if mode != "later":
        return ('<p class="mt-7 text-xs text-slate-500">The first batch starts as soon as you create it.</p>')
    return f"""
      <label class="{ui.LABEL}">First batch — date &amp; time (IST)</label>
      <input type="datetime-local" name="scheduled_at" value="{default_schedule_value()}" class="{ui.FIELD_MONO}">
      {ui.note("Fires even if this browser is closed.")}
    """


def template_detail_fragment(sess, name):
    """Template preview plus one mapping row per {{parameter}}."""
    if not name:
        return ""
    tpl = find_template(name)
    if not tpl:
        return f'<p class="text-sm text-rose-400">Template “{esc(name)}” is not in the approved list any more.</p>'

    headers = sess.get("headers") or []
    parts = []
    if tpl["header"]:
        parts.append(f'<p class="mb-1.5 text-sm font-semibold text-slate-100">{esc(tpl["header"])}</p>')
    parts.append(f'<p class="whitespace-pre-wrap text-sm leading-relaxed text-slate-300">{esc(tpl["body"])[:1200]}</p>')
    if tpl["footer"]:
        parts.append(f'<p class="mt-2 text-xs text-slate-500">{esc(tpl["footer"])}</p>')
    if tpl["buttons"]:
        chips = "".join(f'<span class="border border-slate-700 bg-slate-900 px-2 py-0.5 text-xs text-slate-300">'
                        f'{esc(b)}</span>' for b in tpl["buttons"])
        parts.append(f'<div class="mt-3 flex flex-wrap gap-1.5 border-t border-dashed border-slate-700 pt-3">{chips}</div>')

    if not tpl["params"]:
        mapping = '<p class="text-xs text-slate-500">This template takes no parameters — nothing to map.</p>'
    else:
        choices = [("__name__", "Contact name"), ("__phone__", "Phone (10 digits)"), ("__static__", "Static text")]
        choices += [(f"col:{h}", f"Field · {h}") for h in headers]
        rows = []
        for p in tpl["params"]:
            guess = ("__name__" if p.lower() in ("name", "first_name", "fullname", "full_name")
                     else "__phone__" if p.lower() in ("phone", "mobile", "number") else "__static__")
            rows.append(f"""
              <div class="grid items-center gap-2 border-b border-dashed border-slate-700 py-2 last:border-0
                          sm:grid-cols-[minmax(0,7rem)_1fr_1fr]">
                <code class="font-mono text-xs text-rose-400">{{{{{esc(p)}}}}}</code>
                {_select(f"param_src_{p}", choices, selected=guess)}
                <input name="param_val_{esc(p)}" placeholder="static / fallback when blank" class="{ui.FIELD}">
              </div>""")
        mapping = ('<p class="mb-1 text-xs text-slate-500">Fill each parameter from a field, or type a fixed value.</p>'
                   + "".join(rows))

    return f"""
    <div class="grid gap-5 lg:grid-cols-2">
      <div class="{ui.WELL}">
        <p class="{ui.LABEL}">Preview · {esc(tpl["category"].lower())} · {esc(str(tpl["language"] or ""))}</p>
        <div class="border-l-2 border-emerald-700 bg-slate-900 p-4">{''.join(parts)}</div>
      </div>
      <div class="{ui.WELL}">
        <p class="{ui.LABEL}">Parameters</p>
        {mapping}
      </div>
    </div>
    """


def batch_preview_fragment(total, split, batch_size, gap_minutes, start_utc, error=None):
    """
    The exact schedule the user is about to commit to, before they commit.

    Showing "batch 7 of 10 · 250 contacts · 21 Aug 16:00 IST" up front is the
    difference between scheduling a staggered send and hoping one happened.
    """
    if error:
        return ui.banner(esc(error), "signal")
    if total <= 0:
        return ""

    start = start_utc or store.now_utc()
    starts_now = start_utc is None

    if split != "batches" or batch_size >= total:
        when = "as soon as you create it" if starts_now else f"at {ist(start)}"
        return f"""
        <div class="{ui.WELL}">
          <p class="{ui.LABEL}">Plan</p>
          <p class="text-sm text-slate-300"><b class="font-mono text-slate-100">{total:,}</b> contacts in one campaign,
            starting {esc(when)}.</p>
          {ui.note("Pausing it stops the whole thing; batches let you stop between them instead.")}
        </div>"""

    plan = campaign_runner.plan_batches(total, batch_size, gap_minutes, start)
    rows = []
    for index, size, when in plan:
        label = "now" if (starts_now and index == 1) else ist(when)
        rows.append(
            f'<tr class="border-b border-dashed border-slate-700 last:border-0">'
            f'<td class="w-24 py-1.5 pl-3 pr-2 text-xs text-slate-500">'
            f'Batch&nbsp;{index}</td>'
            f'<td class="w-16 px-2 py-1.5 text-right font-mono text-xs">{size:,}</td>'
            f'<td class="w-full py-1.5 pl-5 pr-3 font-mono text-xs text-slate-300">{esc(label)}</td></tr>')

    last = plan[-1][2]
    hours = (last - start).total_seconds() / 3600
    spread = ("all at once" if hours == 0 else
              f"spread over {hours:.0f} hour{'s' if hours != 1 else ''}" if hours < 48 else
              f"spread over {hours / 24:.0f} days")

    return f"""
    <div class="{ui.WELL}">
      <div class="mb-3 flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1">
        <p class="{ui.LABEL} mb-0">Plan</p>
        <p class="text-xs text-slate-500">
          finishing {esc(ist(last))}
        </p>
      </div>
      <p class="mb-3 text-sm text-slate-300">
        <b class="font-mono text-slate-100">{len(plan)}</b> batches of up to
        <b class="font-mono text-slate-100">{batch_size:,}</b> · {total:,} contacts · {esc(spread)}.
      </p>
      <div class="max-h-[19rem] overflow-auto border border-slate-700 bg-slate-900">
        <table class="w-full"><tbody>{''.join(rows)}</tbody></table>
      </div>
      {ui.note("Each batch is its own campaign — pause, cancel or reschedule any one without touching the rest.")}
    </div>
    """


def test_result_fragment(results, error=None):
    if error:
        return f'<p class="text-sm text-rose-400">{esc(error)}</p>'
    if not results:
        return '<p class="text-sm text-slate-500">No valid numbers entered.</p>'
    lines = []
    for r in results:
        if r["ok"]:
            lines.append(f'<li class="flex items-center gap-2 py-0.5 text-emerald-400">'
                         f'<span class="inline-block h-1.5 w-1.5 bg-emerald-500"></span>'
                         f'<span class="font-mono text-xs">{esc(r["phone"])}</span>'
                         f'<span class="font-mono text-xs">sent</span></li>')
        else:
            lines.append(f'<li class="flex items-start gap-2 py-0.5 text-rose-400">'
                         f'<span class="mt-1.5 inline-block h-1.5 w-1.5 bg-rose-500"></span>'
                         f'<span class="font-mono text-xs">{esc(r["phone"])}</span>'
                         f'<span class="text-xs">{esc(r["error"] or "failed")}</span></li>')
    return f'<ul>{"".join(lines)}</ul>'


def created_fragment(sess, docs, group_id=None, warning=None):
    """What replaces the composer once the campaigns exist."""
    if isinstance(docs, dict):          # single-campaign callers
        docs = [docs]
    if not docs:
        return list_gone_fragment(sess)

    first = docs[0]
    total = sum(d["total"] for d in docs)
    dry = ui.tag("dry run — nothing sent", "wait") if first.get("dry_run") else ""

    warn = '<div class="mt-5">' + ui.banner(esc(warning), "wait") + "</div>" if warning else ""

    if len(docs) == 1:
        when = ("immediately"
                if first.get("scheduled_at") and first["scheduled_at"] <= store.now_utc() + dt.timedelta(seconds=30)
                else f"at {ist(first.get('scheduled_at'))}")
        summary = f"""
          <p class="font-semibold text-xl font-semibold text-slate-100">{esc(first["name"])}</p>
          <div class="mt-4 max-w-md">
            {ui.stat("Recipients", f"{total:,}")}
            {ui.stat("Template", esc(first["template_name"]))}
            {ui.stat("Starts", esc(when))}
            {ui.stat("WATI broadcast", esc(first["broadcast_name"]))}
          </div>"""
        group_button = ""
    else:
        rows = []
        for d in docs:
            starts_now = d.get("scheduled_at") and d["scheduled_at"] <= store.now_utc() + dt.timedelta(seconds=30)
            rows.append(
                f'<tr class="border-b border-dashed border-slate-700 last:border-0">'
                f'<td class="w-24 py-1.5 pl-3 pr-2 text-xs text-slate-500">'
                f'Batch&nbsp;{d.get("batch_index")}</td>'
                f'<td class="w-16 px-2 py-1.5 text-right font-mono text-xs">{d["total"]:,}</td>'
                f'<td class="w-full py-1.5 pl-5 pr-3 font-mono text-xs text-slate-300">'
                f'{"now" if starts_now else esc(ist(d.get("scheduled_at")))}</td></tr>')
        summary = f"""
          <p class="text-sm text-slate-300">
            <b class="font-mono text-slate-100">{len(docs)}</b> batches · <b class="font-mono text-slate-100">{total:,}</b>
            recipients · template <span class="font-mono text-xs text-rose-400">{esc(first["template_name"])}</span>
          </p>
          <div class="mt-4 max-h-60 max-w-md overflow-auto border border-slate-700 bg-slate-900">
            <table class="w-full"><tbody>{"".join(rows)}</tbody></table>
          </div>"""
        group_button = (
            f'<button class="{ui.BTN_GHOST} border-rose-800 text-rose-400 hover:bg-rose-500 hover:text-white" '
            f'hx-post="/campaigns/cancel-group?group={esc(group_id or "")}" '
            f'hx-confirm="Cancel every batch that has not finished yet?" '
            f'hx-target="#wati-send" hx-swap="outerHTML">Cancel all batches</button>')

    body = f"""
      {summary}
      {warn}
      <div class="mt-6 flex flex-wrap gap-3">
        <a href="/campaigns" class="{ui.BTN}">Watch it in the dashboard</a>
        <button class="{ui.BTN_GHOST}" hx-get="/wati/compose" hx-target="#wati-send" hx-swap="outerHTML">
          Create another from the same list
        </button>
        {group_button}
      </div>
    """
    meta = ui.tag("created", "go") + dry
    return ui.section("05", "Batches scheduled" if len(docs) > 1 else "Campaign created",
                      anchor="wati-send", meta=meta, body=body, tone="violet")


# --------------------------------------------------------------------------- #
# Campaigns dashboard
# --------------------------------------------------------------------------- #
# The list polls itself rather than the page polling it, so the same fragment
# works standalone on the detail page and inside the dashboard.
REFRESH = 'hx-get="/campaigns/list" hx-trigger="every 5s" hx-swap="outerHTML"'


def campaigns_table(notice=None, error=None):
    rows = store.list_campaigns(limit=100)
    _kind, note_text = store.backend()

    msg = ""
    if error:
        msg = '<div class="mb-5">' + ui.banner(esc(error), "signal") + "</div>"
    elif notice:
        msg = '<div class="mb-5">' + ui.banner(esc(notice), "go") + "</div>"

    if not rows:
        return f"""
        <div id="campaign-list" {REFRESH}>
          {msg}
          <div class="border-t border-slate-700 py-20 text-center">
            <p class="font-semibold text-2xl font-semibold text-slate-600">Nothing scheduled</p>
            <p class="mx-auto mt-3 max-w-md text-sm text-slate-500">
              Build a list on the <a href="/" class="{ui.LINK}">cleaner page</a>, then use step 5
              to send or schedule a campaign.
            </p>
          </div>
        </div>"""

    head = "".join(
        f'<th class="{"pl-0" if i == 0 else "px-3"} py-2 text-left font-mono text-xs font-medium '
        f' text-slate-500 {cls}">{label}</th>'
        for i, (label, cls) in enumerate([("Campaign", ""), ("Template", "hidden lg:table-cell"),
                                          ("Status", ""), ("Progress", "min-w-[150px]"),
                                          ("Delivery", "hidden md:table-cell min-w-[170px]"),
                                          ("When", "hidden sm:table-cell"), ("", "text-right")]))

    body = "".join(_campaign_row(c) for c in rows)
    return f"""
    <div id="campaign-list" {REFRESH}>
      {msg}
      <div class="overflow-x-auto rounded-xl border border-slate-700 bg-slate-800/60">
        <table class="w-full text-sm">
          <thead class="bg-slate-900/60 text-xs uppercase tracking-wide text-slate-400">
            <tr>{head}</tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
      <p class="mt-2 text-xs text-slate-500">
        Live · refreshes every 5s · {esc(note_text)}
      </p>
    </div>
    """


def _campaign_row(c):
    total = max(1, int(c.get("total") or 0))
    done = int(c.get("sent") or 0) + int(c.get("failed") or 0)
    pct = min(100, round(done * 100 / total))
    failed = int(c.get("failed") or 0)
    cid = c["campaign_id"]
    status = c.get("status", "?")

    fill = {"running": "bg-emerald-500", "completed": "bg-slate-400", "paused": "bg-amber-500",
            "failed": "bg-rose-500", "cancelled": "bg-slate-600"}.get(status, "bg-sky-500")

    def btn(action, label, cls=ui.BTN_XS, confirm=None):
        extra = f' hx-confirm="{confirm}"' if confirm else ""
        return (f'<button class="{cls}" hx-post="/campaigns/{action}?id={cid}"'
                f' hx-target="#campaign-list" hx-swap="outerHTML"{extra}>{label}</button>')

    actions = []
    if status == "scheduled":
        actions += [btn("send-now", "Send now", ui.BTN_XS_GO), btn("cancel", "Cancel")]
    elif status == "running":
        actions += [btn("pause", "Pause"), btn("cancel", "Cancel", ui.BTN_XS_SIGNAL)]
    elif status == "paused":
        actions += [btn("resume", "Resume", ui.BTN_XS_GO), btn("cancel", "Cancel")]
    else:
        actions += [btn("delete", "Delete", ui.BTN_XS_SIGNAL)]
    if c.get("group_id") and status in store.ACTIVE_STATUSES:
        actions.append(
            f'<button class="{ui.BTN_XS_SIGNAL}" hx-post="/campaigns/cancel-group?group={esc(c["group_id"])}"'
            f' hx-confirm="Cancel every unfinished batch of this campaign?"'
            f' hx-target="#campaign-list" hx-swap="outerHTML"'
            f' title="Cancel this batch and all its siblings">Stop all</button>')
    actions.append(f'<a class="{ui.BTN_XS}" href="/campaigns/detail?id={cid}">Details</a>')

    when = (ist(c.get("scheduled_at")) if status == "scheduled"
            else ist(c.get("finished_at")) if c.get("finished_at")
            else ist(c.get("started_at") or c.get("created_at")))

    marks = []
    if c.get("batch_count") and c["batch_count"] > 1:
        marks.append(f'<span class="text-xs text-rose-400">'
                     f'Batch {c.get("batch_index")}/{c["batch_count"]}</span>')
    if c.get("dry_run"):
        marks.append('<span class="text-xs text-amber-400">Dry run</span>')
    # Retry marks read off the top-level fields only. The dashboard drops
    # recipient arrays (see list_campaigns), so anything needing a walk of the
    # list belongs on the detail page, not here.
    if c.get("retry_of"):
        marks.append(f'<span class="text-xs text-violet-300">'
                     f'Retry {c.get("retry_round") or 1}</span>')
    if c.get("retry_state") == "armed" and c.get("retry_due_at"):
        marks.append(f'<span class="text-xs text-amber-400" '
                     f'title="Failed leads are collected and resent then">'
                     f'Retrying failures {esc(ist(c["retry_due_at"], "%d %b"))}</span>')
    elif c.get("retry_state") == "done" and c.get("retry_campaign_id"):
        marks.append(f'<a class="text-xs text-emerald-400 hover:underline" '
                     f'href="/campaigns/detail?id={esc(c["retry_campaign_id"])}">'
                     f'Failures retried &rarr;</a>')
    marks.append(f'<span class="text-xs text-slate-500">{c.get("total", 0):,} recipients · '
                 f'{esc(c.get("source") or "list")}</span>')

    return f"""
    <tr class="border-t border-slate-700 align-middle">
      <td class="px-3 py-2">
        <div class="font-medium text-slate-100">{esc(c.get("name") or "—")}</div>
        <div class="mt-0.5 flex flex-wrap items-center gap-2">{"".join(marks)}</div>
      </td>
      <td class="hidden px-3 py-2 lg:table-cell"><code class="text-xs text-violet-300">
        {esc(c.get("template_name") or "")}</code></td>
      <td class="px-3 py-2">{status_pill(status)}</td>
      <td class="px-3 py-2">
        <div class="h-1.5 w-full overflow-hidden rounded-full bg-slate-700">
          <div class="h-full {fill}" style="width:{pct}%"></div>
        </div>
        <div class="mt-1 text-xs text-slate-400">
          {c.get("sent", 0):,} sent{f' · <span class="text-rose-400">{failed:,} failed</span>' if failed else ''}
          · {pct}%
        </div>
      </td>
      <td class="hidden px-3 py-2 md:table-cell">{delivery_cell(c)}</td>
      <td class="hidden px-3 py-2 text-xs text-slate-400 sm:table-cell">{esc(when)}</td>
      <td class="px-3 py-2"><div class="flex flex-wrap justify-end gap-1.5">{''.join(actions)}</div></td>
    </tr>
    """


def campaign_detail(campaign_id, notice=None, error=None):
    c = store.get(campaign_id)
    if not c:
        return ('<div id="campaign-detail"><p class="border-t border-slate-700 py-16 '
                'text-center text-sm text-rose-400">Campaign not found.</p></div>')

    recipients = c.get("recipients") or []
    failures = [r for r in recipients if r.get("status") == "failed"]

    msg = ""
    if error:
        msg = '<div class="mb-5">' + ui.banner(esc(error), "signal") + "</div>"
    elif notice:
        msg = '<div class="mb-5">' + ui.banner(esc(notice), "go") + "</div>"

    # Built once and placed below: each walks the recipient list, and on a
    # 2 000-contact campaign the difference between one walk and three is
    # visible in the page load.
    replies = replies_block(c)
    blocked = blocked_block(c)
    retry = retry_block(c)

    log = "".join(
        f'<li class="border-b border-dashed border-slate-700 py-1.5 last:border-0">'
        f'<span class="font-mono text-xs text-slate-500">'
        f'{esc((e.get("ts") or "")[:19].replace("T", " "))}</span>'
        f'<span class="ml-2 text-slate-300">{esc(e.get("msg") or "")}</span></li>'
        for e in (c.get("log") or [])[-30:])

    if failures:
        fail_rows = "".join(
            f'<tr class="border-b border-dashed border-slate-700 last:border-0">'
            f'<td class="py-1.5 pl-3 pr-2 font-mono text-xs">{esc(r["phone"])}</td>'
            f'<td class="px-2 py-1.5 text-xs text-slate-300">{esc(r.get("name") or "")}</td>'
            f'<td class="py-1.5 pl-2 pr-3 text-xs text-rose-400">{esc(r.get("error") or "")}</td></tr>'
            for r in failures[:200])
        fail_block = f"""
          <div class="mt-8">
            <div class="mb-2 flex flex-wrap items-baseline justify-between gap-3">
              <p class="{ui.LABEL} mb-0">Failures — {len(failures):,}{' · first 200' if len(failures) > 200 else ''}</p>
              <a class="{ui.LINK} text-xs" href="/campaigns/export?id={campaign_id}" download>
                download full results CSV</a>
            </div>
            <div class="max-h-80 overflow-auto border border-slate-700 bg-slate-900">
              <table class="w-full"><tbody>{fail_rows}</tbody></table>
            </div>
          </div>"""
    else:
        fail_block = (f'<p class="mt-8 text-xs text-slate-500">No failures. '
                      f'<a class="{ui.LINK}" href="/campaigns/export?id={campaign_id}" download>'
                      f'Download results CSV</a>.</p>')

    parent_id = c.get("retry_of")
    lineage = (f'<a class="{ui.LINK} text-xs" href="/campaigns/detail?id={esc(parent_id)}">'
               f'&larr; retry {c.get("retry_round") or 1} of the campaign that failed these leads</a>'
               if parent_id else "")

    return f"""
    <div id="campaign-detail">
    {msg}
    <section class="rounded-xl border border-slate-700 bg-slate-800/60 p-5">
      <div class="mb-7 flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 class="font-semibold text-2xl font-semibold leading-tight text-slate-100">{esc(c.get("name") or "—")}</h2>
          <div class="mt-1.5">{status_pill(c.get("status", "?"))}</div>
          <div class="mt-1.5">{lineage}</div>
        </div>
        <a href="/campaigns" class="{ui.BTN_GHOST}">&larr; All campaigns</a>
      </div>

      <div class="grid gap-x-10 gap-y-7 lg:grid-cols-3">
        <div>
          <p class="{ui.LABEL}">Delivery</p>
          {ui.stat("Template", esc(c.get("template_name") or ""))}
          {ui.stat("WATI broadcast", esc(c.get("broadcast_name") or ""))}
          {ui.stat("Recipients", f'{c.get("total", 0):,}')}
          {ui.stat("Sent", f'{c.get("sent", 0):,}', "text-emerald-400")}
          {ui.stat("Refused at send", f'{c.get("failed", 0):,}', "text-rose-400" if c.get("failed") else "")}
          {ui.stat("Rate limit", f'{c.get("throttle", 60)}/min')}
          {ui.stat("Mode", "dry run" if c.get("dry_run") else "live", "text-amber-400" if c.get("dry_run") else "")}
        </div>
        <div>
          <p class="{ui.LABEL}">Timing</p>
          {ui.stat("Created", esc(ist(c.get("created_at"))))}
          {ui.stat("Scheduled", esc(ist(c.get("scheduled_at"))))}
          {ui.stat("Started", esc(ist(c.get("started_at"))))}
          {ui.stat("Finished", esc(ist(c.get("finished_at"))))}
          {ui.stat("Batch", f'{c.get("batch_index")} of {c.get("batch_count")}' if c.get("batch_count") else "—")}
        </div>
        {funnel_block(c)}
      </div>

      <div class="mt-8 grid gap-x-10 gap-y-7 lg:grid-cols-3">
        <div class="space-y-2 lg:col-span-2">{replies}{blocked}</div>
        <div>
          <p class="{ui.LABEL}">History</p>
          <ul class="max-h-64 overflow-auto text-xs">{log}</ul>
        </div>
      </div>
      {retry}
      {fail_block}
    </section>
    </div>
    """


# --------------------------------------------------------------------------- #
# Delivery — what the webhook tells us happened after the send
# --------------------------------------------------------------------------- #
# The four numbers the dashboard is built around, in the order a message moves
# through them. Colour carries the meaning and nothing else does: emerald for
# arrived, sky for seen, violet for answered, rose for refused — the same
# vocabulary the rest of the app already uses.
FUNNEL_STEPS = [
    ("delivered", "Delivered", "emerald", "reached the handset"),
    ("read", "Read", "sky", "the contact opened it"),
    ("replied", "Replied", "violet", "the contact wrote back"),
    ("undelivered", "Blocked", "rose", "Meta refused it after WATI accepted it"),
]

_FUNNEL_TEXT = {"emerald": "text-emerald-400", "sky": "text-sky-300",
                "violet": "text-violet-300", "rose": "text-rose-400"}
_FUNNEL_BAR = {"emerald": "bg-emerald-500", "sky": "bg-sky-500",
               "violet": "bg-violet-500", "rose": "bg-rose-500"}


def delivery_cell(c):
    """
    The Delivery column of one dashboard row — four counts, or an honest
    explanation of why there are none.

    "Waiting" rather than "0 delivered" while nothing has come back yet: a
    campaign sent thirty seconds ago has no receipts because Meta has not sent
    them, not because it failed, and those two states must not look alike.
    """
    f = wati_webhook.funnel(c)
    if not f["sent"]:
        return '<span class="text-xs text-slate-600">—</span>'
    if not f["tracked"]:
        hooked = wati_webhook.health()
        if not hooked["total"]:
            return (f'<a href="/webhooks" class="text-xs text-amber-400 hover:underline" '
                    f'title="No webhook events have ever arrived">not tracked yet</a>')
        return '<span class="text-xs text-slate-500">waiting for receipts</span>'

    parts = []
    for key, label, tone, _why in FUNNEL_STEPS:
        if not f[key]:
            continue
        parts.append(f'<span class="{_FUNNEL_TEXT[tone]}" title="{label} — {f["pct"][key]}% of sent">'
                     f'{f[key]:,} <span class="text-slate-500">{label.lower()}</span></span>')
    if not parts:
        return '<span class="text-xs text-slate-500">waiting for receipts</span>'
    return ('<div class="flex flex-wrap gap-x-2.5 gap-y-0.5 text-xs font-mono">'
            + "".join(parts) + "</div>")


def funnel_block(c):
    """The delivery funnel on the detail page: one bar per step, measured
    against the number of messages that actually went out."""
    f = wati_webhook.funnel(c)
    if not f["sent"]:
        return ""

    rows = []
    for key, label, tone, why in FUNNEL_STEPS:
        pct = f["pct"][key]
        rows.append(f"""
        <div class="mb-3">
          <div class="mb-1 flex items-baseline justify-between gap-3 text-xs">
            <span class="{_FUNNEL_TEXT[tone]} font-semibold">{label}</span>
            <span class="font-mono text-slate-400">{f[key]:,}
              <span class="text-slate-600">· {pct}% of sent</span></span>
          </div>
          <div class="h-2 w-full overflow-hidden rounded-full bg-slate-700">
            <div class="h-full {_FUNNEL_BAR[tone]}" style="width:{min(100, pct)}%"></div>
          </div>
          <p class="mt-0.5 text-xs text-slate-600">{why}</p>
        </div>""")

    if not f["tracked"]:
        health = wati_webhook.health()
        caveat = (f'<p class="mt-2 text-xs text-amber-400">No delivery events have arrived for this '
                  f'campaign yet. '
                  + ("The webhook has never been called — " if not health["total"] else "")
                  + f'<a class="{ui.LINK}" href="/webhooks">check the webhook setup</a>.</p>')
    elif f["waiting"]:
        caveat = (f'<p class="mt-2 text-xs text-slate-500">{f["waiting"]:,} still with no receipt either '
                  f'way. Meta reports delivery within minutes for a reachable number; a receipt that '
                  f'never comes usually means the message sat undelivered.</p>')
    else:
        caveat = ""

    return f"""
    <div>
      <p class="{ui.LABEL}">After the send
        <span class="text-slate-600">· from the WATI webhook</span></p>
      {"".join(rows)}
      {caveat}
    </div>"""


def blocked_block(c):
    """Why Meta refused what it refused, grouped by reason, with the fix."""
    groups = wati_webhook.failure_breakdown(c)
    if not groups:
        return ""

    rows = "".join(f"""
      <tr class="border-b border-dashed border-slate-700 align-top last:border-0">
        <td class="py-2 pl-3 pr-2">
          <div class="text-xs font-semibold text-rose-300">{esc(g["label"])}</div>
          <div class="mt-0.5 font-mono text-xs text-slate-500">{esc(g["code_summary"])}</div>
        </td>
        <td class="px-2 py-2 text-right font-mono text-sm text-slate-200">{g["count"]:,}</td>
        <td class="py-2 pl-2 pr-3 text-xs text-slate-400">{esc(g["advice"])}</td>
      </tr>""" for g in groups)

    return f"""
    <div class="mt-8">
      <p class="{ui.LABEL}">Refused by Meta — {sum(g["count"] for g in groups):,}</p>
      <div class="overflow-hidden rounded-lg border border-slate-700 bg-slate-900">
        <table class="w-full"><tbody>{rows}</tbody></table>
      </div>
      <p class="mt-1 text-xs text-slate-600">
        These are messages WATI accepted and Meta then refused, which is why they still
        count as sent. The full list of numbers is in the results CSV.
      </p>
    </div>"""


def retry_block(c):
    """
    The second chance: who failed, who is worth messaging again, and when.

    Written to be readable by whoever has to defend the send. The count that
    matters is not "how many failed" but "how many of those we are willing to
    message again" — the gap between the two is opt-outs and dead numbers, and
    naming that gap explicitly is the difference between a retry an operator
    trusts and one they turn off.
    """
    s = campaign_retry.summary(c)
    cid = c["campaign_id"]
    state = s["state"]

    if not s["failed_total"] and state in (None, "none", "capped"):
        return ""                                 # nothing failed; say nothing

    def btn(action, label, cls=ui.BTN_XS, confirm=None):
        extra = f' hx-confirm="{confirm}"' if confirm else ""
        return (f'<button class="{cls}" hx-post="/campaigns/{action}?id={cid}"'
                f' hx-target="#campaign-detail" hx-swap="outerHTML"{extra}>{label}</button>')

    actions, tone, headline = [], "text-slate-400", ""

    if state == "done" and s["child_id"]:
        tone = "text-emerald-300"
        headline = (f'{esc(s["note"]) if s["note"] else "The failed leads have been sent again."} '
                    f'<a class="{ui.LINK}" href="/campaigns/detail?id={esc(s["child_id"])}">'
                    f'Open the retry campaign</a>.')
    elif state == "building":
        tone = "text-amber-300"
        headline = "The retry is being built right now."
    elif state == "armed" and s["due_at"]:
        tone = "text-amber-300"
        headline = (f'{s["eligible"]:,} of {s["failed_total"]:,} failed leads are due for a '
                    f'second send on <strong class="text-slate-100">{esc(ist(s["due_at"]))}</strong>. '
                    f'The exact list is read then, not now — Meta\'s refusals keep arriving '
                    f'for hours after a send, and waiting is what makes the list complete.')
        actions = [btn("retry-now", "Retry now", ui.BTN_XS_GO,
                       confirm=f"Send the campaign again to {s['eligible']:,} failed leads now, "
                               f"without waiting? Receipts still arriving would be missed."),
                   btn("retry-off", "Don\u2019t retry")]
    elif state == "none":
        headline = esc(s["note"] or "Nothing here is worth retrying.")
    elif state == "off":
        headline = "Automatic retry is off for this campaign."
    elif state == "capped":
        headline = (f'This is retry {s["round"]}, and the automatic retry limit stops here — '
                    f'a lead who has already been messaged twice and failed twice is not going '
                    f'to be reached by a third attempt. Sending again is still yours to trigger.')
    elif c.get("dry_run"):
        headline = "A dry run sent nothing, so there is nothing to retry."
    elif c.get("status") not in store.TERMINAL_STATUSES:
        headline = "The retry is armed when the campaign finishes."
    else:
        # Terminal, failures present, never armed — a campaign that finished
        # before retries existed, or one armed while the feature was off.
        headline = (f'{s["eligible"]:,} of {s["failed_total"]:,} failed leads could be '
                    f'sent again. No automatic retry is scheduled for this campaign.')

    if s["can_retry_now"] and not actions:
        actions = [btn("retry-now", f'Retry {s["eligible"]:,} failed leads now', ui.BTN_XS_GO,
                       confirm=f"Send this campaign again to {s['eligible']:,} leads whose first "
                               f"message failed?")]

    skipped = "".join(
        f'<li class="flex items-baseline justify-between gap-4 border-b border-dashed '
        f'border-slate-700 py-1 last:border-0">'
        f'<span class="text-xs text-slate-400">{esc(x["reason"])}</span>'
        f'<span class="font-mono text-xs text-slate-500">{x["count"]:,}</span></li>'
        for x in s["skipped"])
    if s["duplicates"]:
        skipped += (f'<li class="flex items-baseline justify-between gap-4 py-1">'
                    f'<span class="text-xs text-slate-400">Same number already in the retry list</span>'
                    f'<span class="font-mono text-xs text-slate-500">{s["duplicates"]:,}</span></li>')
    skipped_block = (f'<div class="mt-3"><p class="{ui.LABEL}">Not retried</p>'
                     f'<ul class="rounded-lg border border-slate-700 bg-slate-900 px-3">{skipped}</ul>'
                     f'<p class="mt-1 text-xs text-slate-600">Opt-outs, numbers that are not on '
                     f'WhatsApp, and template faults are never resent — the first is a policy '
                     f'violation, the second fails identically every time, and the third needs a '
                     f'fixed template rather than another attempt.</p></div>') if skipped else ""

    return f"""
    <div class="mt-8">
      <p class="{ui.LABEL}">Retry of failed leads</p>
      <div class="rounded-lg border border-slate-700 bg-slate-900 p-3">
        <div class="flex flex-wrap items-start justify-between gap-3">
          <p class="max-w-2xl text-sm {tone}">{headline}</p>
          <div class="flex flex-wrap gap-1.5">{"".join(actions)}</div>
        </div>
        {skipped_block}
      </div>
    </div>"""


def replies_block(c):
    """Who wrote back, and what they said — the reason to run a campaign."""
    rows = wati_webhook.campaign_replies(c)
    if not rows:
        return ""
    body = "".join(f"""
      <tr class="border-b border-dashed border-slate-700 align-top last:border-0">
        <td class="py-1.5 pl-3 pr-2 font-mono text-xs text-slate-300">{esc(r.get("phone"))}</td>
        <td class="px-2 py-1.5 text-xs text-slate-400">{esc(r.get("name") or "")}</td>
        <td class="px-2 py-1.5 text-xs text-slate-100">{esc(r.get("reply_text") or "(no text — media or button)")}</td>
        <td class="py-1.5 pl-2 pr-3 text-right font-mono text-xs text-slate-500">
          {esc(ist(_moment(r.get("last_reply_at") or r.get("replied_at")), "%d %b, %H:%M"))}</td>
      </tr>""" for r in rows)
    return f"""
    <div class="mt-8">
      <p class="{ui.LABEL}">Replies — {len(rows):,}</p>
      <div class="max-h-96 overflow-auto rounded-lg border border-violet-800/50 bg-slate-900">
        <table class="w-full"><tbody>{body}</tbody></table>
      </div>
    </div>"""


def _moment(value):
    """Recipient timestamps are stored as ISO strings (they come back from the
    webhook, not from a Mongo date), so they need parsing before ist()."""
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            return None
    return None


# --------------------------------------------------------------------------- #
# The webhook page — setup, health, raw feed
# --------------------------------------------------------------------------- #
FEED_REFRESH = 'hx-get="/webhooks/feed" hx-trigger="every 5s" hx-swap="outerHTML"'


def webhook_setup(public_base=""):
    """
    The page that gets the webhook working and then proves it is.

    Deliberately shows the URL with the token already in it. The operator has
    to paste that exact string into WATI, and a page that made them assemble it
    from three fields would be a page that produces a typo — which presents as
    silence, the hardest failure to diagnose. It is a secret in the same sense
    the API token in `.env` is: anyone who can reach this page can already send
    campaigns from this account.
    """
    health = wati_webhook.health()
    token = wati_webhook.token()

    if not token:
        state = ui.banner(
            "<b>WATI_WEBHOOK_TOKEN is not set.</b> Until it is, this endpoint refuses every "
            "call — including WATI's — because a webhook that accepts anything is one anyone "
            "can use to write fake delivery numbers into your dashboard. Add a long random "
            "value to <code>.env</code> and restart the app.", "signal")
    elif not health["total"]:
        state = ui.banner(
            "Endpoint is armed, but nothing has arrived yet. Paste the URL below into WATI, "
            "then send a test message — the first receipt usually lands within seconds.", "wait")
    else:
        last = ist(health["last_at"]) if health["last_at"] else "—"
        state = ui.banner(
            f"<b>Working.</b> {health['total']:,} events received · "
            f"{health['matched']:,} matched to a campaign · last one {esc(last)}.", "go")

    if public_base:
        url = f"{public_base}/webhooks/wati?token={token or '<WATI_WEBHOOK_TOKEN>'}"
        url_note = ui.note("Copy this exactly, including everything after the <code>?</code>.")
    else:
        url = f"https://&lt;your-public-domain&gt;/webhooks/wati?token={token or '<WATI_WEBHOOK_TOKEN>'}"
        url_note = ui.note(
            "This app is being reached directly rather than through the reverse proxy, so it "
            "cannot tell what its public address is. Replace the placeholder with the domain "
            "WATI can reach, or set <code>PUBLIC_BASE_URL</code> in the environment.", "wait")

    events = "".join(
        f'<tr class="border-b border-dashed border-slate-700 last:border-0">'
        f'<td class="py-1.5 pl-3 pr-2 font-mono text-xs text-sky-300">{esc(name)}</td>'
        f'<td class="py-1.5 pl-2 pr-3 text-xs text-slate-400">{esc(why)}</td></tr>'
        for name, why in wati_webhook.WANTED_EVENTS)

    counts = health["by_status"] or {}
    tally = "".join(
        ui.stat(esc(k), f'{v:,}') for k, v in
        sorted(counts.items(), key=lambda kv: -kv[1])) or ui.stat("events", "0")

    setup = f"""
    <div class="grid gap-x-10 gap-y-7 lg:grid-cols-3">
      <div class="lg:col-span-2">
        <p class="{ui.LABEL}">1 · The URL to paste into WATI</p>
        <div class="{ui.WELL} break-all font-mono text-xs text-emerald-300">{url}</div>
        {url_note}

        <p class="{ui.LABEL} mt-6">2 · Where to paste it</p>
        <p class="text-sm text-slate-300">
          In WATI: <b>Settings &rarr; Webhooks &rarr; Add Webhook</b>. Give it any name,
          paste the URL, and tick the events below. WATI posts JSON; nothing else needs configuring.
        </p>

        <p class="{ui.LABEL} mt-6">3 · Which events to tick</p>
        <div class="overflow-hidden rounded-lg border border-slate-700 bg-slate-900">
          <table class="w-full"><tbody>{events}</tbody></table>
        </div>
        {ui.note("Anything else WATI sends is stored too — this list is what the dashboard's "
                 "numbers are built from.")}
      </div>
      <div>
        <p class="{ui.LABEL}">Received so far</p>
        {tally}
        {ui.stat("unmatched", f'{health["unmatched"]:,}',
                 "text-amber-400" if health["unmatched"] else "")}
        {ui.stat("store", esc(health["backend"]))}
        {ui.note("Unmatched events are real events about numbers no campaign here messaged — "
                 "chatbot traffic, manual sends, replies to something else. A pile of them next to "
                 "campaigns with no delivery numbers is the sign that matching is broken.")}
      </div>
    </div>"""

    return f"""
    <div class="space-y-6">
      {state}
      {ui.section("01", "Set it up", "", setup, tone="sky")}
      {ui.section("02", "Live feed",
                  '<span class="text-xs font-normal text-slate-500">every event as it arrives</span>',
                  webhook_feed(), tone="violet")}
    </div>"""


def webhook_feed(unmatched_only=False):
    """The raw event feed — the fastest way to tell a working hook from a
    mistyped one, and the only place a payload WATI changed shape on is visible."""
    rows = wati_webhook.recent_events(limit=60, unmatched_only=unmatched_only)

    toggle = (
        f'<a class="{ui.BTN_XS}" href="/webhooks">All events</a>'
        if unmatched_only else
        f'<button class="{ui.BTN_XS}" hx-get="/webhooks/feed?filter=unmatched" '
        f'hx-target="#webhook-feed" hx-swap="outerHTML">Unmatched only</button>')

    if not rows:
        return f"""
        <div id="webhook-feed" {FEED_REFRESH}>
          <p class="py-10 text-center text-sm text-slate-500">
            Nothing yet. This list fills itself the moment WATI calls.</p>
        </div>"""

    tone = {"delivered": "text-emerald-400", "read": "text-sky-300", "replied": "text-violet-300",
            "received": "text-violet-300", "failed": "text-rose-400", "sent": "text-slate-300"}

    body = "".join(f"""
      <tr class="border-t border-slate-700 align-top">
        <td class="px-3 py-1.5 font-mono text-xs text-slate-500">
          {esc(ist(e.get("received_at"), "%d %b, %H:%M:%S"))}</td>
        <td class="px-3 py-1.5 text-xs {tone.get(e.get("status"), "text-slate-400")}">
          {esc(e.get("status"))}
          <div class="font-mono text-xs text-slate-600">{esc(e.get("event_type"))}</div></td>
        <td class="px-3 py-1.5 font-mono text-xs text-slate-300">{esc(e.get("phone") or "—")}</td>
        <td class="px-3 py-1.5 text-xs">
          {(f'<a class="{ui.LINK}" href="/campaigns/detail?id={esc(e["campaign_id"])}">'
            f'{esc(e.get("campaign_name") or e["campaign_id"][:8])}</a>'
            f'<div class="text-xs text-slate-600">matched by {esc(e.get("matched_by") or "—")}</div>')
           if e.get("campaign_id") else '<span class="text-amber-500">unmatched</span>'}</td>
        <td class="px-3 py-1.5 text-xs text-slate-400">
          {esc((e.get("text") or "")[:120])}
          {(f'<div class="text-rose-400">{esc(e.get("failed_label"))} '
            f'<span class="font-mono text-slate-600">{esc(e.get("failed_code"))}</span></div>')
           if e.get("failed_code") or e.get("failed_label") else ""}</td>
      </tr>""" for e in rows)

    return f"""
    <div id="webhook-feed" {FEED_REFRESH}>
      <div class="mb-2 flex items-center justify-between gap-3">
        <p class="text-xs text-slate-500">Newest first · refreshes every 5s
          {" · unmatched only" if unmatched_only else ""}</p>
        {toggle}
      </div>
      <div class="max-h-[32rem] overflow-auto rounded-lg border border-slate-700 bg-slate-900">
        <table class="w-full text-sm">
          <thead class="sticky top-0 bg-slate-900 text-xs uppercase tracking-wide text-slate-500">
            <tr>
              <th class="px-3 py-2 text-left font-medium">Arrived</th>
              <th class="px-3 py-2 text-left font-medium">Event</th>
              <th class="px-3 py-2 text-left font-medium">Number</th>
              <th class="px-3 py-2 text-left font-medium">Campaign</th>
              <th class="px-3 py-2 text-left font-medium">Detail</th>
            </tr>
          </thead>
          <tbody>{body}</tbody>
        </table>
      </div>
    </div>"""

#!/usr/bin/env python3
"""
ui.py

The design system: one place that owns what this app looks like.

`app.py` renders the cleaner and `campaign_ui.py` renders the sender, and
before this module existed they each carried their own copy of every Tailwind
class string — which is how a "Load contacts" button and a "Create campaign"
button end up subtly different. Everything visual now comes from here, so
changing the look is one edit and not a search across two files.

The look is the original dark slate theme: `bg-slate-900` ground, translucent
`slate-800` cards with rounded corners and `slate-700` hairlines, and the step
accents that came with it — **sky** for the working steps, **emerald** for a
finished result, **violet** for the send step, **amber** for caution and
**rose** for failure. Type is Tailwind's default system sans, with `font-mono`
for phone numbers and other data.

Tailwind and htmx both come from CDNs and Tailwind is configured inline, so
there is no build step: `python3 app.py` and open the page.
"""

# --------------------------------------------------------------------------- #
# document head
# --------------------------------------------------------------------------- #
# htmx belongs here and nowhere else. Every interactive control in this app is
# an hx-get/hx-post, so if this tag goes missing the whole UI silently stops
# responding to clicks while still rendering perfectly — which is exactly what
# happened once. Keep it in the shared head so no page can be built without it.
HEAD = """
<script src="https://cdn.tailwindcss.com"></script>
<script src="https://unpkg.com/htmx.org@1.9.12"></script>
<script>tailwind.config = { darkMode: 'class' }</script>
<style>
  /* Phone numbers and counts line up in columns. */
  .font-mono { font-variant-numeric: tabular-nums; }
</style>
"""

# --------------------------------------------------------------------------- #
# component vocabulary
# --------------------------------------------------------------------------- #

#: Field labels and small meta text.
LABEL = "mb-1 block text-xs text-slate-400"

#: Text inputs and selects.
FIELD = "w-full rounded-lg border border-slate-600 bg-slate-900 px-3 py-2 text-sm text-slate-100"
FIELD_MONO = FIELD + " font-mono"

#: Primary — the safe, expected action (load, apply, add).
BTN = "rounded-lg bg-sky-500 px-4 py-2 text-sm font-semibold text-white hover:bg-sky-400"

#: Secondary — reversible, quieter.
BTN_GHOST = "rounded-lg bg-slate-700 px-4 py-2 text-sm font-semibold text-slate-100 hover:bg-slate-600"

#: Outlined secondary, for actions sitting next to a primary one.
BTN_OUTLINE = "rounded-lg border border-slate-600 px-3 py-2 text-xs text-slate-300 hover:bg-slate-700"

#: Generate — the end of the cleaning flow.
BTN_GO = "rounded-lg bg-emerald-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-emerald-400"

#: Send — the irreversible one.
BTN_SIGNAL = "rounded-lg bg-violet-500 px-5 py-2.5 text-sm font-semibold text-white hover:bg-violet-400"

#: Row-level actions in the campaigns table.
BTN_XS = "rounded bg-slate-700 px-2.5 py-1 text-xs text-slate-200 hover:bg-slate-600"
BTN_XS_GO = "rounded bg-emerald-600 px-2.5 py-1 text-xs font-semibold text-white hover:bg-emerald-500"
BTN_XS_SIGNAL = "rounded border border-rose-800 px-2.5 py-1 text-xs text-rose-300 hover:bg-rose-500/10"

#: Panels inside a section.
PANEL = "rounded-lg border border-slate-700 bg-slate-900/60 p-3"
WELL = "rounded-lg border border-slate-700 bg-slate-900/50 p-3"

#: Links inside prose.
LINK = "text-sky-400 hover:underline"

#: Step accent colours, by section tone.
_TONE_BG = {"sky": "bg-sky-500", "emerald": "bg-emerald-500", "violet": "bg-violet-500"}
_TONE_BORDER = {"sky": "border-slate-700", "emerald": "border-emerald-700/50",
                "violet": "border-violet-700/50"}


def section(number, title, meta="", body="", rise=None, anchor=None, tone="sky"):
    """
    One numbered step, as a card with a coloured numeral badge in its heading.

    `number` may be given as "01" — the badge shows "1", because the padded
    form is only there to keep the call sites sorting readably.
    """
    del rise                      # entrance animation is not part of this theme
    ident = f' id="{anchor}"' if anchor else ""
    badge = (number or "").lstrip("0") or number
    meta_html = f'<span class="ml-1 flex flex-wrap items-center gap-2 font-normal">{meta}</span>' if meta else ""
    return f"""
    <section{ident} class="rounded-xl border {_TONE_BORDER.get(tone, 'border-slate-700')} bg-slate-800/60 p-5">
      <h2 class="mb-4 flex flex-wrap items-center gap-2 text-sm font-semibold tracking-wide">
        <span class="grid h-6 w-6 shrink-0 place-items-center rounded-full {_TONE_BG.get(tone, 'bg-sky-500')}
                     text-xs font-bold text-white">{badge}</span>
        {title}
        {meta_html}
      </h2>
      {body}
    </section>
    """


def tag(text, tone="quiet"):
    """A small rounded chip. Tone carries meaning, never decoration."""
    tones = {
        "quiet":  "border-slate-600 bg-slate-900 text-slate-300",
        "go":     "border-emerald-700 bg-emerald-500/10 text-emerald-300",
        "wait":   "border-amber-700 bg-amber-500/10 text-amber-300",
        "signal": "border-violet-700 bg-violet-500/10 text-violet-300",
        "ink":    "border-slate-600 bg-slate-900 text-slate-300",
    }
    return (f'<span class="rounded-full border px-2.5 py-0.5 text-xs '
            f'{tones.get(tone, tones["quiet"])}">{text}</span>')


def note(text, tone="quiet"):
    """A line of explanation under a control."""
    tones = {"quiet": "text-slate-500", "signal": "text-rose-400",
             "go": "text-emerald-400", "wait": "text-amber-400"}
    return f'<p class="mt-1 text-xs {tones.get(tone, tones["quiet"])}">{text}</p>'


def banner(text, tone="go"):
    """Result of an action: created, cancelled, refused."""
    tones = {
        "go":     "border-emerald-800 bg-emerald-500/10 text-emerald-300",
        "signal": "border-rose-800 bg-rose-500/10 text-rose-300",
        "wait":   "border-amber-800 bg-amber-500/10 text-amber-300",
    }
    return f'<p class="rounded-lg border px-3 py-2 text-sm {tones.get(tone, tones["go"])}">{text}</p>'


def stat(label, value, tone=""):
    """One line of a read-only figures column."""
    return (f'<div class="flex items-baseline justify-between gap-4 border-b border-dashed '
            f'border-slate-700 py-1">'
            f'<span class="shrink-0 text-slate-400">{label}</span>'
            f'<span class="min-w-0 break-all text-right font-bold {tone}">{value}</span></div>')


def masthead(title, subtitle, nav_html, status_html):
    """The page header."""
    return f"""
    <header class="border-b border-slate-700 bg-gradient-to-b from-slate-800 to-slate-900 px-6 py-5">
      <div class="mx-auto w-full max-w-[1600px]">
        <div class="flex flex-wrap items-center gap-4">
          <div>
            <h1 class="text-lg font-semibold">{title}</h1>
            <p class="mt-1 text-sm text-slate-400">{subtitle}</p>
          </div>
          <nav class="ml-auto flex items-center gap-2 text-sm">{nav_html}</nav>
        </div>
        {status_html}
      </div>
    </header>
    """


def nav_link(label, href=None, current=False):
    if current:
        return f'<span class="rounded-lg bg-slate-700 px-3 py-1.5 font-semibold">{label}</span>'
    return (f'<a href="{href}" class="rounded-lg border border-slate-600 px-3 py-1.5 '
            f'text-slate-300 hover:bg-slate-700">{label}</a>')


def page(title, head_extra, body):
    """The document shell both pages share."""
    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
{HEAD}{head_extra}
</head>
<body class="min-h-screen bg-slate-900 text-slate-100">
{body}
</body>
</html>"""

# wati_bigin

The **Bigin → WATI CSV cleaner** — moved here out of the `wati_cleanup`
project, which now keeps only the Node/Express + MongoDB side.

## Files

| File | What it is |
| --- | --- |
| `app.py` | htmx web UI. Loads contacts from the Bigin mirror (or a CSV), cleans them, and sends or schedules WATI campaigns. Listens on `127.0.0.1:8000`. |
| `wati_cleanup.py` | The cleaning logic itself (`clean_phone`, `suggest_column`, the WATI header). Imported by `app.py`. |
| `bigin_sync.py` | **Write side** — mirrors the Bigin **Contacts** module into MongoDB. Full pull the first time, delta after. |
| `bigin_store.py` | **Read side** — turns those Mongo documents back into the `(headers, rows)` shape the cleaner works on. |
| `wati_client.py` | **WATI API** — approved templates, and sending one template message. Stdlib `urllib`, no deps. |
| `campaign_store.py` | Campaign persistence. MongoDB, falling back to `data/campaigns.json`. |
| `campaign_runner.py` | The scheduler + sender thread: rate-limited, pausable, resumable. |
| `campaign_retry.py` | **The second chance** — a week after a campaign finishes, its failed leads become a fresh campaign. Opt-outs and dead numbers are never resent. |
| `campaign_ui.py` | HTML for step 5 (send/schedule), the campaigns dashboard, and the delivery-webhook page. |
| `wati_webhook.py` | **Delivery receipts** — takes WATI's webhook and turns it into delivered / read / replied / blocked-by-Meta. |
| `session_store.py` | **Where a browser's wizard state lives** between requests — MongoDB, or a dict when there is no Mongo. This is what lets the app run on a host where no process survives a request. |
| `cron_jobs.py` | The background work (send tick, retry sweep, Bigin sync) as endpoints a scheduler can call. Used by Vercel Cron; harmless everywhere else. |
| `vercel.json`, `requirements.txt`, `api/`, `public/` | The Vercel deployment — see [Hosting on Vercel](#hosting-on-vercel). Ignored when you run `app.py` yourself. |
| `vercel_boot.py` | The few adjustments a serverless function needs that a server does not (line-buffered logs, a writable temp dir). Imported by every entrypoint under `api/`. |
| `ui.py` | **The design system** — palette, fonts, and every shared button/field/section. Change the look here, not in the two files above. |
| `sample_bigin.csv` | Example Bigin export to test an upload against. |
| `wati_cleanup.html` | Legacy standalone single-file version of the UI. Open it in a browser; needs no server. |
| `deploy/wati.service` | systemd unit that runs `app.py`. |
| `deploy/wati-bigin-sync.{service,timer}` | systemd timer that runs `bigin_sync.py` every 3 hours. |
| `.env` | Zoho + Mongo + WATI credentials. Gitignored — never commit it. |
| `server.js`, `package.json` | Pre-existing Express stub in this folder (port 8080), unrelated to the cleaner. Excluded from the Vercel deployment by `.vercelignore`. |

## Run

```bash
python3 app.py            # http://127.0.0.1:8000
HOST=0.0.0.0 PORT=8000 python3 app.py
```

State is per-browser (cookie session) and kept in memory, so run it as a
**single process**. (Campaigns themselves are in the database, not in the
session, so they survive restarts — and if you do end up running two
processes, `campaign_store.claim()` makes sure only one of them sends any
given campaign.)

The UI is five steps:

1. **Load contacts** — one click pulls all mirrored Bigin contacts. No export,
   no upload. A badge shows how many are mirrored and how long ago the cron job
   last ran; *Refresh status* re-checks without reloading. CSV upload is still
   there, tucked under *Or upload a CSV instead*, for one-off lists from
   elsewhere.
2. **Filter** — stack as many keep/remove filters as you like on any Bigin
   field (`Lead_Source1`, `CA_Status`, `Tag`, `Owner`, `Created_Time`, …).
3. **Map & generate** — `Full_Name` and `Phone` are auto-selected; set the
   country code, batch size, and any numbers to suppress.
4. **Download** — `wati_batch_N.csv` files in WATI's exact format, plus
   `skipped.csv` explaining every dropped row.
5. **Send or schedule** — pick an approved WATI template, map its parameters,
   then send or schedule it **batch by batch**. No CSV round-trip through the
   WATI dashboard. See **Sending campaigns** below.

What happens to those messages afterwards — who received them, who read them,
who wrote back, and who Meta refused — comes back on a webhook. See
**Delivery: who saw it, who replied** below.

### Changing the look

Everything visual lives in `ui.py`: the document head (Tailwind + htmx) and a
small vocabulary of components — `BTN`, `FIELD`, `LABEL`, `section()`, `tag()`,
`stat()` — that `app.py` and `campaign_ui.py` both build from. Retheming is an
edit to that one file.

The theme is the original dark slate: `bg-slate-900` ground, translucent
`slate-800` cards, and the step accents that go with it — **sky** for the
working steps, **emerald** for a finished result, **violet** for the send step,
**amber** for caution, **rose** for failure. Type is Tailwind's default system
sans, with `font-mono` for phone numbers and counts.

> `ui.HEAD` carries the **htmx** script tag. Every control in this app is an
> `hx-get`/`hx-post`, so a page built without it renders perfectly and responds
> to nothing — which is exactly what happened once when the page shells were
> rewritten. It lives in the shared head so no page can be built without it,
> and the browser test below is what catches it if it ever goes missing again.

### Testing the UI

Route-level `curl` checks pass straight through a missing htmx tag, because
they call the endpoints directly and never render a page. Anything that changes
the page shell or a fragment's markup needs a real browser:

```bash
python3 -m venv /tmp/pw && /tmp/pw/bin/pip install playwright
/tmp/pw/bin/playwright install chromium
# then drive the app: load from Bigin -> filter -> generate -> pick a template
# -> change the delivery controls, asserting each swap actually happened.
```

The checks worth keeping are `typeof htmx !== 'undefined'`, that `#workspace`
gains sections after the Bigin click, and that `#batch-preview` changes when a
delivery control changes.

`app.py` itself is stdlib-only, but loading from Bigin needs **pymongo**. If it
is missing the app still starts — the Bigin button simply reports why it is
unavailable and CSV upload keeps working.

## Hosting on Vercel

The app also deploys to Vercel, with no separate branch and no second copy of
the code: `python3 app.py` still runs the whole thing on a laptop or a VM
exactly as before. What follows is what had to change to make one codebase do
both, because two of those changes are things you can trip over later.

### The two things that are genuinely different there

**A function does not outlive its response.** That breaks a five-step wizard,
which is what this app is: the process that serves step 2 is not the one that
served step 1, so the rows uploaded in step 1 are simply gone. Sessions
therefore moved out of the process into `session_store.py` — one gzipped JSON
blob per browser in Mongo, expired by a TTL index, written *before* each
response is flushed rather than after (a write queued after the last byte goes
out is a write the platform is free to never run). Locally, with no
`MONGO_URI`, it is still a plain dict and behaves as it always did.

**There is no scheduler thread.** On a server, `campaign_runner` runs a daemon
thread that walks a recipient list for as long as the list takes. A function
gets a few minutes at most. So on Vercel the same walk is driven from outside:

| Cron | Schedule | What it does |
| --- | --- | --- |
| `/api/cron/send` | every minute | `campaign_runner.tick_once()` — claim what is due and send until the time budget runs out. |
| `/api/cron/retry` | hourly | The retry sweep: failed leads from campaigns whose week is up become fresh campaigns. |
| `/api/cron/bigin` | every 3 hours | `bigin_sync.sync()` — the same run `deploy/bigin_sync_cron.sh` does on a VM. |

A campaign too long for one tick is not restarted by the next one. It stops on
a chunk boundary, goes back to `scheduled` with its cursor intact, and the next
tick resumes from there — the log line reads *"Paused for the next tick at
100/200 — the sending window ran out, not the campaign"*. Nobody is messaged
twice, because it is the per-recipient status and not the cursor that decides
what to send. A tick never returns while a send is still in flight, and a
worker killed outright leaves a claim that any later tick may take over only
once `WATI_CLAIM_LEASE_SECONDS` (default 900) has passed. That lease is the
only thing standing between a crashed send and a duplicated one, so do not
shorten it below the send function's `maxDuration`.

### What it needs

* **A MongoDB you can reach from Vercel** (`MONGO_URI`) — Atlas with network
  access open to Vercel's IPs, or `0.0.0.0/0` plus a strong password. Not
  optional here: it holds the sessions as well as the campaigns, and the local
  JSON fallback on a serverless host is a per-instance file in `/tmp` that
  vanishes with the instance.
* **A Vercel Pro plan**, for one reason only: Hobby crons run at most once a
  day, so `* * * * *` would not be honoured. On Hobby, either point an external
  scheduler (cron-job.org, GitHub Actions, an existing box) at
  `https://<your-app>/api/cron/send?key=$CRON_SECRET` every minute, or keep the
  sending on a VM and put only the UI here.
* **`CRON_SECRET`.** These endpoints start real sends. Without it they refuse
  to run at all, which is deliberate — an open one is an open "message
  everyone in the database" button. Vercel Cron presents it as
  `Authorization: Bearer`; the `?key=` form exists for schedulers that cannot
  set headers, and puts the secret in request logs, so prefer the header.

### Deploying

```bash
npm i -g vercel
vercel link                       # once, in this folder

# Copy the .env values in. Do it for Production and Preview both, or a preview
# deployment will come up with no database and no credentials.
for k in ZOHO_CLIENT_ID ZOHO_CLIENT_SECRET ZOHO_REFRESH_TOKEN ZOHO_REGION \
         ZOHO_PRODUCT MONGO_URI WATI_TOKEN WATI_API_URL WATI_WEBHOOK_TOKEN \
         WATI_API_ENDPOINT WATI_API_TOKEN; do
  vercel env add "$k" production
done
vercel env add CRON_SECRET production     # something long and random
vercel env add PUBLIC_BASE_URL production # https://<your-app>.vercel.app

vercel --prod
```

Crons only run on **production** deployments, so a preview URL will serve the
UI but send nothing — which is exactly what you want from a preview, and worth
remembering when a scheduled campaign on one does not go out.

Deploying from the GitHub remote instead of the CLI works the same way; set the
same variables in the project's settings.

### Why `vercel.json` looks the way it does

Four of its settings are not decoration — each one is a way this deploys
wrong if it is missing.

**`framework: null`.** The Vercel project was created for the old Express stub
and its settings still say `express`, which makes the build hunt for a Node
entrypoint and die on `Cannot read properties of undefined (reading 'fsPath')`.
The vercel.json declaration overrides the project setting, so the repo is
self-describing and nobody has to remember a dashboard toggle. Setting the
preset to *Other* in the project's settings is worth doing anyway.

**`outputDirectory: "public"`.** Everything outside `api/` is otherwise copied
into the static half of the deployment and *served* — `https://<your-app>/app.py`
would hand back the application source. Naming an output directory confines
the static half to `public/` (which holds a robots.txt and nothing else). The
modules the functions import are bundled into the functions themselves, not
served, so this costs nothing.

**`functions` with `includeFiles`.** Keeps the root modules — `app.py`,
`campaign_runner.py`, everything they import — inside each function's bundle,
and sets the memory and time limits per endpoint. The send tick gets the
longest (`maxDuration` 300); a UI request needs 60.

**Every entrypoint declares `class handler(...)` literally.** This is the
sharpest edge in the whole deployment. Vercel decides whether a `.py` file
under `api/` is a function by *reading the source* for a `handler` class, not
by importing the module. The natural way to write these files —

```python
from app import Handler as handler        # looks right, silently is not
handler = make_handler("send")            # same problem
```

— is invisible to that check. The file is then treated as a static asset: the
build succeeds, the deployment comes up, and every route 404s while your source
is downloadable. So each entrypoint subclasses instead:

```python
class handler(Handler):
    pass
```

If a new endpoint ever appears to deploy but not exist, this is why.

### The .env file is ignored on Vercel

`.vercelignore` keeps it out of the upload, and `bigin_store._load_env` also
refuses to read one when `VERCEL` is set. Both, deliberately: a `.env` that
slipped into a bundle is a snapshot of whatever was on the machine that built
it, and would quietly outlive the next credential rotation. On the platform,
the platform's environment variables are the only source.

Deploy with `vercel --prod` rather than `vercel deploy --prebuilt` for the same
reason — a local prebuild does not apply `.vercelignore` to the function
bundles.

### After the first deploy

```bash
curl https://<your-app>/health                       # -> ok
curl -H "Authorization: Bearer $CRON_SECRET" \
     https://<your-app>/api/cron/send                # -> {"ok":true,"result":{...}}
```

Then open `/campaigns` and check the storage badge says MongoDB rather than a
JSON file — a JSON badge means `MONGO_URI` did not reach the function, and
every campaign you create will disappear with the instance that served it.

Finally, repoint WATI's webhook at `https://<your-app>/webhooks/wati?token=…`
(see [Setting it up](#setting-it-up)). Set `PUBLIC_BASE_URL` and the webhook
page prints the right URL to paste; without it the page falls back to the
forwarded host, which on a preview deployment is a URL that changes every push.

### Environment variables Vercel adds to the picture

| Variable | Default | What it is for |
| --- | --- | --- |
| `CRON_SECRET` | — | Required. Shared secret for `/api/cron/*`. |
| `PUBLIC_BASE_URL` | forwarded host | The origin printed on the webhook page. |
| `WATI_TICK_BUDGET` | `45` | Seconds one send tick may spend. Keep it well under the function's `maxDuration`. |
| `WATI_CLAIM_LEASE_SECONDS` | `900` | How long a claim is trusted before another tick may take the campaign over. |
| `WATI_DATA_DIR` | `/tmp/wati-data` on Vercel | Where the JSON fallback writes when Mongo is unreachable. |
| `SESSION_TTL_SECONDS` | `7200` | Idle lifetime of a browser session. |

`HOST` and `PORT` mean nothing on Vercel; the platform owns both.

### Limits worth knowing before you rely on it

* **A send starts within a minute, not instantly.** "Send now" marks the
  campaign due and returns; the next tick starts it. Running the walk inside
  the web request would mean a campaign killed mid-send at the request timeout,
  with nothing left to record what happened.
* **Throttling is per tick, not global.** A campaign's rate limiter lives for
  one invocation, so a 60/minute campaign that spans three ticks is 60/minute
  *while each tick runs*. It stays under the limit; it is not exact over the
  hour the way the always-on thread is.
* **A session is capped at 12 MB compressed** — roughly a few hundred thousand
  rows. Over that, the generated CSVs are dropped first (press Generate again
  to rebuild them) and then the save is refused with a message telling you to
  filter the list down. On a VM there is no such ceiling.
* **Every request pays a session read and, when something changed, a write.**
  Unchanged requests skip the write.
* **`deploy/` is for the VM story** — systemd units and the cron wrapper. It is
  excluded from the deployment and is not what runs here.

## Relationship to `wati_cleanup`

`wati_cleanup`'s Express front door still proxies `/` here. Point its
`PY_TARGET` env var at wherever this process listens (default
`http://127.0.0.1:8000`), and install `deploy/wati.service` from this repo —
`wati_cleanup/deploy/wati-node.service` still declares `Requires=wati.service`.


## Bigin -> MongoDB sync

`bigin_sync.py` mirrors the Bigin **Contacts** module (Bigin has no separate
"Leads" module) into MongoDB Atlas, so the cleaner no longer depends on a manual
CSV export.

| | |
| --- | --- |
| Target | `campaigns_leads_wati.bigin_contacts` (from `MONGO_URI`) |
| State | `campaigns_leads_wati.bigin_sync_state` |
| Key | `bigin_id`, unique index — reruns upsert, never duplicate |
| Fields | Every readable field, read live from `/settings/fields` so new Bigin custom fields appear automatically |

```bash
python3 bigin_sync.py             # delta — full pull on the very first run
python3 bigin_sync.py --full      # force a complete re-pull
python3 bigin_sync.py --dry-run   # fetch and report, write nothing
```

The first run pulls everything (~80s for 7k contacts). Later runs send
`If-Modified-Since` with the stored high-water mark, so a quiet 3-hour window
costs about two seconds and one API call. Records deleted in Bigin are removed
from the mirror on each run.

### The 3-hourly schedule (cron)

Installed and running:

```cron
0 */3 * * * /home/sandy/Downloads/Focas/wati_bigin/deploy/bigin_sync_cron.sh
```

Fires at 00:00, 03:00, 06:00 ... 21:00. The entry points at
`deploy/bigin_sync_cron.sh` rather than at Python directly, because cron runs
jobs with almost no environment. The wrapper handles what cron does not:

- resolves the repo root from its own path, so moving the folder cannot silently
  break the job;
- **`flock`** — if a sync is somehow still running when the next tick arrives, the
  new one logs a line and exits instead of running two syncs at once;
- appends to `logs/bigin_sync.log` and **rotates at 5 MB** (keeping one `.log.1`),
  so an unattended job cannot fill the disk;
- redirects stderr into the log, so cron does not try to mail you on every run.

```bash
crontab -l                          # confirm the entry
tail -f logs/bigin_sync.log         # watch runs
./deploy/bigin_sync_cron.sh         # run one now, by hand
```

`logs/` is gitignored.

### Alternative: systemd timer

`deploy/wati-bigin-sync.{service,timer}` do the same job as a systemd timer, if
you ever prefer that to cron. They are **not** installed — do not enable them
alongside the cron entry, or the sync runs twice per cycle. The timer's one real
advantage is `Persistent=true`: a run missed while the machine was off happens
on the next boot, whereas cron simply skips it.

### Requirements

Unlike `app.py`, this script needs **pymongo** (and `dnspython`, which pymongo
pulls in, for the `mongodb+srv://` URI). Both are already installed here.


## Sending campaigns

Step 5 appears under the generated batches. It sends **the exact list shown
above it** — the same cleaned, de-duplicated contacts that go into the CSV
batches — so what you confirm is what goes out.

| | |
| --- | --- |
| Templates | Pulled live from WATI; only `APPROVED` ones are offered |
| Parameters | Each `{{param}}` maps to the contact name, the phone, any Bigin field, or fixed text |
| Delivery | **Batches** (default) or one campaign; batch size and the gap between batches are yours to set |
| Timing | First batch starts now or at a date and time in **IST**; the rest follow at the chosen gap |
| Rate limit | 30–300 messages/minute, to protect your number's quality rating |
| Dry run | Walks the whole list and records results without calling WATI |
| Test send | Up to 5 numbers, using the first contact's real parameter values |

Nothing is sent until you tick the confirmation box, which states the exact
recipient count.

### Batches

A list of any size is normally better sent in staggered batches than in one
push — it protects the number's quality rating, and it gives you somewhere to
stand and look at the results before the rest goes out.

Pick a batch size and a gap, and the composer shows the exact plan before you
commit to it:

```
10 batches of up to 250 · 2,421 contacts · spread over 9 hours
  Batch 1    250   25 Aug 2026, 09:00 IST
  Batch 2    250   25 Aug 2026, 10:00 IST
  …
  Batch 10   171   25 Aug 2026, 18:00 IST
```

Each batch is created as **its own campaign**, so it has its own schedule,
progress, failures and controls. You can pause batch 4, reschedule batch 7, or
let the first two go out and cancel the rest — nothing is entangled. Batches
carry a `group_id`, which is what the dashboard's **Stop all batches** button
uses to cancel every unfinished sibling at once; batches already sent are left
alone, because those messages are gone.

Contacts are split in list order with no overlap: batch 1 gets the first 250,
batch 2 the next 250, and the last batch carries the remainder.

### The campaigns dashboard — `/campaigns`

Live progress for everything sent or scheduled, refreshing every 5 seconds:
status, sent/failed counts, the delivered / read / replied / blocked counts
from the webhook, and per-campaign **Pause**, **Resume**, **Cancel** and **Send
now**. *Details* shows the delivery funnel, who replied and what they said,
Meta's refusals grouped by reason, the history, every failure with WATI's own
error text, and a results CSV covering all recipients.

### What makes it safe to leave running

* **Scheduled campaigns survive a restart.** They live in MongoDB, and the
  scheduler thread starts before the first request — a campaign scheduled
  yesterday for 06:00 today fires on boot if the app was down at 06:00.
* **Nobody is messaged twice.** Every recipient carries its own status and
  progress is saved after each chunk, so a resume — after a pause, a crash, or
  a redeploy — skips whoever already got the message. This is tested: a
  200-recipient run survived a pause, a resume and two `kill -9`s and still
  delivered exactly 200 unique sends.
* **Pause and cancel are quick.** Chunk size is derived from the rate limit so
  a chunk lasts about 20 seconds at any speed; the click takes effect within
  that, not at the end of the list.
* **Batches fire in order, on time.** Verified with four batches a minute
  apart: each started at its scheduled minute and finished before the next
  began, 200 unique sends with no overlap. At most `WATI_MAX_CONCURRENT`
  campaigns send at once, so a short gap queues rather than piles up.

### Configuration

`.env` needs the two WATI keys (both are already set):

```
WATI_API_URL=https://live-mt-server.wati.io/<tenant-id>
WATI_TOKEN=<bearer token from the WATI dashboard>
WATI_CHANNEL=              # optional — send-from number, blank uses the default
```

Delivery tracking needs one more (see **Delivery: who saw it, who replied**):

```
WATI_WEBHOOK_TOKEN=<32+ random characters>   # without it /webhooks/wati refuses every call
```

`WATI_API_ENDPOINT` / `WATI_API_TOKEN` work as aliases, so the same `.env`
still serves the drip engine in the sibling `wati_cleanup` repo.

Optional tuning, via environment variables:

| | | |
| --- | --- | --- |
| `WATI_SEND_WORKERS` | 5 | parallel sends inside one campaign |
| `WATI_MAX_CONCURRENT` | 2 | campaigns sending at the same time |
| `WATI_POLL_SECONDS` | 15 | how often the scheduler looks for due campaigns |
| `WATI_RETRY_ENABLED` | 1 | retry a campaign's failed leads a week later (`0` turns it off) |
| `WATI_RETRY_AFTER_DAYS` | 7 | how long to wait before retrying them |
| `WATI_RETRY_MAX_ROUNDS` | 1 | how many times a lead may be retried |
| `WATI_RETRY_THROTTLE` | — | messages/min for retries; blank reuses the original campaign's |
| `WATI_RETRY_SWEEP_SECONDS` | 300 | how often the scheduler looks for retries that have come due |
| `WATI_EVENT_RETENTION_DAYS` | 180 | how long the raw webhook feed is kept (0 = forever) |
| `WATI_WEBHOOK_LOG` | summary | console logging: `summary`, `verbose`, `off` |
| `PUBLIC_BASE_URL` | — | this app's public origin, for printing the webhook URL |

Campaigns are stored in `campaigns_leads_wati.wati_campaigns`. Without a
reachable MongoDB they fall back to `data/campaigns.json` (gitignored) —
the dashboard shows which backend is live.

```bash
python3 wati_client.py             # connection check
python3 wati_client.py templates   # list approved templates
python3 campaign_store.py          # which backend, and recent campaigns
python3 campaign_retry.py          # which campaigns are waiting on a retry
```

### Retrying failed leads, seven days on

A campaign is not finished when the last message goes out. Some sends are
refused by WATI outright, and more are accepted and then quietly declined by
Meta — the single biggest bucket being error `131049`, *"this message was not
delivered to maintain healthy ecosystem engagement"*, whose own guidance is
that the same message often lands if it is tried again in a few days.

So it is. When a campaign finishes, it is **armed**: a note on the campaign
saying its failures will be looked at in seven days. When that day arrives the
scheduler collects the leads that failed, builds them into a new campaign, and
sends it.

**Why wait, rather than retry at the end of the send?** Because at the end of
the send the failure list does not exist yet. Meta's refusals arrive down the
delivery webhook for hours afterwards, so a retry list built the moment the
last message went out would miss most of what it is for. Waiting the week and
*then* reading the recipient list is what makes the list complete — which is
also why the campaign page shows a date rather than a number until the day
comes.

The retry is an ordinary campaign. It has its own row on the dashboard, its own
progress bar, pause and cancel buttons, delivery counters and results CSV, and
it links back to the campaign whose failures it carries. Nothing about sending
it is special-cased, which is the point.

**Who is not retried.** Three kinds of failure are held back on principle, and
the campaign page names them and their counts rather than quietly dropping
them:

| Held back | Why |
| --- | --- |
| Opted out of marketing (`131050`) | A real opt-out, made inside WhatsApp. Messaging again is a policy violation, not an optimisation. |
| Not reachable on WhatsApp (`131026`) | The number cannot receive WhatsApp at all. A retry fails identically every time and costs quality rating. |
| Template problem (`132xxx`) | The template is at fault, not the contact. Resending the same broken template gets the same rejection — fix it and send fresh. |

Everything else is retried: WATI-side refusals (a 502, a timeout, a rate
limit), Meta's pacing and holdout blocks, account and rate-limit errors, and
anything unclassified. A lead who somehow collected both a failure and a
delivery receipt is left alone, and a number appearing twice in the list is
messaged once.

**Bounds.** By default a lead is retried once — `WATI_RETRY_MAX_ROUNDS` is 1,
so the retry campaign is not itself retried. Dry runs are never armed, because
nothing was sent. Cancelled campaigns are never armed either: someone stopped
that send on purpose, and resurrecting a slice of it a week later is the
opposite of what the click meant.

**Controls.** On a campaign's page:

* **Retry now** — build the retry immediately instead of waiting. Useful for a
  campaign that finished before this feature existed, and for the impatient;
  the warning that receipts may still be arriving is on the confirm dialog.
* **Don't retry** — cancel the pending retry, leaving what was sent alone.

Two app processes pointed at the same database cannot both build a retry of
the same campaign: the claim is a single atomic compare-and-set, the same
mechanism (and for the same reason) as claiming a campaign to send it.

```bash
python3 campaign_retry.py             # what is armed, due, or done
python3 campaign_retry.py --sweep     # create any retries that have come due, now
python3 campaign_retry.py --selftest  # prove the policy, the claim and the sweep
```

### One caveat worth knowing

WATI sits behind Cloudflare, which rejects Python's default `User-Agent` with
`403 error code: 1010` before the request reaches the API. `wati_client.py`
sends a browser UA for that reason — if sending ever starts failing with 403,
that header is the first thing to check.


## Delivery: who saw it, who replied

A send is not a delivery. WATI answers `sendTemplateMessage` with `200 OK` and
that is all the sender ever learns — whether the phone received it, whether
anyone opened it, whether anyone wrote back, and whether Meta refused it
afterwards all arrive later, on a **webhook**.

Point WATI at `/webhooks/wati` and the campaigns dashboard gains four numbers
per campaign:

| | |
| --- | --- |
| **Delivered** | reached the handset |
| **Read** | the contact opened it — *"how many leads saw the campaign"* |
| **Replied** | the contact wrote back, with what they said |
| **Blocked** | Meta refused it after WATI had accepted it, with Meta's own error code |

`sent` still means only "WATI accepted it", and is still counted separately
from `Refused at send` (WATI rejecting the API call). Meta refusing a message
WATI already accepted is a different fact and gets its own counter, because the
fix for each is different.

### Setting it up

Open **`/webhooks`** in the app. It prints the exact URL to paste, lists the
events to tick, and then shows every event as it arrives — which is the fastest
way to tell a working hook from a mistyped one.

1. Put a long random value in `.env`:

   ```
   WATI_WEBHOOK_TOKEN=<32+ random characters>
   ```

   Without it the endpoint refuses **every** call, including WATI's. A webhook
   that accepts anything is one anybody can use to write fake delivery numbers
   into your dashboard, so there is deliberately no default and no fallback.

2. In WATI: **Settings → Webhooks → Add Webhook**, and paste

   ```
   https://<your-public-domain>/webhooks/wati?token=<WATI_WEBHOOK_TOKEN>
   ```

   The token may also be sent as an `X-Webhook-Token` header instead.

   > If the app log fills with `rejected: bad token (length=49, expected 48)`,
   > the URL saved in WATI has picked up a stray character — a trailing quote
   > from a copy-paste is the one that has actually happened, arriving as
   > `token=…4830%22`. Whitespace and surrounding quotes are now stripped
   > before the comparison, so only a genuinely different token is refused.
   > The length in that log line is the diagnostic: one over is a paste
   > artifact, far off is the wrong token.

3. Tick these events:

   | Event | Why |
   | --- | --- |
   | `templateMessageSent` | confirms our send **and carries the message id** — tick this one first |
   | `sentMessageDELIVERED` | reached the handset |
   | `sentMessageREAD` | the contact opened it |
   | `sentMessageREPLIED` | the contact answered it |
   | `sentMessageFAILED` | Meta refused it — carries the error code |
   | `message` | the reply itself, with its text |
   | `newContactMessageReceived` | a first reply from a number WATI has not seen before |

This process listens on `127.0.0.1` behind the reverse proxy, so the proxy has
to forward `/webhooks/wati` through to it. `/webhooks` works out the public URL
from `X-Forwarded-Host`; if your proxy does not send that, set
`PUBLIC_BASE_URL=https://your-domain` in the environment and it will print the
right URL instead of a placeholder.

### How an event finds its campaign

WATI's status events carry a message id but **no phone number**, and its send
response usually carries no id at all — so neither half is enough on its own.
The chain that closes the gap:

1. `templateMessageSent` arrives with **both** the number and the id. It is
   matched by phone, and its id is stamped onto that recipient.
2. Every later event for that message — delivered, read, failed — matches on
   the id **exactly**.
3. An inbound reply's own id matches nothing we sent; `replyContextId`, the id
   of the message being answered, is what links it back.
4. Failing all of that, the number itself, taking the most recent campaign that
   had actually sent to it *before* the event happened. Without that last
   condition a receipt for yesterday's campaign gets credited to this morning's,
   which is the usual way a funnel ends up with more reads than sends.

Anything that matches nothing is still stored, and the **Unmatched only**
filter on `/webhooks` lists them. Unmatched events are normal — chatbot
traffic, manual sends, replies to something else — but a pile of them next to
campaigns showing no delivery numbers is the sign that matching is broken.

### Two things about WATI's webhook worth knowing

* **Every event arrives twice.** WATI fires `sentMessageREAD` and
  `sentMessageREAD_v2` for the same read, and only the `_v2` twin carries
  `localMessageId`. The WhatsApp id is the only key both halves share, so it is
  what the dedupe is built on. Counters are moved by a compare-and-set that
  counts only the transition it performed itself, so a redelivered receipt —
  or WATI retrying after a lost response — cannot inflate a number.
* **`owner: false` means the contact sent it, not us.** WATI still stamps those
  inbound messages `statusString: "SENT"`, so classifying on the status alone
  counts a lead's reply as one of your own sends.

The endpoint answers **200 to anything it can read**, even an event about a
campaign this app has never heard of. WATI retries on a non-2xx, so returning
an error for an unmatched event would turn one of them into an endless stream.

### Meta's refusal codes

`sentMessageFAILED` carries Meta's own code, and the detail page groups the
refusals by what the code means and what, if anything, to do about it:

| Code | Means | What to do |
| --- | --- | --- |
| `131049` | Meta's per-user marketing throttle — the common one | Nothing is wrong with the number or the template; it often lands if retried in a few days |
| `130472` | user is in an experiment holdout group | Retry later |
| `131048` | spam-rate limit hit for this recipient | Slow down, improve targeting |
| `131047` | outside the 24-hour window | Expected for a cold list; templates only |
| `131026` | number cannot receive WhatsApp messages | Drop it from the list — retrying always fails |
| `131050` | contact turned marketing messages off | A real opt-out. Do not message again |
| `132000`–`132015` | template problem (parameters, approval, paused) | Fix the template or the mapping |

Codes are matched **as codes**, never against Meta's prose — the wording is
written for a human reading a log and is free to change. An unrecognised code
is shown as unclassified with its raw text rather than guessed at.

### Watching it work — the console

Every event that tells the app something prints one line, so a campaign's
progress is readable in `journalctl -u wati -f` without opening a browser:

```
[wati/webhook] 13:11:35 sent      919812322201   Asha Nair · "Diwali offer — batch 1" (by phone)
[wati/webhook] 13:11:35 delivered 919812322201   Asha Nair · "Diwali offer — batch 1" (by message-id) -> delivered
[wati/webhook] 13:11:36 read      919812322201   Asha Nair · "Diwali offer — batch 1" (by message-id) -> read
[wati/webhook] 13:11:36 failed    919812322203   Meera Iyer · "Diwali offer — batch 1" (by message-id) · 131049 Meta held it back (healthy-ecosystem limit) -> undelivered
[wati/webhook] 13:11:36 received  919812322201   Asha Nair · "Diwali offer — batch 1" (by message-id) · "Yes, please share the fee structure" -> replied
[wati/webhook] 13:12:27 received  971509161280   unmatched — no campaign here sent to this number · "Ok thank you"
```

`WATI_WEBHOOK_LOG` controls it: `summary` (default, the above), `verbose` (adds
the raw payload of every event), or `off`.

Redeliveries are dropped from `summary` — WATI fires each event twice and a
doubled log reads like doubled delivery. `verbose` shows them.

Two things that make this readable rather than merely present:

* **There is no per-request access log.** `Handler.log_message` is a no-op, so
  a webhook event is one meaningful line rather than one line of URL plus one
  of content. It is also what keeps the token — which travels in the query
  string — out of the journal, so do not re-enable access logging without
  redacting `token=` first.
* **stdout is line-buffered at startup.** Under systemd or `nohup` it is a
  pipe, which Python block-buffers: without the `reconfigure` in `__main__`
  every one of these lines sits in an 8 KB buffer, and a working service reads
  as a dead one.

### Checking it, and where it is stored

```bash
python3 wati_webhook.py                 # token set? how many events? matched vs unmatched
python3 wati_webhook.py --selftest      # drive a synthetic campaign through the whole sequence
python3 wati_webhook.py --replay e.json # feed a captured payload back in
```

`--selftest` is worth running after any change here. It creates a dry-run
campaign, posts a send / delivered / delivered-twin / read / reply sequence at
it, asserts the funnel reads exactly `1 delivered, 1 read, 1 replied` — which
is what caught the array-path bug that had one delivery counted four times —
then deletes the campaign again.

Raw events live in `campaigns_leads_wati.wati_message_events` (or
`data/webhook_events.json` without Mongo) and are pruned after
`WATI_EVENT_RETENTION_DAYS`, default 180. That is only the raw log: the numbers
themselves are stamped on the campaign and its recipients, and are permanent.
The results CSV carries them per contact — delivered, read, replied, the reply
text, and Meta's reason.
# wati_cleanup
# wati-bigin

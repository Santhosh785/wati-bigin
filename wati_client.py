#!/usr/bin/env python3
"""
wati_client.py

Thin, standard-library-only wrapper around the WATI REST API — the send side
of this repo, the way `bigin_store.py` is the read side of the Bigin mirror.

Config comes from `.env` next to this file (real environment variables win):

    WATI_API_URL     tenant endpoint, e.g. https://live-mt-server.wati.io/1087297
    WATI_TOKEN       API bearer token from the WATI dashboard
    WATI_CHANNEL     optional — WhatsApp number to send from ("+9163…"),
                     blank sends from the account's default number

`WATI_API_ENDPOINT` / `WATI_API_TOKEN` are accepted as aliases, because the
drip engine in the sibling `wati_cleanup` repo reads those names and the same
`.env` serves both.

Nothing here has state or does any scheduling — `campaign_runner.py` owns that.
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# WATI sits behind Cloudflare, which answers urllib's default User-Agent with a
# 403 "error code: 1010" before the request ever reaches the API. A normal
# browser UA is what gets through — this is not an attempt to look like a
# person, just the header the edge insists on.
USER_AGENT = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

TIMEOUT = 45          # seconds per HTTP call
BULK_CHUNK = 100      # receivers per /api/v2/sendTemplateMessages call


class WatiUnavailable(RuntimeError):
    """WATI is not configured (missing endpoint or token)."""


class WatiError(RuntimeError):
    """The API was reached but refused or failed the call."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def load_env(path=ENV_PATH):
    """Read .env next to this file; real environment variables win."""
    env = dict(os.environ)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                env.setdefault(k.strip(), v)
    return env


def config(env=None):
    """Return (endpoint, token, channel_number). Raises WatiUnavailable."""
    env = env or load_env()
    endpoint = (env.get("WATI_API_URL") or env.get("WATI_API_ENDPOINT") or "").strip()
    token = (env.get("WATI_TOKEN") or env.get("WATI_API_TOKEN") or "").strip()
    channel = (env.get("WATI_CHANNEL") or "").strip()
    if not endpoint:
        raise WatiUnavailable("WATI_API_URL is not set in .env.")
    if not token:
        raise WatiUnavailable("WATI_TOKEN is not set in .env.")
    if not token.lower().startswith("bearer "):
        token = "Bearer " + token
    return endpoint.rstrip("/"), token, channel


def is_configured():
    try:
        config()
        return True
    except WatiUnavailable:
        return False


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def _request(method, path, body=None, endpoint=None, token=None):
    endpoint, tok, _ = config() if endpoint is None or token is None else (endpoint, token, "")
    url = endpoint + path
    headers = {
        "Authorization": tok,
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except urllib.error.URLError as e:
        raise WatiError(f"Could not reach WATI: {e.reason}") from None
    except TimeoutError:
        raise WatiError(f"WATI timed out after {TIMEOUT}s.") from None

    try:
        payload = json.loads(raw.decode("utf-8", "replace")) if raw else {}
    except ValueError:
        payload = {"raw": raw.decode("utf-8", "replace")[:400]}

    if status >= 400:
        raise WatiError(f"WATI {method} {path} failed ({status}): {_detail(payload)}",
                        status, payload)
    return payload


def _detail(payload):
    if isinstance(payload, dict):
        for key in ("info", "message", "error", "raw"):
            if payload.get(key):
                return str(payload[key])[:400]
        return json.dumps(payload)[:400]
    return str(payload)[:400]


# --------------------------------------------------------------------------- #
# templates
# --------------------------------------------------------------------------- #
def list_templates(approved_only=True, page_size=500):
    """
    Approved WhatsApp templates from the WATI dashboard, newest names first.

    Returns dicts of {name, status, category, language, params, body, header,
    footer, buttons} — `params` is the ordered list of placeholder names the
    campaign builder asks the user to map.
    """
    payload = _request("GET", f"/api/v1/getMessageTemplates?pageSize={int(page_size)}&pageNumber=1")
    out = []
    for t in payload.get("messageTemplates") or []:
        status = (t.get("status") or "").upper()
        if approved_only and status != "APPROVED":
            continue
        out.append({
            "name": t.get("elementName") or "",
            "status": status,
            "category": t.get("category") or "",
            "language": (t.get("language") or {}).get("text")
                        if isinstance(t.get("language"), dict) else (t.get("language") or ""),
            "params": [p.get("paramName") for p in (t.get("customParams") or []) if p.get("paramName")],
            "body": str(t.get("body") or t.get("bodyOriginal") or ""),
            "header": _header_text(t.get("header")),
            "footer": str(t.get("footer") or ""),
            "buttons": [str(b.get("text")) for b in (t.get("buttons") or [])
                        if isinstance(b, dict) and b.get("text")],
        })
    out.sort(key=lambda t: t["name"].lower())
    return out


def _header_text(header):
    """
    WATI is inconsistent here: a text header arrives as {'type':…, 'text':…},
    a media header as a bare type code (an int), and a template with no header
    as null. Only real text is worth showing, so everything else becomes "".
    """
    if isinstance(header, dict):
        return str(header.get("text") or "")
    if isinstance(header, str):
        return header
    return ""


def get_template(name, approved_only=True):
    for t in list_templates(approved_only=approved_only):
        if t["name"] == name:
            return t
    return None


# --------------------------------------------------------------------------- #
# sending
# --------------------------------------------------------------------------- #
def normalize_number(country_code, phone):
    """('91', '9876543210') -> '919876543210'. WATI wants digits, no '+'."""
    digits = "".join(ch for ch in f"{country_code}{phone}" if ch.isdigit())
    return digits


def as_custom_params(params):
    """
    Accept either {'name': 'Asha'} or [('name', 'Asha')] or ['Asha'] and return
    WATI's [{'name': ..., 'value': ...}] shape. A bare list is positional, so it
    is numbered 1..n the way {{1}}-style templates expect.
    """
    if not params:
        return []
    if isinstance(params, dict):
        items = list(params.items())
    elif params and isinstance(params[0], (list, tuple)):
        items = list(params)
    else:
        items = [(str(i + 1), v) for i, v in enumerate(params)]
    return [{"name": str(k), "value": "" if v is None else str(v)} for k, v in items]


def send_template(phone, template_name, broadcast_name, params=None, channel_number=None):
    """
    One approved template message to one number.

    `phone` must already include the country code and no '+'. Returns the API
    payload; raises WatiError when WATI refuses the send.
    """
    endpoint, token, default_channel = config()
    query = urllib.parse.urlencode({"whatsappNumber": phone})
    body = {
        "template_name": template_name,
        "broadcast_name": broadcast_name,
        "parameters": as_custom_params(params),
    }
    channel = channel_number or default_channel
    if channel:
        body["channel_number"] = channel

    payload = _request("POST", f"/api/v1/sendTemplateMessage?{query}", body,
                       endpoint=endpoint, token=token)
    if payload.get("result") is False or payload.get("ok") is False:
        raise WatiError(f"WATI refused the send: {_detail(payload)}", 200, payload)
    return payload


def message_ids(payload):
    """
    ('wamid.…', 'wati-guid') out of a send response — either may be missing.

    WATI's sendTemplateMessage answers with a `result: true` and, depending on
    tenant and template, sometimes the ids of the message it just queued. When
    it does, storing them turns every later delivery receipt into an exact
    match instead of a guess by phone number (see wati_webhook.py). When it
    does not, the `templateMessageSent` webhook carries both the number and the
    ids, and that is what fills the gap.

    Written to look in every place the field has been seen rather than one,
    because the shape differs between the v1 and v2 send endpoints.
    """
    if not isinstance(payload, dict):
        return None, None
    inner = payload.get("message") if isinstance(payload.get("message"), dict) else {}

    def first(*values):
        for v in values:
            if isinstance(v, (str, int)) and str(v).strip():
                return str(v).strip()
        return None

    wamid = first(payload.get("whatsappMessageId"), payload.get("whatsapp_message_id"),
                  inner.get("whatsappMessageId"), inner.get("whatsapp_message_id"),
                  payload.get("messageId"), inner.get("id"), payload.get("id"))
    local = first(payload.get("localMessageId"), payload.get("local_message_id"),
                  inner.get("localMessageId"), inner.get("local_message_id"))
    return wamid, local


def send_template_bulk(receivers, template_name, broadcast_name, channel_number=None):
    """
    Up to BULK_CHUNK receivers in one call.

    `receivers` is [{'whatsappNumber': '9198…', 'customParams': [...]}, …].
    WATI answers with one aggregate result rather than per-number status, which
    is why campaign_runner.py prefers send_template() — this is here for the
    rare case where throughput matters more than knowing who failed.
    """
    endpoint, token, default_channel = config()
    body = {
        "template_name": template_name,
        "broadcast_name": broadcast_name,
        "receivers": receivers,
    }
    channel = channel_number or default_channel
    if channel:
        body["channel_number"] = channel
    payload = _request("POST", "/api/v2/sendTemplateMessages", body,
                       endpoint=endpoint, token=token)
    if payload.get("result") is False:
        raise WatiError(f"WATI refused the bulk send: {_detail(payload)}", 200, payload)
    return payload


# --------------------------------------------------------------------------- #
# status / diagnostics
# --------------------------------------------------------------------------- #
def status():
    """Never raises — powers the connection badge in the UI."""
    info = {"configured": False, "reachable": False, "endpoint": None,
            "templates": 0, "error": None}
    try:
        endpoint, _token, _channel = config()
    except WatiUnavailable as e:
        info["error"] = str(e)
        return info
    info["configured"] = True
    info["endpoint"] = endpoint
    try:
        info["templates"] = len(list_templates(approved_only=True))
        info["reachable"] = True
    except WatiError as e:
        info["error"] = str(e)
    except Exception as e:                      # noqa: BLE001 — a badge must never break the page
        info["error"] = str(e)
    return info


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "templates":
        for t in list_templates():
            print(f"{t['name']:<45} {t['category']:<10} params={t['params']}")
    else:
        print(json.dumps(status(), indent=2))

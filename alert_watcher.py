#!/usr/bin/env python3
"""
Catch missed critical alerts in a Slack channel.

Two modes, run by two separate schedulers:

  detect  (top of every hour, 00:00, 01:00, ...)
      Scan the last WINDOW_SEC of channel history for severity=Critical
      alerts, decide which are unacked, and — if any — post ONE summary that
      pings @sre-shift-leads. The summary message id + the unacked alert list
      are saved to state.json. This is the only mode that scans the channel,
      so a brand-new alert is picked up at the next hour boundary.

  check   (every 5 minutes)
      Look only at the summary posted by the last detect run. If it has been
      acked (any reaction or reply) -> stop, clear state. Otherwise re-post a
      reminder ping and bump the counter, up to MAX_REPOSTS total posts, then
      give up and stay quiet until the next hourly detect.

  escalate  (once a day at 09:00 — end of the night window)
      Rescan the whole overnight window (NIGHT_WINDOW_SEC back, default the
      12h from 21:00 to 09:00). Any critical alert still unacked at 9am gets
      escalated: DM each user in ESCALATE_USERS individually AND post one
      summary in ESCALATE_CHANNEL_ID that pings them. detect/check keep
      running overnight too, so the normal @sre-shift-leads ping still fires;
      escalate is the morning safety net for anything that slipped through.

An alert / summary is "acked" if it has ANY reaction OR ANY reply from someone
other than the alerting app itself.

Usage:
    python3 alert_watcher.py detect
    python3 alert_watcher.py check
    python3 alert_watcher.py escalate
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).with_name(".env"))
except ImportError:
    pass


def load_config():
    """Non-secret settings from config.json (next to the script)."""
    path = Path(os.environ.get("CONFIG_FILE", Path(__file__).with_name("config.json")))
    if path.exists():
        return json.loads(path.read_text())
    return {}


_CFG = load_config()


def cfg(key, default=None):
    """Setting lookup: env var wins, then config.json, then default."""
    return os.environ.get(key, _CFG.get(key, default))


# SLACK_BOT stays a secret in .env / the environment — never in config.json.
SLACK_TOKEN = os.environ.get("SLACK_BOT", "")
CHANNEL_ID = cfg("CHANNEL_ID", "")
SUBTEAM_ID = cfg("SUBTEAM_ID", "")                     # e.g. S0XXXXXXX (sre-shift-leads usergroup)
SUBTEAM_HANDLE = cfg("SUBTEAM_HANDLE", "sre-shift-leads")

_state_val = cfg("STATE_FILE", "state.json")
STATE_FILE = Path(_state_val)
if not STATE_FILE.is_absolute():                           # relative -> anchor to script dir
    STATE_FILE = Path(__file__).with_name(str(STATE_FILE))
WINDOW_SEC = int(cfg("WINDOW_SEC", 3600))                   # detect scans this far back (default 1h = one hourly bucket)
MAX_REPOSTS = int(cfg("MAX_REPOSTS", 10))                   # max summary posts per cycle (initial + reminders) before giving up
WINDOW_LABEL = cfg("WINDOW_LABEL", "1 hr")                  # text shown in the summary header


def _id_list(value):
    """Parse a user-id list from config (JSON array) or env (comma/space string)."""
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except ValueError:
            pass
        return [tok for tok in re.split(r"[,\s]+", value) if tok]
    return []


# escalate mode: overnight safety net -----------------------------------------
NIGHT_WINDOW_SEC = int(cfg("NIGHT_WINDOW_SEC", 43200))       # how far escalate rescans (default 12h = 21:00->09:00)
NIGHT_WINDOW_LABEL = cfg("NIGHT_WINDOW_LABEL", "overnight (9pm–9am)")
ESCALATE_USERS = _id_list(cfg("ESCALATE_USERS", []))         # Slack user ids DM'd at 9am for unacked overnight criticals
ESCALATE_CHANNEL_ID = cfg("ESCALATE_CHANNEL_ID", CHANNEL_ID) # channel the 9am escalation summary is posted to


def _norm(s):
    """Lowercase + collapse whitespace, for tolerant alert-name matching."""
    return re.sub(r"\s+", " ", str(s)).strip().lower()


# Alert names flagged red (:red_circle:) in both the 1 hr and overnight summaries.
# From config.json RED_ALERTS (JSON array); env override is comma-separated.
def _str_list(value):
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except ValueError:
            pass
        return [tok.strip() for tok in value.split(",") if tok.strip()]
    return []


RED_ALERTS = _str_list(cfg("RED_ALERTS", []))
_RED_NORM = [_norm(k) for k in RED_ALERTS]


def is_red(name):
    """True if this alert name matches a configured red-highlight alert."""
    n = _norm(name)
    return any(k and (k in n or n in k) for k in _RED_NORM)

SLACK_API = "https://slack.com/api"


# --------------------------------------------------------------------------- #
# Slack helpers
# --------------------------------------------------------------------------- #
def slack(method, http="get", **params):
    """Call a Slack Web API method, return parsed JSON, raise on Slack error."""
    url = f"{SLACK_API}/{method}"
    headers = {"Authorization": f"Bearer {SLACK_TOKEN}"}
    fn = requests.post if http == "post" else requests.get
    if http == "post":
        headers["Content-Type"] = "application/json; charset=utf-8"
        resp = fn(url, headers=headers, json=params, timeout=30)
    else:
        resp = fn(url, headers=headers, params=params, timeout=30)
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"slack {method} failed: {data.get('error')}")
    return data


def fetch_recent_messages(oldest_ts):
    """All top-level messages with ts >= oldest_ts (handles pagination)."""
    messages, cursor = [], None
    while True:
        params = {"channel": CHANNEL_ID, "oldest": f"{oldest_ts:.6f}", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = slack("conversations.history", **params)
        messages.extend(data.get("messages", []))
        cursor = data.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            break
    return messages


def sender_of(msg):
    """Stable identity of a message's author (the app's bot_id, or a human user)."""
    return msg.get("bot_id") or msg.get("app_id") or msg.get("user")


def has_external_reply(parent_ts, alert_sender):
    """True if the thread has a reply from anyone OTHER than the alert's own app.

    A reply by the alerting app itself (e.g. Argus threading its own updates) does
    NOT count as an acknowledgement.
    """
    data = slack("conversations.replies", channel=CHANNEL_ID, ts=parent_ts, limit=200)
    for r in data.get("messages", []):
        if r.get("ts") == parent_ts:
            continue                                  # the parent alert itself
        if sender_of(r) != alert_sender:
            return True
    return False


def is_acked(ts, sender, has_reaction, reply_count):
    """Acked = any reaction, OR a reply from someone other than the alert app."""
    if has_reaction:
        return True
    if reply_count > 0:
        return has_external_reply(ts, sender)
    return False


def summary_acked(ts):
    """Has the bot's own summary message been acked (any reaction or any reply)?"""
    data = slack("reactions.get", channel=CHANNEL_ID, timestamp=ts)
    msg = data.get("message", {})
    return bool(msg.get("reactions")) or msg.get("reply_count", 0) > 0


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
UUID_RE = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def message_text(msg):
    """All searchable text, with Slack markup (* and `) stripped.

    Alert content lives in attachments: the [Firing]/[resolved] marker is in
    `title`, fields are in `text`, and values are wrapped in backticks.
    """
    parts = [msg.get("text", "")]
    for att in msg.get("attachments", []):
        parts += [att.get("title", ""), att.get("pretext", ""),
                  att.get("text", ""), att.get("fallback", "")]
    parts.append(json.dumps(msg.get("blocks", [])))
    raw = "\n".join(p for p in parts if p)
    return raw.replace("`", "").replace("*", "")          # drop bold/code markup


def alert_key(name, service):
    return f"{name.strip().lower()}|{service}"


def parse_alert(msg):
    """Return a Critical alert dict (firing OR resolved), else None.

    Resolved alerts are returned too (with resolved=True) so a fire-then-resolve
    pair can be correlated and the firing one suppressed.
    """
    text = message_text(msg)
    if not re.search(r"severity\s*:\s*critical\b", text, re.I):
        return None

    m = re.search(r"alertname\s*:\s*(.+)", text, re.I)
    name = m.group(1).strip() if m else "Unknown alert"

    m = re.search(r"impacted_service_ids\s*:\s*(" + UUID_RE + ")", text, re.I)
    if not m:
        m = re.search(UUID_RE, text)                      # fallback: UUID in summary
    service = m.group(1 if m and m.groups() else 0).strip() if m else "unknown"

    return {"ts": msg["ts"], "name": name, "service": service,
            "resolved": bool(re.search(r"\[resolved\]", text, re.I)),
            "sender": sender_of(msg),
            "has_reaction": bool(msg.get("reactions")),
            "reply_count": msg.get("reply_count", 0)}


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def age_str(ts, now):
    secs = max(0, int(now - float(ts)))
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hrs = mins // 60
    rem = mins % 60
    return f"{hrs}h{rem:02d}m ago" if rem else f"{hrs}h ago"


def alert_line(a, now):
    """One summary line; prefixed with :red_circle: for red-flagged alerts."""
    line = f"{a['name']} — service {a['service']} ({age_str(a['ts'], now)})"
    return f":red_circle: {line}" if is_red(a['name']) else line


def build_summary(alerts, now):
    """alerts: list of {ts, name, service}."""
    mention = (f"<!subteam^{SUBTEAM_ID}|{SUBTEAM_HANDLE}>"
               if SUBTEAM_ID else f"@{SUBTEAM_HANDLE}")
    lines = [f":rotating_light: In the last {WINDOW_LABEL} these critical alerts "
             f"have NOT been acked ({len(alerts)}):", ""]
    for a in alerts:
        lines.append(alert_line(a, now))
    lines += ["", mention]
    return "\n".join(lines)


def post_summary(alerts, now, count):
    """Post the summary and record it in state; returns the new summary dict."""
    text = build_summary(alerts, now)
    resp = slack("chat.postMessage", http="post", channel=CHANNEL_ID, text=text,
                 link_names=True)
    return {"ts": resp["ts"], "count": count,
            "alerts": [{"ts": a["ts"], "name": a["name"], "service": a["service"]}
                       for a in alerts]}


# --------------------------------------------------------------------------- #
# Modes
# --------------------------------------------------------------------------- #
def find_unacked(oldest_ts):
    """Scan the channel since oldest_ts, return unacked critical alerts (sorted).

    A firing critical is dropped if a later [resolved] cancels it, or if it is
    already acked. Ack state comes from the freshly-fetched message, so callers
    always see current reactions / reply counts.
    """
    # Ingest firing + resolved criticals; a later [resolved] cancels its firing pair.
    firing = {}                                       # ts -> alert dict
    resolved_at = {}                                  # alert_key -> latest resolved ts
    for msg in fetch_recent_messages(oldest_ts):
        alert = parse_alert(msg)
        if not alert:
            continue
        key = alert_key(alert["name"], alert["service"])
        if alert["resolved"]:
            resolved_at[key] = max(resolved_at.get(key, 0.0), float(alert["ts"]))
        else:
            firing[alert["ts"]] = alert

    unacked = []
    for ts, a in firing.items():
        key = alert_key(a["name"], a["service"])
        if resolved_at.get(key, 0.0) > float(ts):     # resolved after it fired
            continue
        if is_acked(ts, a["sender"], a["has_reaction"], a["reply_count"]):
            continue
        unacked.append(a)
    unacked.sort(key=lambda a: float(a["ts"]))
    return unacked


def detect():
    """Hourly: scan the window, post ONE summary of unacked criticals, seed state."""
    now = time.time()
    state = load_state()

    unacked = find_unacked(now - WINDOW_SEC)

    if not unacked:
        state["summary"] = None
        save_state(state)
        print("no unacked critical alerts")
        return

    state["summary"] = post_summary(unacked, now, count=1)
    save_state(state)
    print(f"posted summary 1/{MAX_REPOSTS} with {len(unacked)} unacked alert(s)")


def check():
    """Every 5 min: re-ping the last summary until it is acked or MAX_REPOSTS hit."""
    now = time.time()
    state = load_state()
    summary = state.get("summary")

    if not summary:
        print("no active summary — nothing to check")
        return

    try:
        if summary_acked(summary["ts"]):
            state["summary"] = None
            save_state(state)
            print("summary acked — stopping reposts")
            return
    except RuntimeError as e:
        print(f"summary recheck: {e}", file=sys.stderr)
        return

    count = summary.get("count", 1)
    if count >= MAX_REPOSTS:
        print(f"summary posted {count}/{MAX_REPOSTS}x, still unacked — giving up")
        return

    state["summary"] = post_summary(summary.get("alerts", []), now, count=count + 1)
    save_state(state)
    print(f"reposted summary {count + 1}/{MAX_REPOSTS}, still unacked")


def build_escalation(alerts, now):
    """9am escalation message: unacked overnight criticals + pings to the DM list."""
    mention = " ".join(f"<@{u}>" for u in ESCALATE_USERS)
    lines = [f":rotating_light: {len(alerts)} critical alert(s) went UNACKED "
             f"{NIGHT_WINDOW_LABEL} — escalating:", ""]
    for a in alerts:
        lines.append(alert_line(a, now))
    if mention:
        lines += ["", f"Not acknowledged overnight. {mention} please take a look."]
    return "\n".join(lines)


def dm_user(user_id, text):
    """Open (or reuse) an IM with user_id and post text there."""
    conv = slack("conversations.open", http="post", users=user_id)
    im_channel = conv["channel"]["id"]
    slack("chat.postMessage", http="post", channel=im_channel, text=text, link_names=True)


def escalate():
    """09:00: rescan the night window; DM + channel-ping any still-unacked criticals."""
    now = time.time()
    unacked = find_unacked(now - NIGHT_WINDOW_SEC)

    if not unacked:
        print("no unacked overnight critical alerts")
        return

    text = build_escalation(unacked, now)
    slack("chat.postMessage", http="post", channel=ESCALATE_CHANNEL_ID, text=text,
          link_names=True)

    delivered = 0
    for user_id in ESCALATE_USERS:
        try:
            dm_user(user_id, text)
            delivered += 1
        except RuntimeError as e:
            print(f"DM to {user_id} failed: {e}", file=sys.stderr)

    print(f"escalated {len(unacked)} overnight unacked alert(s): "
          f"posted to {ESCALATE_CHANNEL_ID}, DM'd {delivered}/{len(ESCALATE_USERS)} user(s)")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    if not SLACK_TOKEN or not CHANNEL_ID:
        sys.exit("SLACK_BOT and CHANNEL_ID must be set in .env")

    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "detect":
        detect()
    elif mode == "check":
        check()
    elif mode == "escalate":
        escalate()
    else:
        sys.exit("usage: alert_watcher.py {detect|check|escalate}")


if __name__ == "__main__":
    main()

# catch_missed_alerts

Slack bot that catches **critical alerts nobody acknowledged**.

An alert is **acked** if it has at least one emoji reaction OR at least one
threaded reply. Unacked `severity:Critical` alerts get re-posted with an
`@sre-shift-leads` ping every 5 minutes until someone acks them. Anything that
stays unacked through the night gets **escalated to named people at 09:00**.

## How it works

One script (`alert_watcher.py`), **three modes**, run by schedulers:

### `detect` — top of every hour (00:00, 01:00, …)

1. `conversations.history` — scan the **last `WINDOW_SEC` (1h)** for
   `severity:Critical` alerts. Skip `[resolved]` ones, and cancel any firing
   alert that a `[resolved]` (same alertname + service) arrived for afterwards.
2. Decide ack per alert: **a reaction (anyone), OR a reply from someone other
   than the alerting app itself**. A reply by the alert app (e.g. it threads its
   own updates) does NOT count.
3. If anything is unacked → post ONE `:rotating_light:` summary pinging the
   usergroup, and save its message id + the unacked list to `state.json`
   (counter = 1). If nothing is unacked → clear state, stay quiet.

`detect` is the **only** mode that scans the channel, so a brand-new alert that
fires mid-hour is picked up at the next hour boundary.

### `check` — every 5 minutes

Looks only at the summary from the last `detect`:

- Summary **acked** (any reaction or reply on it) → clear state, stop.
- Still unacked and under `MAX_REPOSTS` (10) total posts → **re-post the ping**,
  bump the counter.
- Counter hit `MAX_REPOSTS` → give up, stay quiet until the next hourly `detect`.

### `escalate` — once a day at 09:00 (end of the night window)

The overnight safety net. `detect`/`check` keep running through the night, so
the normal `@sre-shift-leads` ping still fires — but if a critical alert is
still unacked at 9am it gets escalated to named people:

1. Rescan the whole night window (`NIGHT_WINDOW_SEC` back, default 12h =
   21:00→09:00) with the same firing/resolved/ack logic as `detect`. Ack state
   is read fresh, so an alert acked at 2am is not escalated.
2. For anything still unacked → **DM each user in `ESCALATE_USERS`
   individually** (Srinivasu, Vivek, Rajeev) AND post one summary in
   `ESCALATE_CHANNEL_ID` (alerts-prod-v2) that pings them.

Night storage needs no extra state — the alerts already live in the channel
history; `escalate` just rescans it in the morning.

## Required Slack bot scopes

`channels:history` (or `groups:history` for private), `channels:read`,
`reactions:read`, `chat:write`, `usergroups:read`, and — for the 9am DMs —
`im:write` (open the DM) plus `users:read`.

Invite the bot to the channel: `/invite @detectunacknowledgeda`.

## Config

The bot token is the only secret — it lives in `.env`. Everything else is
non-secret and lives in `config.json`. For any setting, an env var of the same
name overrides `config.json` (handy for one-off runs).

**`.env`** (secret):

| var | meaning |
|-----|---------|
| `SLACK_BOT` | bot token `xoxb-…` |

**`config.json`** (non-secret):

| key | meaning |
|-----|---------|
| `CHANNEL_ID` | alert channel, e.g. `C074G5Q49D3` |
| `SUBTEAM_ID` | `sre-shift-leads` usergroup = `S07PZLLN8RF` |
| `SUBTEAM_HANDLE` | display handle (`sre-shift-leads`) |
| `WINDOW_SEC` | how far back `detect` scans (default 3600 = 1h, one hourly bucket) |
| `MAX_REPOSTS` | max summary posts per cycle (1 initial + reminders) before giving up (default 10) |
| `WINDOW_LABEL` | header text (default `1 hr`) |
| `STATE_FILE` | state path; relative paths anchor to the script dir (default `state.json`) |
| `NIGHT_WINDOW_SEC` | how far `escalate` rescans at 9am (default 43200 = 12h, 21:00→09:00) |
| `NIGHT_WINDOW_LABEL` | escalation header text (default `overnight (9pm–9am)`) |
| `ESCALATE_CHANNEL_ID` | channel the 9am summary is posted to (default = `CHANNEL_ID`) |
| `ESCALATE_USERS` | Slack user ids DM'd at 9am; JSON array in config, or comma/space list via env |

## Run locally

```bash
pip install -r requirements.txt
python3 alert_watcher.py detect   # hourly scan + initial post
python3 alert_watcher.py check    # 5-min ack re-check / re-ping
python3 alert_watcher.py escalate # 09:00 overnight escalation (DM + channel)
```

## Deploy on EC2

```bash
sudo mkdir -p /opt/catch_missed_alerts
sudo cp alert_watcher.py requirements.txt config.json .env /opt/catch_missed_alerts/
cd /opt/catch_missed_alerts
python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
chmod 600 .env                  # protect the token
```

### systemd timers (recommended)

Two scheduled one-shot units, not daemons: state lives in `state.json`, so each
run is independent. systemd gives logs (`journalctl`), restart, and reboot
survival for free. `detect` fires hourly on the clock; `check` fires every 5 min.

```bash
sudo cp deploy/catch-missed-alerts-detect.service   deploy/catch-missed-alerts-detect.timer \
        deploy/catch-missed-alerts-check.service    deploy/catch-missed-alerts-check.timer \
        deploy/catch-missed-alerts-escalate.service deploy/catch-missed-alerts-escalate.timer \
        /etc/systemd/system/
# edit User=/paths in the .service files if not ec2-user / /opt/catch_missed_alerts
sudo systemctl daemon-reload
sudo systemctl enable --now catch-missed-alerts-detect.timer catch-missed-alerts-check.timer \
        catch-missed-alerts-escalate.timer

systemctl list-timers 'catch-missed-alerts-*'          # next fire times
journalctl -u catch-missed-alerts-detect.service -f    # hourly detect logs
journalctl -u catch-missed-alerts-check.service -f     # 5-min check logs
journalctl -u catch-missed-alerts-escalate.service -f  # 09:00 escalation logs
```

Timezone matters: `escalate` fires at **09:00 local time**, so make sure the
box's clock (`timedatectl`) is the timezone you mean by "9am".

### Or plain cron

```cron
0    * * * * cd /opt/catch_missed_alerts && ./venv/bin/python alert_watcher.py detect   >> /var/log/catch_missed_alerts.log 2>&1
*/5  * * * * cd /opt/catch_missed_alerts && ./venv/bin/python alert_watcher.py check    >> /var/log/catch_missed_alerts.log 2>&1
0    9 * * * cd /opt/catch_missed_alerts && ./venv/bin/python alert_watcher.py escalate >> /var/log/catch_missed_alerts.log 2>&1
```

## Notes

- `state.json` is written next to the script; keep it on persistent disk.
- Output line format: `{alertname} — service {impacted_service_ids} ({age} ago)`.
- Bot's own summary messages aren't Critical, so they're never re-scanned.

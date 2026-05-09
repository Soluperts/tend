# Google integration setup

One-time procedure to wire `gws` (Google Workspace CLI) into tend so it
can read your calendar/inbox and write to a tend-owned calendar.

## Prerequisites

- A Google account (any flavour — personal `@gmail.com` or Workspace).
- Node.js available on the Pi (`gws` installs via npm).
- A second machine with a real browser (your laptop) for the OAuth
  consent step. The Pi has no browser.

## 1. Install gws on the Pi

```bash
sudo npm install -g @googleworkspace/cli
gws --version
```

## 2. Create an OAuth client in Google Cloud

1. Open [Google Cloud Console → APIs & Services → Credentials](https://console.cloud.google.com/apis/credentials).
2. Create or select a project (any name).
3. Enable: Google Calendar API, Gmail API.
4. **Create credentials** → **OAuth client ID** → Application type
   "Desktop app" → name "tend".
5. Download the JSON. Keep it; you'll point `gws` at it on your laptop.

## 3. Login on your laptop

On your laptop (not the Pi):

```bash
npm install -g @googleworkspace/cli
gws auth setup       # paste the OAuth client JSON when prompted
gws auth login \
  --scope https://www.googleapis.com/auth/gmail.readonly \
  --scope https://www.googleapis.com/auth/calendar.readonly \
  --scope https://www.googleapis.com/auth/calendar.app.created
```

A browser window opens. Approve all three scopes. The CLI stores an
encrypted token locally.

## 4. Export the token to the Pi

On your laptop:

```bash
gws auth export --unmasked > google-creds.json
scp google-creds.json pi:~/.tend/secrets/google-creds.json
```

On the Pi:

```bash
chmod 600 ~/.tend/secrets/google-creds.json
```

## 5. Verify on the Pi

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$HOME/.tend/secrets/google-creds.json \
  gws auth status
```

You should see all three scopes listed.

## 6. Bootstrap the tend calendar

Once on the Pi:

```bash
mkdir -p ~/.tend/google
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$HOME/.tend/secrets/google-creds.json \
  gws calendar +insert \
    --summary "tend" \
    --description "Schedule blocks managed by tend" \
    --json \
  | python3 -c "import json, sys; print(json.load(sys.stdin)['id'])" \
  > ~/.tend/google/tend-calendar-id

cat ~/.tend/google/tend-calendar-id   # sanity-check the ID is there
```

If you've already run the create command and got a `jq: command not
found` error, the calendar may have been created anyway (gws panics
on the broken pipe *after* writing its output). Check with:

```bash
GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE=$HOME/.tend/secrets/google-creds.json \
  gws calendar list --json \
  | python3 -c "import json, sys; m=[c for c in json.load(sys.stdin) if c.get('summary')=='tend']; print(m[0]['id'] if m else 'NONE')"
```

If it prints an id, save it to `~/.tend/google/tend-calendar-id`
manually and skip the create.

## 7. Restart tend

```bash
systemctl --user daemon-reload
systemctl --user restart tend
```

## 8. Try it

Speak the wake word, then:

- "Morning briefing." — should describe today's events + inbox state.
- "Set up my daily routine." — walks you through routine-setup.

## 9. Enable the schedule-watcher trigger

The schedule-watcher skill is installed but its `every 15m` trigger is
inert until you explicitly enable it. Speak the wake word, then say:

> "Enable triggers for schedule watcher."

Or, equivalently, while tend is stopped, edit `~/.tend/cron/jobs.json`
to add the trigger directly. After enabling, the watcher will tick
every 15 minutes and react to bracket-tagged events on watched
calendars.

Verify with `tend schedule list` — you should see a `skill:schedule-watcher`
source row.

## Smoke checks

```bash
# Calendar reads work?
gws calendar +agenda --json | head -20

# Watcher finds a tagged event?
python3 ~/.tend/skills/schedule-watcher/bin/tick.py
# expected: {"scheduled": N, "seen_tagged": M, ...}

# A schedule entry was created?
tend schedule list
```

## If a refresh fails (rare)

If `gws auth status` on the Pi later starts reporting "token expired"
(typically only happens if you revoke consent), repeat steps 3-4.

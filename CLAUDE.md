# CLAUDE.md

## Project Overview

**Media Servarr Sync** — A lightweight Flask webhook receiver that listens for Sonarr/Radarr events and triggers targeted Plex library scans on only the affected folder, rather than a full library refresh. Optionally integrates with rclone VFS to clear cache before scanning.

Flow: `Sonarr/Radarr → webhook → [rclone vfs/forget + vfs/refresh] → Plex partial scan`

## Running the App

```bash
# Recommended: Docker Compose
docker compose up -d

# Python directly (port 5000 by default)
python media-servarr-sync.py
```

Copy `.env.example` to `.env` and fill in values before running.

## Key Environment Variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `PLEX_URL` | yes | `http://127.0.0.1:32400` | Plex server address |
| `PLEX_TOKEN` | yes | — | Plex auth token |
| `SECRET_KEY` | yes | — | Session cookie signing key |
| `SECTION_MAPPING` | yes | — | JSON: path prefix → Plex section ID |
| `SONARR_URL` / `SONARR_API_KEY` | no | — | Enables quality profile lookups |
| `RADARR_URL` / `RADARR_API_KEY` | no | — | Enables quality profile lookups |
| `WEBHOOK_DELAY` | no | `30s` | Wait before scanning (e.g. `30s`, `5m`) |
| `USE_RCLONE` | no | `false` | Enable rclone VFS cache clearing |
| `TZ` | no | `UTC` | IANA timezone name |
| `NOTIFY_URLS` | no | — | Notification targets, comma/newline separated — any Apprise URL, or a plain JSON webhook |
| `NOTIFY_ON` | no | `error` | Which results to send: `error`, `all`, or `off` |
| `ONBOARD_WIKI_URL` | no | — | Link to setup/wiki shown on invite onboard page |
| `ONBOARD_REQUEST_URL` | no | — | Link to content request site shown on invite onboard page |

Full reference in README.md.

## Project Structure

```
media-servarr-sync.py   Main application (Flask app + worker thread)
requirements.txt        Python dependencies
Dockerfile              Python 3.14-slim image, non-root appuser (uid 1000)
compose.yaml            Docker Compose config
.env.example            Environment variable template
templates/
  login.html            Login page
  onboarding.html        First-run setup wizard (admin password + connect Plex), shown instead of login.html until PLEX_TOKEN is configured
  settings.html          Settings page — view/edit config, Plex server discovery, grab-token button
  manual_ui.html        Outer shell (header, PJAX script, nav); also serves the Sync tab page-content,
                        which includes Now Playing (active Plex streams + geolocation maps),
                        Server Stats, and the full-library-scan picker
  invites.html          Invite management page (create / revoke invite links and grants)
  invite_onboard.html   Public invite acceptance flow (/invite/<token> and /invite/<token>/accept) — unrelated to onboarding.html, which is the admin first-run setup
```

## Key Internals

- **`SyncTask`** — dataclass for a queued scan task
- **`SyncHistory`** — SQLite3 history at `/data/history.db`; handles dedup and cooldown
- **`SettingsStore`** — SQLite3 key/value store at `/data/settings.db`. `load_config()` resolves each config value as env var → DB setting → default, and is re-run after a Settings page save to hot-reload without a restart. Fields pinned by an env var are locked (read-only) in the Settings UI.
- **Background worker** (`sync_worker`) — drains the queue with configurable `WEBHOOK_DELAY`
- **Notifications** — `notify_sync_result()` is called from `sync_worker` with the same dict written to history. It honours `NOTIFY_ON` and dispatches `send_notification()` on a daemon thread so webhook latency never blocks a scan. `send_notification()` splits `NOTIFY_URLS` via `parse_notify_urls()` and delivers each target independently, returning ok if *any* succeeded. Per target: `_send_via_apprise()` first (Apprise covers 100+ services and also parses raw Discord/Slack/ntfy webhook URLs); it returns `handled=False` for anything Apprise can't parse, which falls through to `_send_via_http()` — that path keeps `_notify_provider()` / `_notification_payload()` for bare Gotify URLs and generic structured-JSON webhooks, and is the whole path when `apprise` isn't installed (the import is optional). Every target is passed through `redact_notify_url()` before it reaches a log or the UI, since these URLs embed tokens. The Apprise logger is set to CRITICAL: probing whether it handles a target logs "Unparseable URL" at ERROR for every plain webhook, which is expected here. The URL notified always comes from validated config, never a request body — the Settings form's `action=save_and_test` button saves first, then notifies
- **Settings validation** — `_validate_setting()` checks every submitted field (`json`, `int`, `duration`, `choice`) *before* anything is written, so a save is all-or-nothing. `SETTINGS_CHOICES` supplies the options for `choice` fields
- **Path prefix matching** — `path_has_prefix()` is the only correct way to test a path against a `SECTION_MAPPING` / `PATH_REPLACEMENTS` key; a bare `str.startswith` matches mid-segment (`/mnt/media/tv` vs `/mnt/media/tv4k`). Use `safe_int()` for anything user-supplied that must be an integer — `load_config()` runs at import, so a raising conversion there prevents startup entirely
- **Deduplication** — duplicate webhooks for the same folder are merged while a task is in-flight
- **Quality/custom format caching** — fetched from Sonarr/Radarr API, refreshed every 6 hours
- **PJAX navigation** — nav-link clicks swap only `#page-content` and `#page-style` in-place; `manual_ui.html` is the persistent outer shell and all other page templates supply only their inner content block. Cleanup callbacks registered as `window.__pjaxCleanup` are called before each swap.
- **Sync-tab-only chrome** — the tag legend (`.legend`) and back-to-top button (`#back-to-top`) are only relevant on the Sync tab; the PJAX handler hides them on navigation away and re-injects them from the fetched HTML when returning to `/` if they were never in the DOM.

### Flask Routes

| Route | Method | Auth | Purpose |
|---|---|---|---|
| `/webhook/sonarr` | POST | none (CSRF exempt) | Sonarr webhook receiver |
| `/webhook/radarr` | POST | none (CSRF exempt) | Radarr webhook receiver |
| `/` | GET/POST | session | Sync tab — manual scan UI + history |
| `/invites` | GET | session | Invite management tab |
| `/invites/create` | POST | session | Create a new invite link |
| `/invites/revoke/<token>` | POST | session | Revoke an invite link |
| `/invites/revoke_grant/<id>` | POST | session | Revoke an accepted grant |
| `/invite/<token>` | GET | none | Public invite landing page |
| `/invite/<token>/accept` | POST | none | Accept an invite (adds Plex friend) |
| `/onboarding` | GET/POST | none | First-run setup wizard (admin password + connect Plex), shown instead of login.html until PLEX_TOKEN is configured |
| `/login` | GET/POST | — | Login page |
| `/auth/plex/start` | POST | none (CSRF exempt) | Create a Plex.tv PIN, return the auth URL for "Sign in with Plex" |
| `/auth/plex/poll` | GET | none | Poll a Plex.tv PIN; logs the session in once claimed |
| `/logout` | GET | session | Logout |
| `/settings` | GET/POST | session | Settings page — view/edit DB-backed config not pinned by env vars |
| `/api/plex/discover` | POST | session (CSRF exempt) | List Plex servers tied to the account behind a token |
| `/api/plex/token/poll` | GET | session | Poll a Plex.tv PIN and return the raw token (Settings page "grab token" button) |
| `/health` | GET | none | Health check (Plex, rclone, queue depth) |
| `/api/stats` | GET | none | Aggregate stats (Homepage widget) |
| `/api/sessions` | GET | session | Raw Plex session data for Now Playing |
| `/api/scan/library` | POST | session (CSRF exempt) | Trigger a full Plex library section scan |
| `/api/libraries` | GET | session | List Plex library sections |
| `/api/geoip` | GET | session | Server-side IP geolocation proxy (cached) |
| `/api/maptile/<z>/<x>/<y>.png` | GET | session | Proxy + 24 h disk cache for OpenStreetMap tiles |
| `/api/server-stats` | GET | session | Plex server CPU / RAM / bandwidth stats |
| `/api/plex-update` | GET | session | Whether a Plex Media Server update is available (cached 1 h) |
| `/api/thumb` | GET | session | Proxy Plex artwork thumbnails |

## Skipped Webhook Events

Delete events are intentionally skipped — upgrades are handled by the subsequent `Download` event:

```python
_SKIP = {'Grab', 'EpisodeFileDelete', 'EpisodeFileDeleted', 'SeriesDelete',
         'MovieFileDelete', 'MovieFileDeleted', 'MovieDelete'}
```

## Dependencies

```bash
pip install -r requirements.txt
# flask, flask-wtf, python-dotenv, requests, PlexAPI
```

## Git Workflow

**Base branch: `dev`** — all changes must be branched off `dev` and PRs target `dev`. Never branch off or PR directly to `master`.

Before making any changes, always:

1. Check if the current working branch (if one exists from a previous session) has any open PRs
   - If it has open PRs, continue making changes on that branch (do not delete it)
   - If it has no open PRs, delete it and create a fresh branch based off `dev`
2. Create a fresh branch based off `dev` (only if the old branch was deleted)
3. Then make your changes on the new branch

```bash
git checkout dev
git pull origin dev

# Check for open PRs before deleting the old branch
gh pr list --head <old-branch> --state open
# If no open PRs:
git branch -D <old-branch>   # delete old branch
git checkout -b claude/<feature-name>
# If open PRs exist:
git checkout <old-branch>    # continue working on the existing branch
```

This ensures changes are always based on the latest `dev` and avoids stale branch state, while preserving branches that have open PRs under review.

### Branch Naming

```
claude/<feature-name>   AI-authored feature/refactor branches
fix/<description>       Bug fixes
hotfix/<description>    Critical production fixes (branch from master)
```

### Commit Messages

Use [Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <subject>
```

| Type | Use for |
|---|---|
| `feat` | New feature |
| `fix` | Bug fix |
| `refactor` | Code change that isn't a fix or feature |
| `chore` | Maintenance, deps, release housekeeping |
| `docs` | Documentation only |

Examples:
```
feat(invites): add time-limited access expiry
fix(webhook): handle missing episodeFile on rename events
refactor: extract _sleep_interruptible helper
chore: release v0.24.0
```

- Subject line: imperative mood, no period, ≤72 chars
- Body (optional): explain *why*, not *what*
- Footer: `Closes #123` or breaking change note

### Pull Requests

- Title matches commit message format: `type(scope): description`
- Target branch: always `dev`
- Body: bullet summary of what changed + manual test checklist

## No Tests

There is no automated test suite. Validate changes manually via the `/health` endpoint and the web UI, or by firing test webhooks from Sonarr/Radarr.

## Docker

```bash
docker build -t media-servarr-sync .
docker compose up -d
```

- Non-root user `appuser` (uid 1000)
- Data volume: `media-servarr-sync-data:/data`
- Healthcheck: `GET /health` every 30s

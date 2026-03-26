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
  manual_ui.html        Outer shell (header, PJAX script, nav); also serves the Sync tab page-content
  now_playing.html      Now Playing page (active Plex streams, geolocation maps, library scan)
  invites.html          Invite management page (create / revoke invite links and grants)
  invite_onboard.html   Public invite acceptance flow (/invite/<token> and /invite/<token>/accept)
```

## Key Internals

- **`SyncTask`** — dataclass for a queued scan task
- **`SyncHistory`** — SQLite3 history at `/data/history.db`; handles dedup and cooldown
- **Background worker** (`sync_worker`) — drains the queue with configurable `WEBHOOK_DELAY`
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
| `/now-playing` | GET | session | Now Playing tab — active Plex streams |
| `/invites` | GET | session | Invite management tab |
| `/invites/create` | POST | session | Create a new invite link |
| `/invites/revoke/<token>` | POST | session | Revoke an invite link |
| `/invites/revoke_grant/<id>` | POST | session | Revoke an accepted grant |
| `/invite/<token>` | GET | none | Public invite landing page |
| `/invite/<token>/accept` | POST | none | Accept an invite (adds Plex friend) |
| `/login` | GET/POST | — | Login page |
| `/logout` | GET | session | Logout |
| `/health` | GET | none | Health check (Plex, rclone, queue depth) |
| `/api/stats` | GET | none | Aggregate stats (Homepage widget) |
| `/api/sessions` | GET | session | Raw Plex session data for Now Playing |
| `/api/scan/library` | POST | session (CSRF exempt) | Trigger a full Plex library section scan |
| `/api/libraries` | GET | session | List Plex library sections |
| `/api/geoip` | GET | session | Server-side IP geolocation proxy (cached) |
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

Before making any changes, always:

1. Check if the current working branch (if one exists from a previous session) has any open PRs
   - If it has open PRs, continue making changes on that branch (do not delete it)
   - If it has no open PRs, delete it and create a fresh branch based off `master`
2. Create a fresh branch based off `master` (only if the old branch was deleted)
3. Then make your changes on the new branch

```bash
git checkout master
git pull origin master

# Check for open PRs before deleting the old branch
gh pr list --head <old-branch> --state open
# If no open PRs:
git branch -D <old-branch>   # delete old branch
git checkout -b claude/<feature-name>
# If open PRs exist:
git checkout <old-branch>    # continue working on the existing branch
```

This ensures changes are always based on the latest master and avoids stale branch state, while preserving branches that have open PRs under review.

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

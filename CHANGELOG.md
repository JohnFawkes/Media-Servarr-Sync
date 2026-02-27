# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] → v1.0.0

### Added
- Logo SVG now displayed in the WebUI main dashboard header and login card, replacing the plain dot placeholder
- CSS `drop-shadow` glow applied to the logo icon for visual consistency with the accent colour

### Fixed
- Multi-episode deduplication: when Sonarr fires 3+ rapid webhooks for separate episodes that all map to the same show folder, the dedup logic now **merges** the episode counts instead of silently discarding duplicates. The history record will correctly show e.g. `3 episodes` rather than only the first episode's filename. Works for any mix of single-episode and batch payloads being deduplicated into one scan.

---

## [v1.0.0] - TBD

### Added
- **Episode display in sync history** — Sonarr webhook events now surface episode metadata in the recent syncs list:
  - Single episode imports show the episode filename (e.g. `Show.S01E01.mkv`)
  - Season pack / batch imports show a styled badge (e.g. `12 episodes`)
  - Rename events also extract and display episode information
- **Support for all Sonarr episode payload variants** — correctly handles `episodeFile` (single download), `episodeFiles` (season pack / batch), and `renamedEpisodeFiles` (rename events)
- **Episode count badge** — golden badge rendered in the history UI for multi-episode imports to distinguish packs from single files at a glance
- **SVG favicon** — inline data-URI SVG favicon served in both the dashboard and login templates; no external file dependency
- **SVG logo** — `logo.svg` added to the repository and displayed in the README header
- **Login / logout authentication** — session-based auth protects the manual trigger dashboard; credentials set via `MANUAL_USER` / `MANUAL_PASS` environment variables
- **Timezone support** — timestamps in the sync history UI reflect the local timezone of the container
- **`PLEXAPI_HEADER_IDENTIFIER` support** — allows overriding the Plex client identifier header for API requests
- **Sync history with configurable retention** — SQLite-backed history (`/data/sync_history.db`) with per-page pagination; retention period controlled by `HISTORY_DAYS` environment variable
- **Health check endpoint** (`/health`) — returns JSON with Plex connectivity status, rclone toggle state, queue depth, and worker thread status; responds HTTP 207 when degraded
- **Plex timeout retry logic** — Plex scan and metadata analysis attempts are retried up to 3 times with a 10 s back-off on timeout errors; connection is invalidated and re-established between retries
- **Rclone VFS cache support** — optional rclone remote-control integration (`USE_RCLONE`, `RCLONE_RC_URL`) to issue VFS forget + async refresh before triggering a Plex scan
- **Minimum file age check** (`MINIMUM_AGE`) — worker can wait until a newly-downloaded file has settled on disk before scanning
- **Configurable webhook delay** (`WEBHOOK_DELAY`) — optional pause between receiving a webhook and starting the Plex scan, useful when Sonarr is still writing files
- **Deduplication** — in-flight set prevents the same folder from being queued multiple times simultaneously; rapid duplicate webhooks are collapsed into a single scan
- **Rclone host-path decoupling** — `RCLONE_PATH_REPLACEMENTS` environment variable allows the path sent to rclone's VFS refresh to differ from the Plex library path, supporting setups where the container mount target differs from the host path seen by rclone
- **Docker health check** in `Dockerfile` hitting the `/health` endpoint at 30 s intervals
- **JSON log rotation** in `compose.yaml` (10 MB max, 3 files retained)
- **Renovate bot** configuration for automated dependency updates

### Changed
- **Web UI served from `/`** instead of the previous `/webhook/manual` path — the root URL now shows the dashboard directly
- **Sonarr scans the show root folder** — after initially scanning the season subfolder, behaviour was corrected to always scan the series root so Plex can associate new episodes with the existing show entry
- **Full project rebrand** to Media Servarr Sync
- **"Redo Everything"** — significant internal rewrite for a cleaner architecture: dataclass-based `SyncTask`, thread-safe `SyncHistory` class, lazy Plex singleton with reconnect, proper queue worker with shutdown event
- Upgraded base Docker image to Python 3.14 slim
- Upgraded Flask to 3.1.3

### Fixed
- Dockerfile Python script name and path corrections post-rebrand
- Double reload issue in Docker entrypoint
- Various GitHub Actions workflow fixes (trigger file, path filters, push configuration)

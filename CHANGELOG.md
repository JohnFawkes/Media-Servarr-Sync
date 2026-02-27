# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased] → v1.0.0

### Added
- Logo SVG now displayed in the WebUI main dashboard header and login card, replacing the plain dot placeholder
- CSS `drop-shadow` glow applied to the logo icon for visual consistency with the accent colour

### Fixed
- Multi-episode deduplication: when Sonarr fires 3+ rapid webhooks for separate episodes that all map to the same show folder, the dedup logic now merges the episode counts instead of silently discarding duplicates — the history record correctly shows e.g. `3 episodes` rather than only the first episode's filename

---

## [v1.0.0] - TBD

### 2026-02-27

#### Added
- Episode count badge rendered in the sync history UI for season pack / multi-episode imports to distinguish them from single-file downloads at a glance
- Support for `RCLONE_PATH_REPLACEMENTS` — decouples the path sent to rclone VFS refresh from the Plex library path, supporting setups where the rclone remote host path differs from the container mount target
- Handle all three Sonarr episode payload variants: `episodeFile` (single download), `episodeFiles` (season pack / batch), and `renamedEpisodeFiles` (rename events)
- SVG favicon embedded as an inline data-URI in both the dashboard and login templates — no external file dependency
- `logo.svg` added to the repository and displayed in the README header

### 2026-02-25

#### Added
- Episode filename shown in the recent syncs history list for Sonarr events (single episode shows filename, batch shows count)

#### Changed
- Web UI moved from `/webhook/manual` to the root path `/` — the dashboard is now accessible directly at the container URL

#### Fixed
- Sonarr webhooks now always scan the show root folder rather than the season subfolder, ensuring Plex correctly associates new episodes with the existing show entry

### 2026-02-22

#### Changed
- Sonarr scan path changed from show root to season subfolder (later corrected on 2026-02-25)

### 2026-02-20

#### Changed
- Upgraded Flask to 3.1.3

### 2026-02-15

#### Added
- Proper health check endpoint (`/health`) returning JSON with Plex connectivity, rclone state, queue depth, and worker thread status; responds HTTP 207 when degraded
- Sync history retention configurable via `HISTORY_DAYS` environment variable

#### Changed
- Project rebranded to **Media Servarr Sync**
- Upgraded base Docker image to Python 3.14 slim

#### Fixed
- Dockerfile script name and path corrections post-rebrand
- Removed stale path info from script output

### 2026-02-13

#### Added
- Login / logout authentication protecting the manual trigger dashboard; credentials configured via `MANUAL_USER` and `MANUAL_PASS` environment variables
- Timezone-aware timestamps throughout the sync history UI
- `PLEXAPI_HEADER_IDENTIFIER` environment variable to override the Plex client identifier sent with API requests
- GitHub Actions CI/CD workflows for Docker image publishing
- LICENSE file (MIT)
- Renovate bot configuration for automated dependency updates
- GitHub Sponsors funding configuration

#### Changed
- Upgraded `actions/checkout` to v6, `docker/build-push-action` to v6, `sigstore/cosign-installer` to v4

### 2026-02-12

#### Added
- Initial project push with core webhook receiver for Sonarr and Radarr
- Plex library scan triggered on webhook receipt via PlexAPI
- Plex timeout handling with up to 3 retries and 10 s back-off; connection invalidated and re-established between attempts
- Metadata analysis (`item.analyze()`) after successful scan to update Plex match data
- Thread-safe task queue (`sync_queue`) with a background worker daemon thread
- In-flight deduplication — prevents the same folder being queued more than once simultaneously
- SQLite-backed sync history with per-page pagination in the UI
- Optional rclone VFS cache integration (`USE_RCLONE`, `RCLONE_RC_URL`) to issue VFS forget + async refresh before scanning
- Configurable webhook delay (`WEBHOOK_DELAY`) to allow Sonarr time to finish writing before scanning
- Minimum file age check (`MINIMUM_AGE`) to ensure files have settled on disk before scanning
- Path replacement mappings for Plex paths, rclone paths, and library section IDs via environment variables
- Docker Compose configuration with JSON log rotation (10 MB max, 3 files) and persistent data volume
- Docker health check hitting `/health` at 30 s intervals
- `requirements.txt` pinning Flask, PlexAPI, requests, and python-dotenv

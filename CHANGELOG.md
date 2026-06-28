# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

### Security
- **Map tile disk cache** — the `/api/maptile` proxy now writes validated tiles to a 24-hour disk cache (`/data/tile_cache/`) and serves subsequent requests directly from the filesystem. Upstream content (derived from user-supplied tile coordinates) is never forwarded directly to the HTTP response, fully eliminating the reflected-content vector (CodeQL CWE-79 / alert #14). PNG magic-byte validation (`\x89PNG\r\n\x1a\n`) is enforced before any tile is written to cache.

---

## [v0.21.1] - 2026-06-28

### Security
- **Map tile proxy content-type validation** — the `/api/maptile` route now rejects upstream responses whose `Content-Type` is not `image/*` and sets `X-Content-Type-Options: nosniff` on all tile responses, preventing a compromised or MITM'd upstream from injecting HTML/JS into a proxied response (CodeQL CWE-79 / alert #14).

---

## [v0.21.0] - 2026-06-28

### Added
- **Plex server stats on Now Playing page** — a new Server Stats card shows live rolling line charts for CPU % and RAM %, each with two lines: **System** (host) and **Plex process**. Charts poll `/statistics/resources` every 10 s and display a 5-minute / 30-point rolling window. Current LAN, WAN, and total bandwidth from `/statistics/bandwidth` is shown below the charts. Chart.js 4.4.4 is bundled locally under `static/chart.min.js` (no CDN dependency). A new `/api/server-stats` route (session-authenticated) proxies both Plex endpoints.

### Fixed
- **HW Transcode now labelled correctly** — streams that use hardware transcoding (where Plex sets `transcodeHwEncoding` or `transcodeHwRequested`) now display as **HW Transcode** instead of the generic **Transcode** in the Now Playing stream quality row.
- **Audio transcode now shown separately** — when video and audio decisions differ (e.g. video Direct Stream + audio Transcode), the quality row now shows both, e.g. `Direct Stream · Audio Transcode`, matching the Plex dashboard behaviour.
- **Server stats card blank after PJAX navigation** — when `manual_ui.html` was the outer shell (e.g. after a hard refresh on the Sync tab), navigating to Now Playing via PJAX never loaded `chart.min.js`, so `new Chart()` failed silently. Fixed by dynamically loading `chart.min.js` in the `page-init` script when `Chart` is not yet defined.
- **Tag legend missing after navigating from Now Playing → Sync** — `now_playing.html`'s PJAX handler lacked the legend/back-to-top hide-and-restore logic present in `manual_ui.html`. Added the same show/hide behaviour so the legend reappears correctly on every return to the Sync tab.
- **Server stats chart lines only visible after first 10-second poll** — Chart.js draws nothing for a single data point when `pointRadius` is 0 (a line segment requires 2 points). Charts are now seeded with a `null` placeholder on init so the first real fetch immediately produces a visible line.

---

## [v0.20.1] - 2026-06-27

### Fixed
- **Per-episode tags now render for batch downloads** — when Sonarr sends a single webhook with multiple files in `episodeFiles` or `renamedEpisodeFiles`, each file's quality is now stored individually in the rich episode object rather than being merged into a flat tag row. Custom formats from the same release are shared across all files in the batch.
- **Episode badge tooltip shows per-file quality and format tags** — hovering the yellow episode-count badge now displays each filename alongside its own quality and custom-format pills inside the popup, instead of expanding into separate rows below the folder path. The flat merged tag row below the folder is unchanged.
- **Single-episode rich records no longer show as a tooltip badge** — a template condition bug caused single-file rich episode records to render the dict repr inside the tooltip; they now display as a plain inline filename as intended.
- **Tag legend redesigned** — wider panel, larger text, a divider, and a live example `2 episodes` badge above the tooltip hint so users immediately know what to hover.

---

## [v0.20.0] - 2026-06-27

### Added
- **Automated cleanup of untagged container versions** — a new workflow deletes untagged GHCR image layers after each release build and on a weekly schedule, keeping the package registry tidy.
- **CHANGELOG sync from master back to dev after release** — the release workflow now cherry-picks the CHANGELOG promotion commit back onto `dev` immediately after stamping a version on `master`, so `dev` never retains stale `[Unreleased]` content from a previous release.

### Fixed
- **Docker build warnings silenced** — `DEBIAN_FRONTEND=noninteractive` suppresses debconf frontend noise during `apt-get`; pip dependencies are now installed into a virtualenv (`/app/.venv`) rather than the system Python, eliminating the root-user pip warning and properly isolating app dependencies.
- **`workflow_dispatch` now triggers CHANGELOG promotion and release creation** — the promote step and all release steps were conditioned on `github.event_name == 'push'` only, so manually-triggered runs skipped the entire release flow. Added `|| github.event_name == 'workflow_dispatch'` to all six conditions.

---

## [v0.19.0] - 2026-06-27

### Added
- **Per-episode quality and format tags in sync history** — when multiple episodes for the same show folder are deduplicated into one sync entry, each file now shows its own quality and custom-format badges beneath its filename. Differences between files (e.g. dual-audio vs Japanese-only, different quality tiers) are immediately visible rather than being merged into a single flat tag row. Older history records continue to render as before.

---

## [v0.18.0] - 2026-06-27

### Fixed
- **Movie filename missing from sync history** — Radarr webhooks were not storing the movie filename in history, so the sync entry showed only the folder path. The filename is now extracted from `movieFile.relativePath` and displayed below the path, consistent with Sonarr episode entries.

---

## [v0.17.0] - 2026-06-27

### Added
- **PR auto-labeling** — PRs touching Docker-related files are automatically labelled `docker`; labelled PRs trigger a Docker image build and post a comment with the image tag for testing.

### Fixed
- Back-to-top button flash on PJAX navigation — the PJAX handler now hides `#back-to-top` before swapping the stylesheet, eliminating the brief unstyled flash.
- Self-hosted IBM Plex fonts — Google Fonts `@import` replaced with locally served woff2 files under `/static/fonts/`, eliminating load failures behind reverse proxies or bot-protection layers (e.g. Anubis).
- Replace CDN Leaflet with local `/static/` bundle in `invites.html`.
- Resolve label accessibility issues and remove unused Leaflet import from the Invites page.
- Remove manifest link tags to stop Anubis-caused console spam.
- Add `pull-requests: write` permission for the PR image-comment step in CI.

---

## [v0.16.3] - 2026-06-27

### Fixed
- Bundle Leaflet JS and CSS locally to fix blank map boxes on the Now Playing page.

---

## [v0.16.2] - 2026-06-27

### Fixed
- Restore correct `ipinfo.io` request URL — `urllib.parse.quote()` was percent-encoding dots in IPv4 addresses, breaking all geolocation lookups.

---

## [v0.16.1] - 2026-06-26

### Fixed
- Block all non-global IP addresses in `/api/geoip` (loopback, private, link-local, multicast) and construct the upstream URL safely to prevent SSRF.
- Remove sync history from the unauthenticated `/health` endpoint to prevent path disclosure.

### Changed
- Dependency update: `actions/checkout` → v7.

---

## [v0.16.0] - 2026-05-26

### Added
- **PWA support** — the app ships a web app manifest so it can be installed to the home screen on Android and iOS like a native app.

### Changed
- Dependency updates: `sigstore/cosign-installer` → v4.1.2; `aquasecurity/trivy-action` → v0.36.0; `softprops/action-gh-release` → v3.

---

## [v0.15.10] - 2026-05-15

### Changed
- Dependency update: `requests` → v2.34.2.

---

## [v0.15.9] - 2026-05-14

### Changed
- Dependency update: `requests` → v2.34.1.

---

## [v0.15.8] - 2026-05-12

### Changed
- Dependency update: `requests` → v2.34.0.

---

## [v0.15.7] - 2026-04-26

### Fixed
- FD exhaustion crash — file descriptors leaked on connection errors; fixed with explicit `response.close()` calls.
- GeoIP requests upgraded from HTTP to HTTPS.

---

## [v0.15.6] - 2026-04-24

### Changed
- Dependency update: `flask-wtf` → v1.3.0.

---

## [v0.15.5] - 2026-03-31

### Changed
- Dependency update: `requests` → v2.33.1.

---

## [v0.15.4] - 2026-03-26

### Changed
- Minor internal adjustments (no user-facing changes).

---

## [v0.15.3] - 2026-03-26

### Changed
- Dependency update: `sigstore/cosign-installer` → v4.1.1.

---

## [v0.15.2] - 2026-03-26

### Changed
- Dependency update: `requests` → v2.33.0.

---

## [v0.15.1] - 2026-03-24

### Fixed
- Resolve semgrep CSRF, SSRF, and NaN-injection findings in the invite creation form.
- Swap `#page-style` on PJAX navigation to prevent CSS bleed between tabs.

---

## [v0.15.0] - 2026-03-23

### Added
- **Persistent PJAX outer shell** — `manual_ui.html` becomes the persistent outer shell; nav clicks swap only `#page-content` and `#page-style` without a full reload. Now Playing is included in the nav bar.
- Library scan trigger moved from the dashboard to the Now Playing page.

---

## [v0.14.2] - 2026-03-23

### Fixed
- Set map container height inline and call `map.invalidateSize()` after PJAX injection so Leaflet tiles render correctly.

---

## [v0.14.1] - 2026-03-23

### Fixed
- Pass `plex_url` to the `now_playing` template so thumbnail proxy URLs are constructed correctly.

---

## [v0.14.0] - 2026-03-23

### Added
- Now Playing page enhancements — larger map, display of both local and remote IP addresses, full library scan trigger, fix for remote session pause-state detection.

---

## [v0.13.9] - 2026-03-23

### Fixed
- Read `remotePublicAddress` from the raw Plex XML attribute rather than a parsed property, fixing blank geo maps for remote players.

---

## [v0.13.8] - 2026-03-23

### Fixed
- Merge quality values across deduplicated webhook events so multi-episode imports show all unique qualities.
- Prefer the remote public IP over the local IP when looking up geolocation for the Now Playing map.

---

## [v0.13.7] - 2026-03-23

### Security
- Replace `send_file` with `make_response` in the thumbnail proxy to eliminate the path-injection taint flow flagged by CodeQL.

---

## [v0.13.6] - 2026-03-23

### Security
- Whitelist allowed content-types in the thumbnail proxy to break the path-taint chain detected by static analysis.

---

## [v0.13.5] - 2026-03-23

### Security
- Serve thumbnail proxy responses via `send_file` to eliminate the XSS sink flagged by CodeQL.

---

## [v0.13.4] - 2026-03-23

### Security
- Validate image magic bytes in the thumbnail proxy before serving the response.

---

## [v0.13.3] - 2026-03-23

### Fixed
- Sanitize the thumbnail cache key to safe path characters only, preventing path traversal.
- Show all unique quality values when a multi-episode import contains files of different qualities.

---

## [v0.13.2] - 2026-03-23

### Fixed
- Catch curl errors in the Docker health check retry loop so transient failures do not abort the check.

### Changed
- Dependency updates: `actions/upload-artifact` → v7; `plexapi` → v4.18.1.

---

## [v0.13.1] - 2026-03-20

### Changed
- Minor internal CI adjustments (no user-facing changes).

---

## [v0.13.0] - 2026-03-20

### Added
- **Demo mode** (`DEMO_MODE=true`) — runs the app with synthetic data so the UI can be explored without a live Plex server.
- **Now Playing page** (`/now-playing`) — active Plex streams with player info, thumbnail, progress bar, stream quality, and an interactive Leaflet geo-map. Local-network IPs show a "Local Network" indicator.
- **Invite management** (`/invites`) — create time-limited Plex invite links; configure library sections, sync/channels permissions, home-user flag, max uses, and link/access expiry. Public acceptance flow at `/invite/<token>`.

### Fixed
- Restore full invite system that had been lost in a prior refactor.

---

## [v0.12.2] - 2026-03-20

### Changed
- Minor internal adjustments (no user-facing changes).

---

## [v0.12.1] - 2026-03-20

### Changed
- Minor internal adjustments (no user-facing changes).

---

## [v0.12.0] - 2026-03-20

### Added
- **Top pagination bar** — a second set of pagination controls above the sync history list so you can jump pages without scrolling to the bottom.
- **First / Last page buttons** and a **jump-to-page** number input alongside the standard Previous / Next controls.
- **Back-to-top button** — a fixed `↑ Top` button appears on the Sync tab after scrolling down; hidden on other tabs.
- **Clickable logo** — the header logo links back to the Sync tab home page.

---

## [v0.11.4] - 2026-03-19

### Fixed
- Episode hover tooltip width uses `max-content` so long filenames are never clipped.

---

## [v0.11.3] - 2026-03-19

### Fixed
- Widen episode hover tooltip and display one filename per line for easier reading.

---

## [v0.11.2] - 2026-03-19

### Fixed
- Widen tag legend box to 180 px to prevent text overflow on longer label text.

---

## [v0.11.1] - 2026-03-19

### Fixed
- Remove truncation ellipsis from the episode hover tooltip so every filename is fully visible.

---

## [v0.11.0] - 2026-03-26

### Added
- **PJAX single-page navigation** — clicking nav tabs (Sync, Now Playing, Invites) swaps only `#page-content` and `#page-style` without a full page reload, preserving scroll position and avoiding flash-of-unstyled-content between tabs.
- **Now Playing page** (`/now-playing`) — dedicated page showing all active Plex streams with player info, media thumbnail, progress bar, stream quality, and an interactive Leaflet map showing the player's approximate geolocation. Local-network IPs show a "Local Network" indicator instead of a map.
- **Full library scan from Now Playing UI** — a library selector on the Now Playing page lets you trigger a full Plex section scan directly from the UI without navigating away.
- **Invite management** (`/invites`) — create and manage time-limited Plex invite links; configure allowed library sections, sync/channels permissions, home-user flag, max uses, and link/access expiry. Invite acceptance flow at `/invite/<token>` walks the user through confirming their Plex username.

### Fixed
- **Tab-aware chrome** — the tag legend and back-to-top button are now shown only on the Sync tab. When navigating to a non-Sync tab they are hidden; when navigating back to Sync from a page that never had them in the DOM they are injected from the fetched HTML.
- **Auto-refresh preserved across tab switches** — PJAX navigation now properly cleans up and re-initialises page timers so the sync history countdown and Now Playing poll continue after switching tabs.

### Security
- **Information exposure through exception (CodeQL)** — two API endpoints (`/api/scan/library`, `/api/libraries`) previously returned raw `str(exc)` in JSON error responses, leaking internal Plex details. Errors are now logged server-side and a generic message is returned to the client.
- **NaN injection / boolean coercion (semgrep)** — invite creation form fields (`allow_sync`, `allow_channels`, `home_user`) now use `'field' in request.form` instead of `bool(request.form.get('field'))`, preventing a non-empty string like `"false"` from being coerced to `True`.
- **CSRF false positive suppression (semgrep)** — `invite_onboard.html` form action switched to `url_for()` and annotated with `nosemgrep: django-no-csrf-token`; Flask-WTF `CSRFProtect` is active globally and the token is present on the very next line.

### Changed
- Dependency update: `requests` → v2.33.0.
- CI: `sigstore/cosign-installer` action updated to v4.1.1.

---

## [v0.10.0] - 2026-03-18

### Added
- **Custom formats fetched from arr API** — custom formats are now retrieved directly from the Sonarr/Radarr `/episodefile/{id}` or `/moviefile/{id}` endpoint instead of relying on the webhook payload, which can be incomplete or stale.

### Fixed
- **Waitress WSGI server** — replaced the Flask development server with [waitress](https://docs.pylonsproject.org/projects/waitress/) for production use.
- **`EpisodeFileDelete` / `MovieFileDelete` skipping** — the event names in `_SKIP` were incorrect (`EpisodeFileDelete` instead of `EpisodeFileDeleted`); events are now skipped as intended.
- **Custom format tag tooltip** — hovering a custom format (purple) tag now shows a `"Format"` tooltip label, consistent with Quality and Profile tags.
- **Episode tooltip truncation** — filenames in the multi-episode hover tooltip no longer truncate with an ellipsis; they wrap fully so every filename is readable regardless of length.

### Changed
- Raw `quality` and `customFormats` fields from every incoming Sonarr/Radarr webhook are now logged at `INFO` level, making it easier to diagnose mismatches between what the arr sends and what gets stored.
- CI: `templates/**` added to the Docker workflow path filter so image builds are triggered when template files change.

### Security
- **Reflected XSS (CodeQL #2/#3)** — webhook responses no longer echo user-supplied data (`path`, `eventType`) back in the JSON body, eliminating the taint flows flagged by CodeQL.
- **Open redirect (login)** — the post-login `next` redirect is now replaced entirely with a static `redirect(url_for('manual_webhook'))`, removing all user-controlled data from the call to `redirect()`.
- **CSRF protection** — replaced the invalid Django-style `{% csrf_token %}` tag with proper Flask-WTF CSRF token injection (`{{ csrf_token() }}`) in both the login and manual UI forms.
- **SSTI prevention** — inline template strings moved to files under `templates/` and rendered via `render_template()`, eliminating server-side template injection risk.
- **SQL injection prevention** — removed f-string interpolation in raw SQL queries; all dynamic values now use parameterised query placeholders.
- **Pip CVE-2026-1703** — base Docker image pins `pip >= 26.0` to address the path traversal vulnerability.

---

## [v0.9.0] - 2026-03-14

### Added
- **Tag colour legend** — a fixed panel on the left side of the web UI lists the three tag colours and what they mean: green = Quality Profile, blue = Quality, purple = Custom Format. The legend is hidden automatically on narrow viewports where it would overlap the main content.

### Security
- **Open redirect hardening** — the post-login `next` redirect now additionally rejects URLs with an explicit scheme (e.g. `javascript:`, `data:`, `http:`) and protocol-relative URLs (e.g. `//evil.com`). Only strict relative paths beginning with `/` are accepted; everything else falls back to the default page.

---

## [v0.8.0] - 2026-03-07

### Added
- **Hoverable quality and profile tags** — hovering a quality or profile tag shows a small tooltip label (`"Quality"` / `"Profile"`) so the purpose of each tag is immediately clear without needing to know the colour convention.
- **Filterable quality and profile tags** — clicking any quality or profile tag filters the sync history to entries matching that exact value. The active tag is highlighted with a colour-matched glow ring. Active filters appear as dismissible `×` pills in the filter bar alongside the status pills. All active filters (search, status, quality, profile) are preserved across pagination and search-form submits.

### Fixed
- Tag tooltips now use a dark background (`#1a1a2e`) with light text for legibility against all tag colours.

---

## [v0.7.0] - 2026-03-07

### Added
- **Event type in webhook logs** — the `eventType` field from each Sonarr/Radarr webhook is included in the processing log line for easier debugging.

### Fixed
- Sonarr and Radarr API credentials are validated at startup and their configured/missing state is logged, so misconfigured quality-profile lookups are immediately visible.
- Removed a spurious double border line above the pagination buttons in the sync history UI.

---

## [v0.6.0] - 2026-03-07

### Added
- **Quality profile badges** — the quality profile name (e.g. `HD Quality`, `Any`) is fetched from the Sonarr/Radarr API using the profile ID from the webhook and displayed as a green tag alongside quality and custom-format tags. Profile ID→name mapping is cached in memory to avoid redundant API calls.

---

## [v0.5.0] - 2026-03-05

### Added
- **Auto-refresh with countdown** — the sync history panel refreshes automatically every 30 seconds. A live countdown timer shows seconds until the next refresh and a manual `↻` button triggers an immediate update.

---

## [v0.4.0] - 2026-03-05

### Added
- **Quality and custom-format tags** — each sync history entry now captures the file quality (e.g. `WEBDL-1080p`) and custom format names (e.g. `Uncensored`, `JA+EN Audio`) from the Sonarr/Radarr webhook payload and displays them as colour-coded inline tags beneath the episode info. Quality tags are shown in blue; custom-format tags in purple. Works for single downloads, batch/season-pack imports, rename events, and Radarr movies.

### Fixed
- Quality and custom-format values are now correctly extracted from the nested `quality.quality.name` and `customFormatScore` fields in Sonarr/Radarr webhook payloads.

### Changed
- Renovate updated `docker/setup-buildx-action` to v4 and `docker/login-action` to v4.

---

## [v0.3.0] - 2026-03-03

### Added
- All environment variables are logged at startup with secret values redacted, making misconfiguration easier to spot.
- Flask development-server startup banner is suppressed for cleaner container logs.

### Fixed
- Sonarr upgrade events (and standalone file-delete events) no longer record the **old** episode filename in sync history. `EpisodeFileDeleted`, `SeriesDelete`, `MovieFileDeleted`, `MovieDelete`, and `Grab` events are now silently skipped — the `Download` event that follows an upgrade is sufficient to trigger the scan with the correct new filename.
- Additional guard for Sonarr `Download` events with `isUpgrade: true`: if the `episodeFile` field transiently references one of the files listed in `deletedFiles` (the files being replaced), that stale filename is discarded rather than written to history.
- Multi-episode deduplication: when Sonarr fires both an `Import` and a trailing `Rename` webhook for the same episodes, the rename no longer creates a duplicate history entry with a doubled episode count.

### Changed
- Added Trivy container image vulnerability scanning to the Docker CI workflow.
- Added `workflow_dispatch` trigger to allow manual runs of the Docker publish workflow.

---

## [v0.2.0] - 2026-03-02

### Added
- **Stats API** — new unauthenticated `GET /api/stats` endpoint returning aggregate sync counts (total, ok, failed, sonarr, radarr, manual), average duration, queue depth, in-flight count, worker status, and last sync info. Designed for the [Homepage](https://gethomepage.dev) `customapi` widget; widget YAML snippet included in the endpoint docstring
- **Episode hover tooltip** — when multiple episodes are merged into a single sync record the episode-count badge (e.g. `5 episodes`) now shows a dropdown tooltip on hover listing every individual filename. Tooltip only appears when filenames are known (new-format records); old count-only records display the badge without a tooltip
- **History search bar** — text field above the sync history list that filters by path using a server-side SQL `LIKE` query so results span all pages, not just the currently visible page. Debounced auto-submit fires 400 ms after the user stops typing
- **History status filter** — **All / OK / Failed** pill buttons filter the history list by status. Active pill is highlighted in the matching colour (amber / green / red). Search text and status filter are preserved across pagination and when switching between filter tabs

### Changed
- Episode data for batch Sonarr imports is now stored as a JSON list of filenames (e.g. `["S01E01.mkv","S01E02.mkv"]`) instead of a plain count string. Enables the hover tooltip. Single-episode records continue to store a plain filename string. Old count-only records (`"N episodes"`) are handled transparently
- `_merge_episode_counts` updated to merge JSON filename lists and dedup by normalised SxxExx key; falls back to additive count for legacy records that carry no filenames

---

## [v0.1.2]

### Added
- `SYNC_COOLDOWN` documented in README env var table and `.env.example`

---

## [v0.1.1] - 2026-02-28

### Added
- Post-sync cooldown window (`SYNC_COOLDOWN`, default `5m`) — after a path finishes processing it is held in a cooldown set for the configured duration; any Sonarr follow-up events that arrive within that window (e.g. the `Rename` webhook that fires after a `Download`) are silently dropped, preventing duplicate history entries for the same show

### Fixed
- Multi-episode deduplication: when Sonarr fires 3+ rapid webhooks for separate episodes that all map to the same show folder, the dedup logic now merges the episode counts instead of silently discarding duplicates — the history record correctly shows e.g. `3 episodes` rather than only the first episode's filename
- Duplicate history entries caused by Sonarr's trailing `Rename` webhook arriving after a `Download` task had already finished processing

---

## [v0.1.0] - 2026-02-27

### Added
- Logo SVG displayed in the WebUI dashboard header and login card, replacing the plain text placeholder
- CSS `drop-shadow` glow applied to the logo icon for visual consistency with the accent colour
- `logo.svg` added to the repository and displayed in the README header

---

## [v0.0.16] - 2026-02-27

### Added
- Episode count badge rendered in the sync history UI for season pack / multi-episode imports, distinguishing them from single-file downloads at a glance
- SVG favicon embedded as an inline data-URI in both the dashboard and login templates — no external file dependency

---

## [v0.0.15] - 2026-02-27

### Changed
- Removed mergerfs options from Docker Compose example
- Updated README screenshots

---

## [v0.0.14] - 2026-02-26

### Added
- `RCLONE_PATH_REPLACEMENTS` — decouples the path sent to rclone VFS refresh from the Plex library path, supporting setups where the rclone remote host path differs from the container mount target

---

## [v0.0.13] - 2026-02-26

### Added
- Handle all three Sonarr episode payload variants: `episodeFile` (single download), `episodeFiles` (season pack / batch), and `renamedEpisodeFiles` (rename events)

---

## [v0.0.12] - 2026-02-25

### Fixed
- Sonarr webhooks now always scan the show root folder rather than the season subfolder, ensuring Plex correctly associates new episodes with the existing show entry

---

## [v0.0.11] - 2026-02-25

### Added
- Episode filename shown in the recent syncs history list for Sonarr events (single episode shows filename, batch shows count)

---

## [v0.0.10] - 2026-02-25

### Changed
- Web UI moved from `/webhook/manual` to the root path `/` — the dashboard is now accessible directly at the container URL
- Sonarr scan path changed to season subfolder (later corrected in [v0.0.12](#v0012---2026-02-25))

---

## [v0.0.9] - 2026-02-20

### Changed
- Upgraded Flask to 3.1.3

---

## [v0.0.7] - 2026-02-15

### Changed
- Upgraded base Docker image to Python 3.14 slim
- Fixed typos from rebrand

---

## [v0.0.6] - 2026-02-15

### Fixed
- Dockerfile typo

---

## [v0.0.5] - 2026-02-15

### Changed
- Project rebranded to **Media Servarr Sync**

---

## [v0.0.4] - 2026-02-15

### Fixed
- Removed stale path info from script output

---

## [v0.0.3] - 2026-02-15

### Fixed
- Dockerfile script name correction post-rebrand

---

## [v0.0.1] - 2026-02-15

### Added
- Initial project push with core webhook receiver for Sonarr and Radarr
- Plex library scan triggered on webhook receipt via PlexAPI
- Plex timeout handling with up to 3 retries and 10 s back-off; connection invalidated and re-established between attempts
- Metadata analysis (`item.analyze()`) after successful scan to update Plex match data
- Thread-safe task queue with a background worker daemon thread
- In-flight deduplication — prevents the same folder being queued more than once simultaneously
- SQLite-backed sync history with per-page pagination in the UI
- Sync history retention configurable via `HISTORY_DAYS` environment variable
- Optional rclone VFS cache integration (`USE_RCLONE`, `RCLONE_RC_URL`) to issue VFS forget + async refresh before scanning
- Configurable webhook delay (`WEBHOOK_DELAY`) to allow Sonarr time to finish writing before scanning
- Minimum file age check (`MINIMUM_AGE`) to ensure files have settled on disk before scanning
- Path replacement mappings for Plex paths, rclone paths, and library section IDs via environment variables
- Login / logout authentication protecting the manual trigger UI; credentials configured via `MANUAL_USER` and `MANUAL_PASS`
- Timezone-aware timestamps throughout the sync history UI
- `PLEXAPI_HEADER_IDENTIFIER` environment variable to override the Plex client identifier sent with API requests
- Proper health check endpoint (`/health`) returning JSON with Plex connectivity, rclone state, queue depth, and worker thread status; responds HTTP 207 when degraded
- GitHub Actions CI/CD workflows for Docker image publishing
- Docker Compose configuration with JSON log rotation (10 MB max, 3 files) and persistent data volume
- Docker health check hitting `/health` at 30 s intervals
- LICENSE file (MIT)
- Renovate bot configuration for automated dependency updates

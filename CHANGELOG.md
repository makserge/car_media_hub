# Changelog

## 0.0.6

- **Compressed region downloads (2026-11-24).** New option `region_transfer_gzip` (default on): a background thread (new
  `regions_gzip.py`) keeps a deterministic gzip copy (level 6, mtime 0, no file name) of every hashed `routing.tar` and `places.sqlite`
  of at least 1 MiB as `routing.tar.gz` / `places.sqlite.gz` next to it, recorded in the new `transfer.json` (size, mtime, SHA-256 and
  the SHA-256/size of the plain file it was built from). The catalogue adds an optional `"transfer": {"encoding": "gzip", "url", "bytes",
  "sha256"}` to those file entries; `url`/`bytes`/`sha256` keep describing the installed plain file, so older car apps ignore it and
  download the plain file. The copies are served on `/api/regions/files/<id>/<name>.gz` exactly like the plain files (Range,
  `If-Range`, strong ETag = SHA-256 of the gz bytes, `X-Max-Streams`, stream budget). A copy is offered only while it fits the plain
  file's current SHA-256; stale or orphaned copies are deleted at a rescan (all of them while the option is off), deleting a file deletes
  its copy, unfinished `.gzip-*.part` temps are removed at the start and after 24 h. A file that stays above 95 % is recorded as
  `skipped` and sent plain. Extra disk use about 30-50 % of the routing and places files. The Regions page shows the copy's state.
  Tests: `test_regions_gzip.py`.

## 0.0.5

- **Fast sync, add-on side (2026-11-23).** Music, region and charger files are sent with `loop.sendfile` (zero copy; new `fileserve.py`) and
  carry `Accept-Ranges`, a strong ETag and `X-Max-Streams`; music `Range` now also works together with `If-Range` (strong ETag
  `"<size hex>-<mtime_ns hex>"`, before every `If-Range` request got the whole file). The global limit of 4 region downloads is replaced
  by a shared per-client budget of 8 streams and 16 in total (new `streams.py`; music, regions, charger files and probe; 503 +
  `Retry-After: 2` above it). New `GET /api/probe` (random bytes from memory, default 8 MiB, max 64 MiB) and `GET /api/probe/disk` (new
  `probe.py`), one new option (`sync_dscp`, see below). The library listing is rebuilt in the background (stale-while-revalidate, at most 2 minutes old) and a
  region file request no longer waits for a rescan; charger files are no longer read into memory and hashed on the event loop per request;
  the charger CSV download writes and fsyncs, and source downloads hash, in worker threads. DSCP CS1 is set on file and probe connections.
  `tools/bench_sync.py` measures MB/s per stream count on localhost (or against a real add-on). Tests: `test_sync_ranges.py`,
  `test_probe.py`, `test_streams_budget.py`, `test_resources_fileresponse.py`, `test_no_loop_blocking.py`.
- **Review fixes of the fast sync (2026-11-23, round 2).** A stalled stream that loses its slot is now cancelled and unwinds out of
  `loop.sendfile` before its connection is aborted (aborting it from outside left a selector registration behind: the next connection that
  reused the file descriptor was never answered, and asyncio logged an error); the slot is free at once and also when the last byte is out
  (a client with exactly 8 workers no longer sees a short `503`). Stall detection is time based per 256 KiB (was 2 MiB), so a slow but
  moving stream is not taken over; at the total limit the client with the most streams gives up its oldest one to a client with clearly
  fewer. The stale listing is served for at most 120 s (was 600 s) and a cheap folder-time check shows direct changes within the TTL. Charger
  hash cache keyed by size, mtime, inode and ctime. `/api/probe/disk`: `HEAD` reads nothing, one probe at a time in its own thread,
  no link followed. `If-Range` with a date only for a file older than a second. New option `sync_dscp` (default on) switches the DSCP CS1
  marking. Tests: `test_fastsync_review.py`.

- **Session expired banner (2026-11-23).** A bare `401` instead of the UI does not come from the add-on (its middleware only sends `403`):
  Home Assistant's ingress layer answers it when its short-lived ingress session cookie has expired (idle tab, companion app, a bookmarked
  `/api/hassio_ingress/<token>/` link, a long viewer session). Every page (Media, Trips, Regions, Browser viewer) now carries one shared
  script (`session_guard.py`) that wraps `fetch` and `XMLHttpRequest`: a `401` shows the banner "Session expired - reload this page" with a
  **Reload** button (reloads the top window when it is same-origin, else the page) and all further requests of the page stop, so polling no
  longer hammers. The page is not rebuilt, so unsaved input stays until the user reloads. The viewer WebSocket asks once with a plain
  request after a close (a refused handshake only shows as code 1006) and stops reconnecting after a `401`; reconnects back off from 2 s
  to 15 s. DOCS.md: new *Troubleshooting a bare 401*. Tests: `test_session_guard.py`.

- **Browser: new tabs and pop-ups (2026-11-23).** The embedded browser keeps one tab. A new tab or window that the viewer did not just ask
  for (click or key press within 3 s, opened by the viewed page) is closed at once; before, every new tab was loaded into the viewed tab
  and the viewer was taken to the advertisement. A wanted one (a `target=_blank` link the page script did not catch) is loaded in the viewed tab
  once its address is known (checked like any address) and then closed. `window.open` follows its address only with user activation;
  `target=_blank` forms are redirected to the same tab like links. A download started from a wanted new tab is accepted (its frame is that
  tab). Tests: `test_browser_popups.py`.
- **Image: Chromium, Xvfb and fonts.** The image already installs them from Debian trixie (`amd64`, `aarch64`); `fonts-liberation` is added (the
  recommended fonts are skipped by `--no-install-recommends`; about 1 MB). The "Chromium is not installed" error now says what to do (update or
  rebuild the add-on).

## 0.0.4

- **Charger database built by the add-on (2026-11-19).** New `charger_refresh.py`: weekly (option `charger_refresh_days`, default 7, 0 = off;
  optional `charger_source_url`) download of the Bundesnetzagentur Ladesaeulenregister CSV (streamed to `.resources/tmp/`), conversion in a
  worker thread (port of the app's `tools/build_charger_db.py`, same schema), validation (schema, integrity, at least 20000 sites, not below
  60 % of the current file), atomic replace of `<region_storage_dir>/.resources/chargers/chargers-de.sqlite`; the existing
  `/api/resources/manifest` flow lets the app pick it up. One build at a time; failures keep the old file; the status (last success, sites,
  register date, last error, next run) and a **Refresh now** button are on the Regions page. Clear messages for no internet and for a changed
  source format. Status is kept in `.resources/charger-status.json`. Tests: `tests/test_charger_refresh.py`.
  Hardened downloads: errors name the step (landing page, CSV download, convert, validate), host, seconds, bytes and exception class (also
  logged at WARNING and shown on the Regions page); IPv4 only, browser-like headers, 3 attempts per HTTP step (5 s / 20 s pauses), landing page read
  until EOF; first failure retried after 15 min (then 6 h); fallbacks when the page is unusable: `charger_source_url`, the last known address, then
  the dated file name of the last 60 days.
- Everything under *Unreleased* below is part of 0.0.4 as well (resources API, build stamp, regions work).

## Unreleased

- **Build stamp (2026-11-18).** `build_info.py`: build id (SHA-256 over the add-on's `*.py` files), start time and version from
  `config.yaml` (the Dockerfile now copies it to `/app/config.yaml`); shown as a footer line on the Regions page, returned as `build` by
  `/regions/api/list` and `/api/regions/devices`, logged at start; `tools/addon_build_id.py` prints the id of a folder. Tests:
  `tests/test_build_stamp.py` (also checks the "done" display with the real car's data).

- **Done vs. pending on the Regions page (2026-11-18).** A wanted region that every known device has installed (current version or
  version unknown) is shown as "Installed on the car app (n of m devices)" instead of "Wanted ... (queue position 3)"; queue positions
  on the page and in `/api/regions/devices` count only pending regions (dense); an installed older version is "update pending"; a
  device without work says "no installs pending". The catalogue's `installOrder` and the request in `want.json` are unchanged.
  Tests: `tests/test_regions_done.py`.

- **Estimated sync time (2026-11-18).** Optional `bytesPerSecond` in a device report (validated 0..1e9); otherwise the rate is derived
  from consecutive reports (60 s exponential average, resets, negative deltas and gaps over 3 min restart it, stored with the device,
  dropped after 10 min). `/api/regions/devices` and the Regions page show per region done/total, rate and remaining time (only while
  downloading), for queued regions the start time after the ones before, and per device `estimate` for the whole queue ("measuring..."
  below 50 KB/s or when unknown; "paused" without a number). En/de wording, note that it is an estimate. Tests: `tests/test_regions_eta.py`.
  A routing file shared by a routing group is counted once per device and a region waiting for it shows "waiting for the shared routing
  file (X of Y GB done)" instead of 0 % (`waitingForShared`, `sharedRouting`, `leftBytes`); tests: `tests/test_regions_eta_shared.py`.

- **Resumable region installs (2026-11-18).**
  - Serving: a resumed `Range`/`If-Range` download needs no re-hash after an add-on restart (hash from `files.json`); a hash that finished
    while a rescan was running is no longer lost (the catalogue could stay on `hashing` for 10 minutes and the file was hashed twice);
    a stalled connection (no chunk accepted for 30 s) gives up its download slot when all 4 are taken, so a car that wakes up is not
    refused with `503 busy`; a client that stopped reading ends quietly (no warning or traceback).
  - Install state: new report states `paused` / `interrupted` (stored as `paused`) and `cancelled` are accepted (old apps unaffected);
    the add-on keeps the best byte count of a device; `GET /api/regions/devices` and the page show `paused - last seen N min ago, X of Y`
    for a quiet download or a network failure after progress (German wording when the browser is German); install requests never expire
    because of silence.
  - **Cancel install is a state**: catalogue fields `cancel` / `cancelRevision`, persisted in `want.json`; a new install request gets a
    later `installRevision`. Device view shows `cancelled` for such a region.
  - **Install queue**: catalogue field `installOrder` (position in the order of the requests, kept in `want.json` as `wantedSeq`),
    **Install all** (`POST /regions/api/install`), per-device `running` / `queue` / `queuePosition` in the devices JSON and on the page.
  - Imports: one at a time with a persistent queue (`.import-queue.json`), resumed after a restart and after a lost connection (Range
    from the part), part cut back to its fsynced length, the streamed SHA-256 stored at once, region with a new file kind being built
    is `state: "updating"` (not offered); cancel of a queued or running import. Tests: `tests/test_regions_resume.py`, `tests/test_regions_queue.py`.

- **Regions review fixes (2026-11-16).**
  - Uploads and imports keep `min_free_space_gb` free (`main.py` passes it) and check it before and every 64 MiB while streaming, also
    without a stated size (the ingress proxy sends no `Content-Length`); a part file is deleted on abort or a full disk.
  - Catalogue: the routing file of a routing group is named `<group>-<sha8>-tiles.tar` (different content never shares a name, no clash
    with a region's own `<id>-tiles.tar`). Additive: `installRevision` / `removeRevision` (time of the current request, else `""`).
  - Files: a file younger than 30 s or whose size changed between scans is neither listed nor served; a file is served only while its
    size and mtime match the hashed one (else `503` + `Retry-After`); the `ETag` is the file's SHA-256 (quoted hex).
  - Imports: stale `.import-*` / `.upload-*` parts (> 24 h) are removed at start and on rescan, partial files are shown on the page with
    **Discard**; resume only with a validator the server confirms (`If-Range`), else restart; time limit option `region_import_max_hours`
    (6); small blocking file calls moved off the event loop.
  - Regions page: **Forget** per device and **Forget all devices** (same CSRF checks); `region_storage_dir` below `/data` only as
    `/data/regions` (excluded from backups), `/addon_config` refused; stricter header checks (PMTiles 127 byte header, SQLite page size,
    tar checksum).

- **Offline map regions for the car app.**
  - New files `regions.py`, `regions_api.py`, `regions_import.py`, `regions_web.py`; options `region_storage_dir`
    (default `/share/car_media_hub/regions`, validated: below a mapped folder, never the library, staging or inbox) and
    `region_max_file_mb` (8192); `map: share:rw` was already in `config.yaml`.
  - Sync API (same token and port 8100): `GET /api/regions/catalogue` (JSON compatible with the app's `regions.json` v2, with
    `url`, `bytes`/`sizeBytes`, `sha256`, `version`, `updatedAt`; regions with a map only; `state: "hashing"` with progress
    while a SHA-256 is pending; `routingGroup` lets neighbouring regions share one routing archive) and
    `GET|HEAD /api/regions/files/<id>/<name>` (whitelist of three names, id pattern, `O_NOFOLLOW`, single `Range` with `206`/`416`,
    `ETag`, `Last-Modified`, `If-Range`, chunked streaming, at most 4 simultaneous downloads).
  - SHA-256 is computed lazily by a worker thread and persisted in `<region>/files.json` (valid while size and mtime match), so
    the first catalogue call on a big file never blocks the event loop. Rescan on request and every 10 minutes; folders
    produced by `tools/build_region_pack.py` (`<id>.pmtiles`, ...) are adopted by renaming them.
  - **Regions** page of the ingress UI: list, create, delete, edit, streamed upload with header check (`PMTiles`, `SQLite format 3`,
    `ustar`) and atomic rename, import from an https URL (netguard, pinned address, redirects re-checked, size limit, optional
    SHA-256, background task with progress, cancel and resume). Admin POSTs need `X-Requested-With: car-media-hub`, JSON where
    there is a body, and are refused cross-site.
  - Documentation: *Offline map regions* in DOCS.md and the design note `docs/REGION-BUILDER-DESIGN.md` for an in-add-on builder
    (phase 2, not implemented, off by default). Tests: `tests/test_regions.py`, `tests/test_regions_import.py`.
- **Regions: install on / remove from the car app from Home Assistant.**
  - New `regions_devices.py`; per-region desired state in `<region>/want.json` (`wanted`, `wantedAt`, `wantedBy`,
    `removeRequested`, `removeAt`); catalogue regions get the additive booleans `install` (wanted and ready) and `remove`
    (cleared when all known devices report the region `absent`/`removed`, or after 30 days).
  - Sync API: `POST /api/regions/devices/report` (bearer, JSON, 8 KB, strict validation, `deviceId` `[A-Za-z0-9_-]{8,64}`,
    states `installed|downloading|queued|failed|removed|absent`) and `GET /api/regions/devices`; devices live in
    `.devices/<id>.json` (atomic writes, at most 20, dropped after 90 days).
  - Regions page: **Install on car app** / **Cancel install** and **Remove from car app** per region, a status line per device,
    a devices table, auto-refresh every 5 s while something is pending (30 s otherwise); admin POSTs
    `/regions/api/<id>/wanted` and `/regions/api/<id>/remove` with the usual CSRF checks.
  - Tests: `tests/test_regions_want.py`.
- **Security / fixes: fourth pass (re-review 3 of B1).**
  - **R1:** after `7z`/`bsdtar` ran as uid 1011, root takes the tree back with an `O_NOFOLLOW` descriptor walk: owner
    root, folders 0755, files 0644, ACLs and xattrs removed, links/FIFOs/sockets/devices deleted;
    `_verify_tree` also refuses special files.
  - **R2:** all processes of uid 1011 are killed after every `7z`/`bsdtar` run (listing included) and at add-on start.
  - **F1/F2:** extracted releases keep normal modes into the library (umask 077 only in the extractor process), so
    nested `.rar`/`.7z` archives in subfolders now extract.
  - **D1:** `python3-aiohttp`/`python3-mutagen` from apt, no pip; `armv7` dropped from `config.yaml`/`build.yaml`.
  - **D2:** Chromium is started from `/usr/lib/chromium/chromium`; behind the wrapper script `HOME` is `/nonexistent`.
  - **D4:** `resource` is imported at module level (no import inside `preexec_fn`).
- **Security: third pass (re-review 2 of B1).**
  - **Image builds again (N1):** the managed WebRTC policy is now a real file (`policy/cmh.json`, copied by the
    Dockerfile) instead of a `printf` whose `\n` had become a line break. New tests parse the Dockerfile (quotes
    balanced per instruction, policy file present and valid JSON). A `.dockerignore` keeps caches out of the image.
  - **Debian base (N2, owner decision):** the image is now `ghcr.io/home-assistant/{arch}-base-debian:trixie`
    (assumed tag; not pulled or built here). Chromium, Xvfb, 7zip,
    libarchive-tools and the Python dependencies come from `apt-get` (`--no-recommends`, lists removed); users are
    created with `useradd`. Debian's security team updates Chromium, but the version is fixed at image build time, so
    rebuild or update the add-on regularly. The browser refuses to start (prominent log warning) when the installed
    Chromium major version is below `MIN_CHROMIUM_MAJOR` (140). DOCS and the option texts (en/de) say that the
    browser is only as current as the image and is expected to stay unavailable on Home Assistant OS.
  - **Archives never extracted as root (N3):** `7z` and `bsdtar` (listing and extraction) run as the unprivileged user
    `extract` (uid 1011) with a clean environment (no Supervisor token), RLIMIT_AS/CPU/FSIZE/NOFILE/NPROC/CORE, the
    archive passed as a file descriptor and a target folder that user owns while it runs and that root takes back
    afterwards. The existing name, link, entry-count and size checks stay and are now tested.
  - **Kill (M5):** after stopping, the add-on also runs `kill(-1, SIGKILL)` as the browser uid in a helper (atomic for
    the uid); the capped /proc loop stays as backup; the browser user gets `RLIMIT_NPROC` 1024.
  - `/tmp/.X11-unix` is created and chmod'ed through an `O_NOFOLLOW` descriptor (N6).
- **Security: second pass on the embedded browser (re-review of B1).**
  - **Root never follows links in the browser user's tree (C1).** The profile and its `tmp`, `.config`, `.cache` folders
    are created and given to the browser user through directory descriptors (`O_NOFOLLOW|O_DIRECTORY`, `fchown`,
    `fchmod`); a link or file in their place is removed, never followed. The recursive `lchown` migration is now
    descriptor based too (`os.fwalk`, no hard-linked files). The download folder moved out of the profile to
    `/data/browser-incoming/dl` below a root-owned (0711) parent, emptied at each start. The DevTools port file is read
    without following links, and the hand-over also checks owner and hard-link count and no longer overwrites a file
    of the same name. `/data` is mode 0711 (the browser user can enter it, not list it).
  - **No browser without a sandbox by default (H2).** The browser starts only when the sandbox probe passes, or when
    the new option `browser_allow_unsandboxed` (default off, strongly worded) is on. The automatic retry without
    sandbox is gone. Wording in the translations and the Documentation no longer claims that the proxy contains a
    compromised browser.
  - **Image (H1).** `build.yaml` moves from Alpine 3.20 (end of life, frozen Chromium) to 3.22 (assumption: the HA
    base image tag `3.12-alpine3.22` exists; the image was not built here). The Chromium version is logged at start.
  - **WebRTC (M1):** `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (value on the switch), `--webrtc-ip-handling-policy`
    and a managed policy file; mDNS hiding stays on.
  - **Proxy (M2):** one plain-http request per connection (`Connection: close` both ways, pipelined bytes end the
    connection, `Upgrade` and chunked request bodies are refused); every request is checked.
  - **Downloads (M3):** one download per click, 5 s window, only from the main frame; a reload or a feed item opened by the
    page is no click. **manual_only (M4)** is stored with the release (`report.manual_only`) and also applies to
    releases of the reserved source name `browser`, so removing or disabling the source cannot make them auto-approvable.
  - **Processes (M5):** the browser user's processes are killed in rounds until none remain (cap 50, SIGKILL).
  - Blocklist: `::/96` (IPv4-compatible), `fec0::/10` and percent-encoded hosts are refused.
  - `config.yaml`: `timeout: 30`, `backup_exclude` for the browser profile and download folders. The sync server is
    stopped before the Home Assistant session closes.
- **Security: the embedded browser is hardened and opt-in (review B1, H1, H2).**
  - New option `browser_enabled` (default **off**). Off means no Chromium, Xvfb or proxy is started, the viewer
    routes do not exist, and feed items open in your own browser.
  - Chromium and Xvfb run as the unprivileged user `browser` (created in the Dockerfile) with a clean, explicit
    environment (no `SUPERVISOR_TOKEN`); the profile moved from `/data/chromium` to `/data/browser-profile` (the old
    folder can be deleted). The Chromium sandbox is required (see the second pass below for `browser_allow_unsandboxed`). `run.sh` makes the s6 environment folder root-only.
  - A local filtering proxy refuses private, loopback, link-local and Home Assistant (172.30.32.0/23) targets,
    non-http(s) schemes and `supervisor`-style names, resolving names itself (DNS rebinding safe); page and frame
    requests are checked again in the DevTools gate.
  - Resource limits (renderers, script heap, 4 h session, parallel downloads) and cleanup of orphaned Chromium and
    Xvfb processes at start, stop and shutdown (the browser is stopped first).
  - **Downloads need a user gesture** (click, key press or opened address in the viewer within 20 s), must be
    archives and under `max_release_size_mb`; others are cancelled. Files go to a private folder and, when complete,
    to a separate `browser` inbox (`/data/browser-downloads`), not the shared inbox. Releases from it are never
    auto-approved (not by `rss_auto_approve`, not by the pending timeout).
  - **Feed links:** only http(s) (or relative) links are rendered (server and page), with
    `rel="noopener noreferrer nofollow"`; `javascript:`/`data:` links are dropped.
  - Tests added under `tests/` (run with `python -I -m pytest`).
- **Media page layout.** The page has two columns: the RSS feed on the right and all other blocks on the left (below 900 px wide they stack, feed first). **Pending review** and **Recently decided** are panels like the other blocks, with their releases as list rows instead of separate cards. The **Library** is a list of clickable items again: a folder opens and closes the list of its files (sub-folders too; open folders stay open when the library is listed again), and **Delete** stays on the top level.
- **Renamed.** The top button **Review** is now **Media** (also on the Trips page and in the browser viewer's back link), and the app is called **Car Media and Trips** instead of **Car Media and Trips Hub** (add-on name, sidebar panel, page titles, log lines, notifications and the friendly names of the trip sensors; entity ids are unchanged).
- **Review page.** The RSS feed block is above the status. The feed is a plain list of all items (no Done button, no "Show all"); an item is marked done when its download is complete, and the list is refreshed then (visited links no longer change colour). "Recently decided" is a list like the library, not cards. The Library is always shown (the top level of `library_dir`, no collapsible, no sub-folder browsing) and is listed again whenever a release is approved or something is deleted. The Sources table shows the type under a source name only when it differs from the name (inbox and rss were shown twice).
- **Inbox row in the Sources table.** It now shows the last check, the next check (about) and a result such as "1 waiting until unchanged for 30s, 1 still downloading, 1 ignored (not an archive) - last: Album (done)". Before, those cells were empty for the inbox.
- **Inbox log.** The inbox now logs once when it finds an entry (and when it ignores a file that is not an archive), so it is visible in the add-on log whether a download in the inbox was seen and why it is not imported yet.
- **Still picture matches the page.** The Chromium window is now as large as the virtual screen (2560x1600), so its content area is never smaller than the page (a smaller window cut the still PNG, which was then stretched and showed the links lower than the mouse). A still picture whose size differs from the page is not shown while a matching moving frame exists (the log says so).
- **Picture and mouse always agree.** A moving frame that is shifted (a bar above the page) or of another size than the page area is no longer drawn; a screenshot of the page is shown instead while the page moves (the log says when this happens). The mouse cursor (hand over links) is also asked for after the mouse stopped, after the page scrolled and when the mouse moved during a request, so it no longer shows the link above or below.
- **Wrong item opened.** The number that tells the server which click is newest is now made at the click, not when the Review list is drawn: a list kept by the Back button had old numbers, so an item above the one opened before was ignored and the lower one stayed open.
- **Back button fix.** The viewer's "Review" link now steps back in the history (or replaces the viewer entry) instead of adding a new one, so Home Assistant's Back button no longer returns to the embedded browser after leaving it.
- **Up to 3 redirects per click** (`ALLOWED_REDIRECTS` in `browser.py`; 0 = none, -1 = no limit). After a click, a key press or an opened address, the page asked for plus up to 3 redirects (HTTP, script or refresh) load; the next one is refused before it loads and the viewer shows a message. The log also shows the size of the moving (JPEG) and still (PNG) picture and the frame offset whenever they change, to find why the picture height jumps on some pages.
- **New add-on browser instead of the Browse proxy.** A feed item on the Review page opens in a Chromium that runs
  inside the add-on and is shown live (picture, mouse and keyboard through the page). Its download folder is the
  inbox, so finished archives are picked up, extracted, cleaned and reviewed as before, and the item is marked done
  once a file arrived. The proxy (`/browse/*`) and the `manual` source are removed; a leftover
  `staging/_downloads/manual` folder can be deleted. The image build installs Chromium from the Alpine mirror.
  The page is as large as the free area of the window (it follows window resizes), so nothing is cut off; a finger
  drag or the wheel scrolls it. Frames are capped at about 20 per second, and mouse
  and key events no longer wait for Chromium's answer, which keeps the page responsive on a slow host. The page is
  rendered at the window's pixel density (up to 2x on Retina screens) with the highest JPEG quality while it moves;
  when it stops, one lossless PNG replaces the last frame.
  The Review page shows the progress of the browser's downloads in its own block, without the browser window.
  Dragging a scrollbar or a selection works (moves with a button held are sent as drags, and the window follows the
  mouse past the edge); scrolling is reversed on macOS. Chromium is forced to the sRGB colour profile.
  The page is only rendered and encoded while a viewer is open, so a download running in the background gets all the
  CPU. The downloads list on the Review page shows sizes and has a Stop button for a running download.
  It also shows the speed and the estimated time left. Pages are never cached (cache and service workers are off, the cache is cleared and loading stopped before each
  open), and a viewer page that reconnects late cannot send the browser back to an older item. The viewer toolbar has
  no Done and Close buttons. Chromium's infobars (`--no-sandbox`, missing API keys) are switched off: they pushed the page about 50 px down in the
  moving picture but not in the still one, which made the picture jump and clicks land on the next link; the log warns
  if a shift remains. The mouse cursor follows the page (a hand over links, a text cursor over fields). The viewer's layout no longer depends on its messages and download list (they sit over the picture), so the picture does
  not change height; scrollbars inside pages have no arrow buttons (styled in every page). More than 3 redirects in a row (HTTP, script or refresh) after a click or an opened address stop the page and
  the browser shows a message; a Stop button (next to Reload) stops loading by hand. Every click on a feed item opens that item's own page (before, the same item reopened where you had left the browser). Opening another item shows a loading indicator instead of the
  previous item's page; the browser toolbar has no back and forward buttons, and the page's scrollbars are the thin
  overlay kind without arrow buttons. The loading screen (dark spinner on white) stays until the new page's first picture
  is drawn. Home Assistant's Back button (and a swipe in the page) only moves between Home Assistant screens; the
  embedded browser's own history is reached with the mouse's side buttons or Alt+Left/Right (Cmd+[ / Cmd+] on a Mac).
- **Removed the item-page scan from the RSS source.** The add-on no longer opens an item's page to look for file
  links when the item has no enclosure (it logged `found 0 file(s)` on every poll for hosts that never link files
  directly). Only enclosures are downloaded; use the add-on browser for the rest. The options `rss_scrape_item_pages` and
  `rss_scrape_formats` and the source settings `scrape_item_pages` and `scrape_formats` are removed; remove them
  from your configuration if Home Assistant reports them as unknown.
- **Renamed the add-on to `car_media_hub`** (slug, package and identifiers). This is a new add-on in Home
  Assistant: install it, copy the settings from the old add-on's Configuration tab, then remove the old one.
  Its history (database, releases, trips) and the old panel link do not carry over.
- **Renamed the internals to Car Media and Trips Hub**: the Python package is now `car_media_hub`,
  the environment variables are `CAR_MEDIA_HUB_*`, and event, entity and notification ids use the `car_media_hub`
  prefix: the decision event is `car_media_hub_decision`, trip sensors are `sensor.car_media_hub_*`. **Update any
  automation that listens for the old event or reads the old trip sensors.**
- Default `inbox_dir` and `staging_dir` for new installs are `/share/car_media_hub/inbox` and
  `/media/.car_media_hub/staging`. Existing installs keep the folders saved in their configuration.
- **Removed** the `examples/` folder (the example plugin is still described in the documentation).

## 0.0.1

- **Browse tab** (panel > **Browse**): open an RSS item (or any address) and click through the site's pages
  yourself; the first response that is a file is saved on the server and offered for approval like any other
  release. Links, forms (GET and POST), cookies, redirects and meta refresh work; scripts are off unless
  switched on per session; pages run in a sandbox and only public addresses are opened.
- New source type `manual` (always on) that turns the saved files into releases and marks the RSS item as handled.
- **Removed the custom RSS parsers**: the parser plugins (`/config/parsers`, the seeded `custom.py`, `ektoplazm`),
  the `rss_parser` and `parsers_dir` options and the follow-links API. The RSS source keeps enclosures,
  direct file links and the file links found on the item page (what the `generic` parser did). A `parser` or
  `parsers_dir` key in `sources.json` is ignored with a warning; files left in `/config/parsers` are ignored.
- **Removed the archive.org source** and its options (`archive_org_items`, `archive_org_query`,
  `archive_org_max_items_per_scan`, `archive_org_poll_minutes`). Releases already imported from it stay in the
  library and history; stored archive.org state in the database is simply no longer used.

## 0.3.2

- **EV settings sync** for the car app: `GET /api/settings/ev` and `PUT /api/settings/ev` on the sync port
  (same bearer token) store one JSON document with the app's operator preferences, tariffs, owned cards,
  default tariff, battery capacity and vehicle choice, so they survive a phone or box change. `PUT` uses
  optimistic concurrency (`base_revision`, `409` with the current document on a conflict), validates the
  document and accepts at most 1 MB. New table `ev_settings` (created automatically).
  Contract: *EV settings* in docs/TRIPS_API.md.

## 0.3.1

- **Trips page** in the web UI (sidebar panel > **Trips**): summary cards (trips, total km, total kWh,
  average kWh/100 km, last 30 days), an overview map of all trips of the month filter colour-coded by
  consumption, a bar chart of the last 20 trips, a paged list (50 per page, newest first) with a month
  filter, a trip page with route map and facts, trip deletion, and CSV export (UTF-8 with BOM,
  semicolon separated, optional decimal comma for German Excel).
- Leaflet 1.9.4 is bundled (no CDN). Map tiles are fetched by the add-on and cached in `/data/tile_cache`;
  without internet access the routes are drawn on a plain background.
- New navigation (Review / Trips) on the web UI pages.

## 0.3.0

The add-on is now called **Car Media and Trips Hub** and also collects the car app's trips and energy
consumption. The slug stayed the same, so existing installs, paths, `/addon_configs/<slug>/`,
the database and the panel link keep working; only the display name changed.

- **Trip API** on the sync port 8100 (same `sync_token`, same bearer auth): `POST /api/trips` (batch
  upsert, idempotent by trip id, at most 1000 trips and 5 MB per request, per-item rejection reasons),
  `GET /api/trips?since=&before=&limit=`, `GET /api/trips/{id}`, `DELETE /api/trips/{id}`,
  `POST`/`GET /api/trips/{id}/samples` (optional power/SoC/speed samples) and `GET /api/stats`
  (totals and last 30 days). Contract: docs/TRIPS_API.md and the *Trips and energy* section of DOCS.md.
- **Home Assistant sensors**, published after each accepted upload and at start: `sensor.car_media_hub_last_trip_distance`,
  `_last_trip_consumption`, `_last_trip_end`, `_total_distance`, `_month_energy`, `_average_consumption`.
- New database tables `trips` and `trip_samples` (created automatically; existing data is untouched).
- The trip endpoints need the sync API to be on (`sync_enabled` and a valid `sync_token`).

## 0.2.0

Merges the rss_downloader add-on into Car Media and Trips Hub and serves the library to the car app and cyp.
See DOCS.md, "Migrating from rss_downloader".

- **Offline image build**: the Python packages are bundled in `wheels/` (no PyPI needed), and a
  failed Alpine install of `7zip`/`libarchive-tools` only prints a `WARNING`. Without them `.rar`
  and `.7z` releases are marked as failed, with a clear message on their review card and in the log.

- **New default `staging_dir`: `/media/.car_media_hub/staging`** (was `/share/car_media_hub/staging`).
  It is on the same storage as the default library, so an approval is an instant move instead of a
  copy that briefly needs the release's size twice. Home Assistant keeps only saved options: an
  installation whose Configuration tab was saved with 0.1 keeps `/share/car_media_hub/staging` until
  you change it; one that was never saved switches at the update. Releases pending in the old folder
  are still approved from there (by a copy).

- The "Review in Car Media and Trips Hub" link in notifications opens the panel on Home Assistant versions
  that call add-ons "apps".

- **RSS source** (`rss_feed_url` and the other `rss_*` options, or `{"type": "rss"}` entries in
  `sources.json`): RSS 2.0 and Atom feeds, enclosures, direct file links and item pages handled by
  rss_downloader-compatible parsers (`generic`, `ektoplazm`, `custom` and your own files in
  `/config/parsers/`, re-read on every check; only `custom.py` is seeded). Daily `rss_check_time`
  or `rss_poll_minutes`, `rss_run_on_start`, `rss_max_items_per_scan`. `parser: pages` is not
  ported (warning, enclosures and direct links only). `rss_scrape_formats` also matches the file
  extension.
- **One-time import** of rss_downloader's `downloaded.json` (`rss_import_state`); the audio files
  already in the library are indexed once at start, so nothing is downloaded twice. The import arms
  the empty-library guard: until `library_dir` lists files, an empty folder there answers `503` (not
  an empty list the car app would mirror) and approvals wait, until you press **Confirm**.
- **Sync API** on container port 8100 (`sync_enabled`, `sync_token`), wire-compatible with
  rss_downloader's content sync for the CarNavigationHelper car app and cyp. The host port is off
  by default (`ports: 8100/tcp: null`) so the add-on can start next to rss_downloader; set it in the
  Network section. Empty-library guard with a Confirm button.
- The add-on never creates `library_dir` at start-up, not even as the parent of `staging_dir` or
  `inbox_dir` (those are skipped, with a Log warning, while the library is missing): a missing or
  unmounted library answers 503 (`library_unavailable`, with a Log warning) so the car app deletes
  nothing. A library on network storage that is down no longer stops the add-on from starting, and
  approvals into it are refused (*cannot be read*) instead of failing. With the sync API on and
  `staging_dir` inside a missing `library_dir`, nothing is downloaded or imported until the library
  exists or is confirmed. The migration guide now tests on host port 8101 first and has a
  fail-closed check and a stop rule before switching to 8100; the rollback steps are complete.
- With the sync API enabled, an approval does not create a missing `library_dir` either, and
  approvals wait while the library looks missing or unexpectedly empty after it was published (a
  persistent *library unavailable* notification; **Confirm** in the status bar lifts it). Library
  deletions lift the empty-library guard only when nothing listable is left. The status bar shows why
  the sync API answers 503 and why approvals wait (also with the sync API off); **Confirm** is only
  offered for a missing or deliberately empty library, not for one that cannot be read. When free
  space is above the minimum, the bar says which release waits for how much instead of "downloads are
  paused".
- Folder twins that differ only in case or Unicode form are served once (the published or first
  arrived spelling wins, also after a restart; the other folder is reported as *folder collides with
  ...*). A folder renamed only in case or Unicode form keeps its published spelling, so the phone does
  not duplicate it; when the served folder of a pair is deleted, the other is reported as *folder was
  published as X* until it is renamed. The first listing after a restart no longer hides old files
  whose ctime changed; failed listings are retried at most every 5 s; background re-listings never
  overlap; a huge `Range` number no longer gives a 500; the empty-library guard and Confirm survive a
  backward clock step.
- `inbox_dir`, `staging_dir` and `library_dir` must not overlap (the add-on refuses to start, with
  the reason in the Log); an inbox that is `/share` or `/media` itself is refused too. A second inbox
  in `sources.json` gets its own name (`inbox-<folder>`).
- `rss_check_time: interval` checks every `rss_poll_minutes` (the form cannot save an empty value).
- Deleting in the Library forgets the release (or the deleted tracks), so the same archive can be
  imported again; a deleted release's folder name is never reused (`Album (2)`; a name with a dot such
  as `Vol. 2` becomes `Vol. 2 (2)`). Deleting also lets archives that brought nothing new be looked at
  again, and deletions that race the library index or an approval no longer leave deleted tracks
  known. An inbox duplicate of an earlier release leaves a persistent notification instead of
  disappearing silently; a release left in *processing* by a failed attempt no longer turns the next
  attempt into a deleted "duplicate". A library folder named like an internal path token (`~mw64~...`)
  can no longer make the Library browser open or delete another folder. Unreadable library folders no
  longer make every start index the whole library again (only those folders are retried).
- The *library unavailable* notification is dismissed after a restart or Confirm; a refusal that
  repeats is logged once.
- Free space: an archive that does not fit yet waits instead of failing (and is not hashed or
  downloaded again until the space is there, including the download itself for sources without
  declared sizes and the copy of an inbox folder holding a nested archive; a short dip below the
  minimum keeps the recorded need, and needs expire after 2 days); only that release waits, the
  source goes on with the others; a full disk while writing a download is a space wait, not a
  failure; approval copies across mounts keep 1 GiB free and
  answer *not enough free space* (HTTP 507) otherwise; a bsdtar/7z run is stopped when it writes more
  than the budget, and bsdtar listings can no longer be spoofed through owner names.
- Robustness: `__MACOSX`/hidden entries no longer break nested extraction (and are not downloaded);
  file names that are not valid UTF-8 no longer stop the library index, inbox imports or the Library
  browser; the library index is kept per `library_dir` (a changed path is indexed again) and survives
  vanished sub-folders; an interrupted approval never deletes the only copy; releases left pending by
  0.1 get the 0.2 name cleanup at start-up.
- RSS: items sharing an id (no guid, same link) are told apart by their full enclosure URLs (also
  query-only ones such as `download.php?id=N`, and feeds that list one item at a time); one malformed URL
  no longer breaks the feed; the `<?xml encoding?>` declaration is honoured when the server sends no
  charset; `ctx.fetch_text` reads any content type; item pages have a 90 s total deadline that also
  covers the response headers, each item has a time budget, and Stop interrupts page scraping; a file
  named from the server's response can no longer overwrite another file of the same release.
- archive.org: skipping hidden/`__MACOSX` files no longer changes the stored signature, so items
  are not downloaded again after the upgrade. A RAR entry with an odd date no longer fails the release.
- The controller and the HA event listener are restarted with backoff when they crash; the sync API
  and web UI stay up. The ingress web server no longer logs every status poll. An invalid
  configuration stops the add-on with a clear Log message and exit code 1.
- **Auto-approve** per source (`rss_auto_approve`, `auto_approve` in `sources.json`) and
  `pending_timeout_action: decline|approve`.
- **Free-space guard** `min_free_space_gb` (default 50) for downloads, staging and extraction of
  every source; waiting for space and the Stop button never count as failures.
- **Name hygiene** while cleaning: hidden files and `__MACOSX` are removed, a single wrapper folder is
  collapsed, names are made safe for phones and FAT/exFAT (renames listed in the review card), and
  audio types the car app cannot play are flagged. Approved files get the approval time as mtime.
- `.rar` is extracted with bsdtar (`libarchive-tools`); `.7z` falls back to bsdtar when 7z is missing.
- Web UI: status bar (activity with progress and Stop, per-source last/next check with Check now,
  free space, sync status), library browser with delete.
- Remote sources: optional `name`, `check_time`, `run_on_start` and `auto_approve` settings; the last
  check is persisted; releases that failed 3 times are retried once a day; downloads verify
  Content-Length and reject HTML pages served instead of files; remote sources no longer post
  "nothing new" notifications.
- User-Agent `CarMediaHub/0.0.1`; requires `aiohttp>=3.11,<4`.
- Source plugin system: `sources/` package with a registry, a `RemoteSource` template and fetch
  helpers. New websites are added with a Python file in `/config/plugins/` and an entry in
  `/config/sources.json` (see DOCS.md, "Writing a source plugin"). The add-on now maps `addon_config`.
- inbox and archive.org are built-in plugins; existing options and archive.org state are unchanged.
  archive.org downloads are staged under `_downloads/archive.org/<id>/`.
- A source whose import failed is retried instead of being treated (and deleted) as a duplicate.
- archive.org downloads use connect/idle timeouts instead of a 60 s total, are bounded by the
  declared file size and `max_release_size_mb`, and write to disk off the event loop.
- Extraction is bounded by free disk space and shares one size budget across nested archives.
- Legacy (cp866/cp1251) Cyrillic file names in zip/tar archives are decoded and filtered.
- Cyrillic detection also covers ID3 comment/lyrics/TXXX frames, WAV/AIFF ID3, ASF and APEv2 tags.
- Approval is crash-safe: content is built in a hidden temp folder and renamed; interrupted
  approvals are completed or rolled back on start-up.
- Library folder names are limited to 200 UTF-8 bytes; notification markdown is escaped;
  undecodable file names no longer break the web UI.

## 0.1.0

- Initial release.
- Inbox folder source, archive.org source (items and search queries).
- Safe archive extraction (zip, tar, rar, 7z), Cyrillic and duplicate filtering.
- Approve/Decline via Ingress UI, persistent notification, mobile actionable notification and `car_media_hub_decision` event.

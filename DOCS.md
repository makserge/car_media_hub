# Car Media and Trips

## Installation

1. In Home Assistant open **Settings -> Add-ons -> Add-on Store**.
2. Open the menu (three dots) -> **Repositories** and add the URL of the repository that hosts this add-on (the `url` in `repository.yaml`, for example `https://github.com/makserge/car_media_hub`).
3. Install **Car Media and Trips**, review the options, and start it. A **Car Media and Trips** entry appears in the sidebar (Ingress).

## How it works

1. **Sources** deliver candidate releases: the inbox folder (also where your own browser saves files, see [Download in the add-on browser](#download-in-the-add-on-browser)), [RSS feeds](#rss-source)
   and any [plugins](#writing-a-source-plugin) you add.
2. Each candidate is hashed; one that was imported or declined before is skipped as a duplicate
   (a persistent notification names the earlier release, so a re-added inbox entry never disappears
   silently); one that failed is retried (an inbox entry when it changes or the add-on restarts,
   feeds a limited number of times). An archive that brought nothing new (all its
   tracks were already known) is skipped too, until something is deleted in the Library section. A
   release you deleted in the [Library section](#status-bar-and-library) is forgotten, so it can be
   imported again. A release folder deleted outside the Library section (Samba, File editor) is not:
   the release and its tracks stay known, so the same archive stays a duplicate.
3. It is extracted/copied into the staging directory and cleaned:
   - hidden files and folders (names starting with `.`) and `__MACOSX` folders are removed;
   - files with Cyrillic in the file name (and, optionally, directory name) are removed;
   - audio files with Cyrillic in text tags are removed;
   - if any FLAC remains, all MP3 files are removed;
   - optionally all tags are stripped;
   - empty folders are removed and a single wrapper folder (`Album/Album/...`) is collapsed;
   - file and folder names are made safe for phones and FAT/exFAT cards (no `" * : < > ? |`,
     backslash or control characters, no leading/trailing dots or spaces, at most 240 bytes,
     names that differ only in case get ` (2)`); renames are listed in the review card;
   - tracks already in your library, or repeated inside the release, are removed.
4. If audio remains, the release becomes **pending** and you are notified (or it is approved at
   once when the source has `auto_approve`). Otherwise it is marked empty.
5. **Approve** moves the cleaned release to `library_dir` (every file gets the approval time as
   its modification time, so the car app sees it as the newest music); **Decline** deletes it
   (the record is kept so the same archive is never re-imported). A folder name that was used
   before is never handed out again, even after you deleted that release (the new one becomes
   `Album (2)`): sync clients keep files under an old path and would not pick up the new content.

Archives: zip and tar are extracted by the add-on itself, `.rar` with `bsdtar`
(`libarchive-tools`) and `.7z` with `7z`. Every archive is checked first: no absolute paths, no
`..`, no links or device files, and the total size must fit into the free space; `bsdtar` and `7z`
are also watched while they run and stopped when they write more than that. Hidden entries and
`__MACOSX` companions (such as `__MACOSX/._CD1.zip`) are never treated as nested archives.

**Free space.** Before every download, staging copy and extraction the add-on checks that at least
`min_free_space_gb` stays free on the disk. When it would not, the work stops without counting as
a failure, the status bar shows *waiting for free space*, and the release is tried again on a later
check. This includes an archive whose extracted size does not fit yet: it waits (it is not
hashed or downloaded again until that much is free, or for at most 2 days) instead of failing,
and the status bar names it and the space it needs. Other releases go on meanwhile, also from the
same source. Only an archive that could never fit on the disk fails. Downloads, staging and the library usually share the Home Assistant data
disk (database, backups), so keep a comfortable margin. Approving a release **copies** it when staging
and library are on different mounts (for example staging in `/share`, the 0.1 default; the current
default keeps both in `/media`, see [Folders](#folders)): it then briefly needs the release's size
free a second time, until the staging copy is removed. The copy is
refused when it would leave less than 1 GiB free (the release stays pending; the panel says
*not enough free space*).

## Options

| Option | Default | Description |
|---|---|---|
| `inbox_enabled` | true | Watch the inbox folder. |
| `inbox_dir` | /share/car_media_hub/inbox | Drop archives or folders here. A folder of its own: everything in it is imported and then deleted (or moved to `_processed/`). Never `/share` or `/media` itself, and never a folder that contains or lies inside `library_dir` or `staging_dir` (the add-on refuses to start). |
| `staging_dir` | /media/.car_media_hub/staging | Working area for downloads and releases awaiting a decision. Not `library_dir` itself; inside the library only under a hidden (dot) folder. The default sits on the same storage as the default library, so an approval is an instant move (see [Folders](#folders)). |
| `library_dir` | /media/music | Destination of approved releases; the folder the sync API serves. |
| `scan_interval` | 60 | Seconds between scans (10-86400). |
| `settle_seconds` | 30 | Entry must be unchanged this long before processing. |
| `duplicate_action` | delete | `delete` or `keep` an inbox entry that was already imported. |
| `delete_source` | true | Delete processed inbox entries; otherwise move them to `_processed/`. |
| `dedupe_tracks` | true | Remove tracks already in the library. |
| `strip_all_tags` | false | Remove all tags from remaining audio files. |
| `check_directory_names` | false | Also remove files under directories with Cyrillic names. |
| `notify_service` | empty | Mobile notify service, e.g. `mobile_app_pixel_8` (without `notify.`). |
| `pending_timeout_hours` | 0 | Decide automatically on releases pending longer than this (0 = never). |
| `pending_timeout_action` | decline | What the timeout does: `decline` or `approve`. |
| `log_level` | info | `debug`, `info`, `warning`, `error`. |
| `max_release_size_mb` | 5000 | Largest release any download source (RSS, plugins) may fetch; larger ones are skipped or stopped. |
| `min_free_space_gb` | 50 | Keep at least this much space free (decimals allowed, `0` = no check). See *Free space*. |
| `sync_enabled` | false | Serve `library_dir` to the car app on port 8100 (cyp can use it too). See [Sync API](#sync-api). |
| `sync_token` | empty | Secret the clients send; at least 16 printable ASCII characters, no spaces. |
| `sync_dscp` | true | Mark the sync API's file and probe traffic with DSCP CS1 ("background"), so routers and Wi-Fi access points that honour it keep the sync from slowing down other traffic. Turn it off if the sync is slow on a Wi-Fi that puts such traffic into a very slow queue (some access points map CS1 to their background queue, which waits longer under contention). |
| `rss_feed_url` | empty | RSS or Atom feed (http/https). Empty = no RSS source. |
| `rss_check_time` | "03:00" | Check the feed once a day at this local time (`HH:MM`). Enter `interval` to check every `rss_poll_minutes` instead (an empty field cannot be saved in the form: it brings back `03:00`; `""` in YAML mode works too). |
| `rss_poll_minutes` | 1440 | Minutes between feed checks when `rss_check_time` is `interval` (5-10080). |
| `rss_run_on_start` | true | Also check the feed right after the add-on starts. |
| `rss_max_items_per_scan` | 10 | Feed items (releases) downloaded per check (1-1000); the rest wait for the next check. |
| `rss_auto_approve` | false | Move cleaned RSS releases to the library without asking. |
| `browser_enabled` | false | Switch on the embedded browser (Chromium) of the Media page. **Off by default**: with it off, nothing browser-related starts (no Chromium, Xvfb or proxy) and feed items open in your own browser. See [Download in the add-on browser](#download-in-the-add-on-browser). |
| `browser_allow_unsandboxed` | false | **Dangerous.** Without it the browser starts only when Chromium's own sandbox works in the container (on Home Assistant OS it usually does not, so the browser then stays off and says why). With it on, Chromium also runs without the sandbox: a browser exploit then reaches Home Assistant, other add-ons and your LAN directly, and the filter below does not stop it. See *Security of the browser*. |
| `region_storage_dir` | /share/car_media_hub/regions | Folder of the offline map regions (one sub-folder per region). Must be below `/share`, `/media`, `/config` or `/data/regions` (`/data` otherwise and `/addon_config` are refused: `/data` is part of every add-on backup, `/addon_config` is not a mount inside the container; `regions/**` is excluded from add-on backups), not a mount root, and never the library, staging or inbox folder nor contain or lie inside one (the add-on refuses to start). See [Offline map regions](#offline-map-regions). |
| `region_max_file_mb` | 8192 | Largest single region file for an upload or an import from a URL on the Regions page (1-200000). Uploads and imports also keep `min_free_space_gb` free (checked before, and every 64 MiB while streaming, also when the size is not stated) and are aborted below it. |
| `region_import_max_hours` | 6 | Overall time limit of one import from a URL, in hours (0-720, 0 = no limit). The partial download is kept and resumes. |
| `region_transfer_gzip` | true | Keep a gzip copy of every `routing.tar` and `places.sqlite` (`routing.tar.gz`, `places.sqlite.gz`, built in the background after the file is hashed) and offer it in the catalogue as `transfer`, so the car app downloads the compressed file and unpacks it (the installed file is the same). Costs about 30-50 % of those files in extra disk space. Off deletes the copies. See [Compressed downloads](#compressed-downloads-gzip-copies). |
| `charger_refresh_days` | 7 | The add-on builds the charger database for the car app itself, this often (1-365 days; 0 = automatic refresh off, the **Refresh now** button on the Regions page still works). See [Charger database](#charger-database). |
| `charger_source_url` | (empty) | Optional direct address of the Bundesnetzagentur Ladesaeulenregister CSV. Empty = the add-on finds the newest CSV link on the Bundesnetzagentur page. Set it when the Regions page says the landing page cannot be read or has changed (see *Troubleshooting the charger download*). |

## Status bar and library

The Ingress panel starts with a status bar:

- **Activity**: idle, checking a source, downloading (with a progress bar and **Stop**),
  processing or indexing. **Stop** ends the current download; the release is not counted as a
  failure and is fetched again on the next check.
- **Sources**: per source the last and next check, the result of the last check (for example
  `2 new, 5 unchanged, 1 failed`) and **Check now**.
- **Disk**: free space against `min_free_space_gb`, with a warning while waiting for space.
- **Sync API**: listening or why not, the number of files served, and names it had to leave out
  (empty files and symbolic links are skipped silently and are not shown). While the API answers
  `503` (library missing, unreadable or unexpectedly empty) a warning says why, with **Confirm**.

At the bottom, the **Library** section lists the top level of `library_dir` (it is listed again by itself when a release is
approved or something is deleted) and lets you delete a release folder or file (after a confirmation). The car app then removes its copy on the next sync; cyp
keeps its copies. Deleting also makes Car Media and Trips forget that release (or just the deleted
tracks), so the same archive can be imported again and its tracks no longer count as known. RSS
still never downloads a known URL again.

## Approving and declining

- **Ingress panel**: open *Car Media and Trips* in the sidebar and press Approve or Decline.
- **Persistent notification**: each pending release creates a notification with a link to the panel.
- **Mobile actionable notification**: set `notify_service` (for example `mobile_app_pixel_8`). The notification has Approve and Decline buttons; the add-on listens for the `mobile_app_notification_action` event with actions `MW_APPROVE_<id>` and `MW_DECLINE_<id>`.
- **Custom event** `car_media_hub_decision` with data `{"id": "<release id>", "decision": "approve" | "decline"}`. Fire it from any automation, script or Developer Tools -> Events.

Example script that approves a release by id:

```yaml
script:
  car_media_hub_approve:
    fields:
      release_id:
        description: Release id (the part after car_media_hub_ in the notification id)
    sequence:
      - event: car_media_hub_decision
        event_data:
          id: "{{ release_id }}"
          decision: approve
```

Example automation that declines every new release automatically when you are away, using the notification event:

```yaml
automation:
  - alias: Decline Car Media and Trips releases at night
    trigger:
      - platform: event
        event_type: mobile_app_notification_action
        event_data:
          action: MW_DECLINE_abc123def456
    action:
      - event: car_media_hub_decision
        event_data:
          id: abc123def456
          decision: decline
```

## RSS source

Set `rss_feed_url` to an RSS 2.0 or Atom feed (http or https) and every new feed item becomes one
release: its files are downloaded into `staging_dir/_downloads/rss/`, extracted and cleaned like
any other release, and then wait for your approval (or go straight to the library with
`rss_auto_approve: true`). The release is named after the item title, so approved items land in
`library_dir/<item title>/`.

When the feed is checked:

- once a day at `rss_check_time` (local time of the add-on), or every `rss_poll_minutes` when
  `rss_check_time` is `interval` (or `""` in YAML mode; the Configuration form cannot save an empty
  value);
- right after start when `rss_run_on_start` is on;
- when you press **Check now** in the status bar.

Each item is one release, identified by its `guid` (Atom: `id`), else its link, else its first
enclosure. Some feeds give every item the same link (the show's home page) and no guid: items that
share an id but point to different files (compared by their full enclosure URLs, so
`download.php?id=1` and `?id=2` differ) are then told apart, and the id becomes `<id>#<hash>`, a hash
of the item title and its enclosure URLs without the query string (a rotating token in the query
does not make an item new). Only items with exactly the same URLs count as one. Feeds that list just
their latest item under the shared link work too: a later item whose title or files differ from the
one handled before is downloaded under its own id. So every episode is downloaded. One limit: items
with the same title whose URLs differ only in a query that also carries a rotating token look like one
item. A malformed URL in one item is ignored (and logged)
instead of breaking the whole feed. A feed served without a charset is decoded with the encoding its
`<?xml ... encoding="..."?>` declaration names.

At most `rss_max_items_per_scan` items are downloaded per check; the rest wait for the next one.
URLs that were downloaded before are never fetched again, and tracks that are already in the
library are removed by the duplicate filter, so a re-published item only brings new tracks.
A failing item is retried on the next checks; after 3 failures it is parked and retried once a
day. A release larger than `max_release_size_mb` is not downloaded: feeds rarely declare sizes, so
the download stops when the limit is reached.

More feeds: add RSS sources to `/config/sources.json` (each with its own `name`; see
[Writing a source plugin](#writing-a-source-plugin)):

```json
[
  {"type": "rss", "name": "psy", "feed_url": "https://example.com/psy/feed/",
   "check_time": "04:30", "auto_approve": true}
]
```

Settings of an `rss` source: `feed_url` (required), plus the common `name`, `check_time`, `poll_minutes` (1440),
`run_on_start` (true), `max_items_per_scan` (10) and `auto_approve` (false).

### Which links are downloaded

Only the files a feed item declares itself: `<enclosure>` links (RSS) and `rel="enclosure"` links
(Atom). The add-on never opens an item's page and never looks for file links on it, so an item
without an enclosure is skipped by the automatic check. For those items, open them from the **RSS feed**
block on the **Media** page and download the file yourself, see [Download in the add-on browser](#download-in-the-add-on-browser).

File names come from the URL or from the server's `Content-Disposition` header (for links like
`download.php?id=5`). Only http and https links are followed.

Custom link parsers (your own `.py` files in `/config/parsers/`, `rss_parser`, `parsers_dir`) are not
supported. Files left in that folder are ignored, and a `parser` or `parsers_dir` key in
`sources.json` only logs a warning.

## Download in the add-on browser

Some sites hide the file behind several pages (a button, a form, a waiting page, a captcha). The add-on does
not click through them by itself; it gives you a browser of its own for that. It is **off by default**: set
`browser_enabled: true` and restart the add-on first. Read *Security of the browser* below before you do.

1. On the **Media** page open the **RSS feed** block (every item of the feed; the ones whose download is complete are marked **done**) and
   click an item's title. The page opens in a live Chromium that runs inside the add-on: you see it as a picture
   and click and type into it. The arrow next to a title opens the same page in your own browser instead.
2. Do the steps of the site. A download is accepted only right after your own click, key press or opened address
   (up to 5 seconds) and only **one** download per click, only if it is an archive and only if the page itself (not an
   embedded frame such as an advertisement) starts it; a file that a page starts by itself is cancelled and listed as
   *blocked*. Opening a feed item or reloading is not a click. Accepted files are saved in a private folder, not in your inbox.
3. When the download is complete the add-on copies it to its own *browser* inbox (`/data/browser-downloads`, not
   shared), which picks it up after `settle_seconds`, extracts it, cleans it and
   shows it on the **Media** page for approval. Releases from the browser are **never approved automatically**:
   neither `rss_auto_approve` nor `pending_timeout_action: approve` applies to them; you decide. The downloads of the open page are listed
   under the picture, and the item is marked **done** by itself when a file has arrived (opening an item does not mark it).
4. The browser stops by itself ten minutes after you leave the page (not while a download runs). Cookies are kept
   in `/data/browser-profile`, so a site you logged in to remembers you. Pages are never cached: every click on a feed
   item loads that item's page again.

**New tabs and pop-ups.** The browser has exactly one tab, the one you see. A link or form that wants a new tab
(`target="_blank"`) opens in that tab, and `window.open` only works right after your own click. If a page still manages
to open a new tab or window (advertisements do), it is closed at once, unless you clicked or pressed a key during the last
3 seconds and the tab was opened by the viewed page: then its address is loaded in the viewed tab (when it is an allowed
http(s) address) and the extra tab is closed. The viewed tab is never taken over by an ad that nobody asked for.

Limits: the inbox takes archives (`.zip .rar .7z .tar .tar.gz .tgz .tar.bz2 .tar.xz`) and folders. Other files are
cancelled (the list under the picture says so). The size limit is `max_release_size_mb`. It is Chromium without sound or
video playback. Some sites block automated browsers.

### Security of the browser

The browser opens untrusted, ad-heavy sites. **What is protected, and what is not:**

- The filter below (the proxy) refuses private addresses for everything an *honest, uncompromised* Chromium requests. It
  is **not** a boundary against a compromised renderer: code that runs inside Chromium can open network connections
  by itself, without the proxy. What stops that is Chromium's **sandbox** plus the unprivileged user. Therefore the
  browser **does not start** when the sandbox cannot work in the container (the usual case on Home Assistant OS)
  unless you set `browser_allow_unsandboxed`. With that option on, a single browser exploit gives an attacker
  network access from inside the add-on (the Supervisor and other add-ons on 172.30.32.0/23, your LAN) as the user
  `browser`; it cannot read the Supervisor token, the add-on's files or write outside the browser's folders, but
  that network access is real. If you cannot accept it, leave the browser off.
- Every folder the browser user can write to is treated as hostile by the add-on (which runs as root): it never follows
  links there. The download folder is outside the browser's profile, below a root-owned parent
  (`/data/browser-incoming`).

Measures:

- Chromium and Xvfb run as the unprivileged user `browser` (no shell, home = `/data/browser-profile`), never as root,
  with an explicit minimal environment: no `SUPERVISOR_TOKEN`, no Home Assistant or sync token. `run.sh` also makes
  the s6 environment folder root-only, and the add-on's `options.json` and database are mode 0600.
- The Chromium sandbox is required (checked at the first start). Most Home Assistant containers do not allow user
  namespaces; then the browser is not started and the viewer shows why. Only `browser_allow_unsandboxed: true` starts
  it with `--no-sandbox` (still as the unprivileged user, with the clean environment, behind the filter below, but
  see above: that does not contain an exploit).
- WebRTC may only use the proxy (`--force-webrtc-ip-handling-policy=disable_non_proxied_udp`, also set as a Chromium
  managed policy), so a page cannot send UDP/STUN/TURN to your LAN directly. The proxy forwards one plain-http request
  per connection (`Connection: close`, no pipelining) so each request is checked.
- All browser traffic goes through a small filtering proxy inside the add-on. Only http and https are allowed; names
  are resolved by the proxy, and a name that resolves to loopback, link-local, private (including Home Assistant's
  172.30.32.0/23 network), carrier-grade NAT or other non-public addresses is refused, as are single-label names such as
  `supervisor` or `homeassistant`, `.local`/`.lan`/`.internal` names and port 25. The proxy connects to the checked
  address itself, so DNS rebinding does not help a page. The add-on's own ports (8099, 8100) are unreachable.
- Limits: two renderer processes, a 384 MB script heap per renderer, two downloads at once, a session is ended after
  4 hours, and Chromium, Xvfb and any helper of the browser user are killed when the browser stops or the add-on
  shuts down.
- Chromium comes from Debian's own repository (the image is based on Debian trixie, `build.yaml`), where the Debian
  security team publishes Chromium updates. The version is **fixed when the image is built**, so it only becomes
  current again when you **rebuild or update the add-on**: do that regularly, and leave `browser_enabled` off if you do
  not use it. The add-on logs the Chromium version at every start and **refuses to start the browser** (with a
  prominent warning in the log) when the installed major version is below `MIN_CHROMIUM_MAJOR`
  (`app/car_media_hub/browser_proc.py`, currently 140; raised with add-on releases).
- Expect the browser option to stay **unavailable on Home Assistant OS**: the Chromium sandbox needs user namespaces,
  the sandbox probe usually fails there, and the browser then does not start (see `browser_allow_unsandboxed` above for
  the dangerous alternative).
- Downloaded archives are untrusted. `7z` and `bsdtar` never run as root: they run as the separate unprivileged user
  `extract` (uid 1011, no shell, no access to `/data` or the Supervisor token) with a clean environment, limits on
  memory, CPU time, file size, open files and processes, and write into a folder that user owns for the duration of the
  run. Entry names are validated first (no `..`, absolute or drive paths), links, devices and encrypted entries are
  refused, as are archives with more than 100,000 entries or more uncompressed bytes than the limit or the free disk
  space allows; the result is checked again afterwards (no links, no hard links, nothing outside the folder). zip and
  tar archives are read by Python's own libraries. The folders above the extraction folder must be enterable by others
  (the default).
- If the browser user's processes outlive "stop", the add-on kills them with `kill(-1, SIGKILL)` issued as that user
  (atomic for the whole uid), repeats the /proc scan as a backup, and caps the browser user's number of processes
  (`RLIMIT_NPROC`).
- The network service of Chromium runs as the browser user inside the add-on's network namespace; a bug there reaches
  the same network as `browser_allow_unsandboxed` would, even with the sandbox on. Prefer a separate add-on without the
  Home Assistant APIs for the browser if that worries you.
- `ws://` pages do not work through the filtering proxy (WebSocket upgrades are refused); `wss://` works.

A page can still abuse other public sites you are logged in to in this browser; use it for downloads only.

### Architectures

The add-on is built for `amd64` and `aarch64` only. `armv7` was dropped: Home Assistant deprecated its 32-bit base
images and Debian's Chromium on armhf is unverified. The Python dependencies (`aiohttp`, `mutagen`) come from Debian's
packages, so the build needs no compiler and no pip wheels.

### Archive extraction

`.rar` and `.7z` releases are unpacked by `bsdtar`/`7z` as the unprivileged user `extract` (uid 1011). After every run
(listing and extraction) every process of that user is killed, and once more when the add-on starts. Afterwards the
add-on takes the extracted folder back with a descriptor-based walk that never follows links: owner root, folders
0755, files 0644, ACLs and extended attributes removed, and everything that is not a regular file or folder (links,
FIFOs, sockets, devices) deleted. Extracted releases therefore reach the library with normal permissions, and
archives nested in subfolders of a `.rar`/`.7z` release are unpacked too. The `umask 077` applies only to the
extractor process, never to the add-on.

The browser is started from Debian's real binary `/usr/lib/chromium/chromium`, not the `/usr/bin/chromium` shell
wrapper (which sources files below `HOME`); if only the wrapper exists, `HOME` is `/nonexistent`.

### Chromium

Chromium (about 150-250 MB) is installed in the image from Debian's packages (`chromium`, `xvfb`, `fonts-freefont-ttf`,
`fonts-liberation`, for `amd64` and `aarch64`), so the browser works out of the box; nothing is downloaded at run time. The first
build takes a few minutes and the image is larger. If the browser says it cannot find Chromium, the image is an old or incomplete
build: update or rebuild the add-on and read the apt step of the build log. With `xvfb` (installed too) Chromium runs on a virtual screen as a normal, not
headless, browser; this passes the human checks of sites such as Cloudflare more often, but not always: they also
look at the graphics hardware, which a container does not have. If a check never passes, open the page with the
arrow next to its title in your own browser.

## Sync API

With `sync_enabled` on and a valid `sync_token`, the add-on serves `library_dir` on its own port
8100, separate from the web UI, for the **CarNavigationHelper** car app (and, optionally, **cyp**
directly). It speaks exactly the protocol of rss_downloader's content sync, so neither client needs
a change.

- `GET /api/v1/files` returns a JSON list of `name` (path relative to `library_dir`, e.g.
  `Album/CD 1/01.mp3`), `size`, `mtime` (when the file arrived in the library) and `sha256`
  (always `null`), newest first.
- `GET /api/v1/files/<path>` returns the file (each path part URL-encoded). Files are sent with `sendfile` (no copy through
  Python) and carry `Accept-Ranges: bytes`, a **strong** `ETag` (`"<size hex>-<mtime_ns hex>"`, quoted) and `Last-Modified`. One byte range
  is supported (`bytes=a-b` or `a-`, answer `206`; behind the end `416`); with `If-Range` the range is honoured when it equals the current
  ETag, or the exact `Last-Modified` of a file that was last modified more than a second ago (RFC 9110: a date is only a strong validator
  then); anything else sends the whole file. The ETag is the safe choice. Parallel range requests of one file are fine.
- Streams: one client (by address) may run up to 8 file or probe streams at once, the add-on 16 in total; above that the answer is `503` with
  `Retry-After: 2` and the body `{"error":"busy"}` (not to be confused with the `503` of an unavailable library, which carries
  `Retry-After: 300`). Every file response carries `X-Max-Streams: 8` (the per-client limit) so a client can size its worker pool.
  A stream that has accepted no data for 30 s (a car that fell asleep) is cancelled and its slot goes to the next request, at once; the same
  holds for a connection that is already closing. Progress is counted per 256 KiB, so a slow but moving connection (above about 8 KB/s) is
  never taken for stalled. When the add-on is at the total limit and one client holds at least two streams more than the asking one, that
  client's oldest stream is given up too (the app resumes it with a `Range` request), so two busy clients cannot lock out a third.
  The client is identified by its address on port 8100. Behind a reverse proxy, a VPN add-on or Docker's userland proxy all clients share one
  address and therefore one budget of 8 streams; `X-Forwarded-For` is deliberately not trusted (a client could fake it).
- `GET /api/probe?bytes=N` streams `N` bytes (default 8 MiB, at most 64 MiB; `mb=N` is a shorthand) of random data from memory (no disk) for
  measuring the network path: `Cache-Control: no-store`, exact `Content-Length`, `X-Max-Streams`, `X-Probe-Started`; it counts as a stream.
  `GET /api/probe/disk?bytes=N` reads up to `N` bytes (at most 64 MiB) of the largest library file and answers
  `{"bytes", "seconds", "cached"}` (the add-on's disk speed; `cached` is a guess). Only one disk probe runs at a time (another gets `503`
  busy), in a worker thread of its own so downloads and listings are not held up; `HEAD` answers the headers only (no read, no slot). The
  second run of a file is usually served from the page cache (`cached: true`).
- **Listing freshness.** A listing is reused for 30 s. After that the next request starts a rebuild in the background and is answered from the
  previous listing, but only while that listing is at most 120 s old; an older one is not served: the request waits for the rebuild (as
  before). So files added, renamed or deleted directly in the library folder (Samba, the NAS, the Home Assistant media browser) show up
  within 30 s plus the time of one rebuild, and never later than about 2 minutes. A cheap check (modification times of the library folder
  and its first-level folders, at most every 5 s) makes changes made directly in those folders visible within the 30 s as well, by
  rebuilding at once; changes deeper in the tree are picked up at the 30 s mark. New files stay hidden for 60 s after they appeared (the
  copy must finish). An approval or deletion through the add-on invalidates the listing at once, and a file that is asked for but not listed
  is looked for again (at most every 5 s).
- Every request needs `Authorization: Bearer <sync_token>`; without it the answer is 401.

Only audio files the clients can play (`.mp3 .flac .wav .ogg .m4a`) are listed. Names the clients
cannot store (characters such as `? : *`, names that differ only in case, very long names) are left
out, logged and shown in the status bar; releases imported by Car Media and Trips are already renamed so
this does not happen. When two **folders** differ only in case or Unicode form (`Pink Floyd/` and
`pink floyd/`), only one of them is served, always the same one (the one already published, also
after a restart, else the one that arrived first); every file of the other is left out as *folder
collides with ...*. Rename, merge or delete the folder reported as *folder collides with*, never the
served one: the car app holds the served spelling, and phone storage does not tell the two apart.
If the served folder of such a pair is deleted anyway, the other one is not listed either; the
status bar says *folder was published as X: rename it to X to list it*. A folder that is renamed only
in case or Unicode form (and keeps exactly the same files) is still listed under its old spelling, so
the phone neither deletes nor duplicates it. A deleted folder with no twin is forgotten: if you later
add a folder of the same name in another case, reuse the exact old spelling, or the phone may keep a
duplicate. Empty (0-byte) files and symbolic links
are also left out, without a message. A newly approved release appears in the list after about 1-2
minutes.

**Missing library.** The add-on does not create `library_dir` when it starts. While the folder is
missing, is not a folder or cannot be read (for example network storage whose server is down: the
add-on still starts), the API answers `503` with `library_unavailable` (and the Log warns at start),
so the car app deletes nothing. Create the folder or fix the `library_dir` option, or bring the
storage back. A `staging_dir` or `inbox_dir` that lies inside a missing `library_dir` is not created
either (the Log names it), because that would create the library. While the sync API is enabled
and `staging_dir` lies inside the missing `library_dir`, nothing is downloaded or imported either (the
status bar and the *library unavailable* notification say why) until the folder exists or you press
**Confirm**. The default layout keeps staging outside the library
(`/media/.car_media_hub/staging`). While the sync API is enabled, or after rss_downloader's history was
imported, an approval does not create a missing `library_dir` either: the
release stays pending and a persistent notification says why, until you create the folder or press
**Confirm** in the status bar (then the next approval creates it). The status bar shows this
reason and the **Confirm** button even when the sync API is off or not listening. **Confirm**
is offered only where it can help (a missing, new or deliberately empty folder), not for a folder that
cannot be read: restore access to the storage instead, and everything resumes by itself.

**Empty-library guard.** If the library suddenly looks empty (for example a disk that is not
mounted) although it had files, the API answers 503 instead of an empty list, so the car app does
not delete its music. The guard is only lifted when deletions in the Library section left the
library with nothing to list, or when you press **Confirm**; after a partial deletion it stays armed.
The status bar shows *library unexpectedly empty* while it holds; press **Confirm** if the library
really is meant to be empty. Approvals wait while the library looks missing or unexpectedly empty (a
persistent notification *library unavailable* says so), because one approved release in an empty
mount point would turn the 503 into a short list and the car app would delete everything else. This
also holds with the sync API turned off, once it has published something: the status bar then shows
the reason and **Confirm** on its own (*Approvals are on hold: ...*).

The traffic is plain HTTP, so the token is only protected on your home network. Use a reverse
proxy with HTTPS for access from outside.

### Trips and energy

The car app can upload finished trips (and, optionally, power/SoC samples) to the same port 8100 as the
library sync. It needs the same `sync_enabled` and `sync_token`, and the same header
`Authorization: Bearer <sync_token>`; without it every answer is 401. The full contract with examples is in
`docs/TRIPS_API.md` of the repository. In short:

- `POST /api/trips`: a JSON **list** of trips, at most 1000 items and 5 MB (413 beyond that). A trip is
  identified by its `id` (uuid), so sending it again replaces it. The answer is
  `{"inserted": n, "updated": n, "rejected": n, "errors": [{"index", "id", "reason"}]}`; valid items are
  stored even when others are rejected.
- `GET /api/trips?since=<iso>&before=<iso>&limit=<1..1000>&shape=0|1`: newest first (by `started_at`),
  `since` is exclusive; `next_before` in the answer pages to older trips.
- `GET /api/trips/{id}`, `DELETE /api/trips/{id}` (also removes its samples).
- `POST /api/trips/{id}/samples`: list of `{trip_id, t, soc, power_kw, speed_kmh, lat, lon}`;
  `GET /api/trips/{id}/samples` returns them.
- `GET /api/stats`: `total` and `last_30_days` with `trips`, `km`, `kwh` and `avg_kwh_per_100km`.

Trip fields: `id`, `started_at`, `ended_at` (ISO 8601 with zone, UTC), `start_lat`, `start_lon`, `end_lat`,
`end_lon`, `distance_m`, `duration_s`, and optional `energy_kwh`, `soc_start`, `soc_end` (percent),
`avg_consumption_kwh_per_100km`, `ext_temp_c`, `shape` (polyline, precision 6), `source` (default
`car-app`), `vehicle`.

**Home Assistant entities** (created by the add-on, updated after each accepted upload or deletion and at
start; Home Assistant forgets them when it restarts until the add-on publishes again, so restart the
add-on after a Home Assistant restart or send any upload):

| Entity | Meaning |
|---|---|
| `sensor.car_media_hub_last_trip_distance` | km of the trip that ended last |
| `sensor.car_media_hub_last_trip_consumption` | its kWh/100 km (reported value, else derived from kWh and km) |
| `sensor.car_media_hub_last_trip_end` | its end time (timestamp) |
| `sensor.car_media_hub_total_distance` | km of all trips |
| `sensor.car_media_hub_month_energy` | kWh of trips that started in the current calendar month (UTC) |
| `sensor.car_media_hub_average_consumption` | kWh/100 km over all trips that have a kWh value |

### EV settings

`GET /api/settings/ev` and `PUT /api/settings/ev` (same token) store the car app's EV settings (operator
preferences, tariffs, owned cards, default tariff, battery and vehicle choice) as one JSON document with a
revision. The app uses them to keep its settings after a phone or box change. `PUT` sends
`{"base_revision", "settings"}` and gets `409` with the current document when the revision is stale;
bodies are limited to 1 MB. Contract: docs/TRIPS_API.md, section *EV settings*.

### Setting it up

1. In the **Configuration** tab turn on `sync_enabled` and enter a `sync_token`: at least 16
   characters, letters, digits and punctuation, no spaces. Keep it; the clients need the same value.
2. In the **Network** section of the same tab enter `8100` as the host port for `8100/tcp` (it is
   empty by default) and **Save**. Use another host port, for example 8101, if 8100 is taken.
3. Restart the add-on. The Log says `Sync API listening on port 8100`, or
   `Sync API not started: <reason>`; the status bar shows the same.
4. Find the address of your Home Assistant machine on your network (for example `192.168.1.10`,
   in **Settings -> System -> Network**) and test from a computer on the same network:

   ```
   curl -H "Authorization: Bearer YOUR-TOKEN" http://192.168.1.10:8100/api/v1/files
   ```

   A JSON list means it works; `401` means a wrong token; a timeout or "connection refused" means
   the port is not reachable (check steps 1-3); `503` with `library_unavailable` means `library_dir`
   is missing or cannot be read (check the option and the storage behind it).

**Car app (CarNavigationHelper):** in **Content sync** enter the server address
`http://192.168.1.10:8100` (your Home Assistant address and the host port) and the same sync token,
then tap **Sync now**. Choose your home Wi-Fi under **Wi-Fi network for automatic sync** to sync
automatically. The car app keeps the newest files (by arrival time) that fit its storage limit.

**cyp:** in the usual chain (Car Media and Trips -> car app -> phone -> cyp) cyp keeps pointing at the car
app: the phone's address and the car app's key in cyp's single **App sync** setting (called
*Second sync* in cyp's README). It needs no change; only the car app is pointed at Car Media and Trips.
cyp can also sync straight from Car Media and Trips (enter `http://192.168.1.10:8100` and the sync token
there, then **Save** and **Sync now**), but cyp has only one such source: this **replaces** the
phone, so away from your home network cyp cannot sync at all. Use it only for a cyp that stays on
your home network. cyp copies new files into the MPD music folder and never deletes files.

| What you see | What to do |
|---|---|
| Log or status says `Sync API not started` | Enable `sync_enabled`, check the token (16+ printable characters, no spaces), restart |
| The list answers `503` `library_unavailable` | `library_dir` is missing, not a folder or unreadable (a network share that is down, a permission or I/O error): fix the option or the mount |
| The add-on does not start after entering 8100 in the Network section | Another add-on (for example rss_downloader) still publishes host port 8100: stop it, or use another host port |
| "Cannot reach the server" in the car app | Check the address, the host port in the Network section (rss_downloader must not hold it), and that the phone is on the same Wi-Fi |
| "Wrong token" | Copy the token again: it must match exactly |
| A file is missing in the list | Check **Sync API** in the status bar for left-out names (empty files and symbolic links are not listed there); new releases need 1-2 minutes |

## Offline map regions

The add-on can host offline map regions (map, routing tiles and an address database) for the car app, which downloads them
from the add-on when it is reachable. The regions are plain files in `region_storage_dir` (default
`/share/car_media_hub/regions`, the Samba share `share`), one folder per region:

```
/share/car_media_hub/regions/<regionId>/
    map.pmtiles       PMTiles v3 map (planetiler, OpenMapTiles)
    routing.tar       Valhalla tile extract
    places.sqlite     address and place database
    manifest.json     id, name {de,en}, bounds [west,south,east,north], version, source, license, created, routingGroup
    files.json        written by the add-on: size, mtime and SHA-256 of each file (do not edit)
    routing.tar.gz    written by the add-on (option region_transfer_gzip): gzip copy for the download (do not edit)
    places.sqlite.gz  written by the add-on, likewise
    transfer.json     written by the add-on: size, mtime, SHA-256 of each .gz and the plain file it was built from (do not edit)
    want.json         written by the add-on (Regions page): wanted, wantedAt, wantedBy, removeRequested, removeAt (do not edit)
```

Reports of car apps are kept in `<region_storage_dir>/.devices/<deviceId>.json` (written by the add-on, see below).

**Backups and disk space.** `/share` and `/media` are part of Home Assistant full backups, so tens of GB of region packs enlarge every
full backup; to keep them out of backups use `region_storage_dir: /data/regions` (excluded from add-on backups via `backup_exclude`; the
files then live in the add-on's data partition and are not visible over Samba). Uploads and imports stop when less than
`min_free_space_gb` (default 50) would stay free: on a small host lower that option or put the packs on the share by hand.

`regionId` matches `[a-z0-9][a-z0-9_-]{1,40}`. Every file is optional, but a region is only listed in the catalogue when it
has a map. `manifest.json` example:

```json
{"id": "oberpfalz", "name": {"de": "Oberpfalz", "en": "Upper Palatinate"}, "bounds": [11.6, 48.8, 13.0, 50.0],
 "version": "2026-11", "source": "Geofabrik oberpfalz-latest.osm.pbf", "license": "Data (c) OpenStreetMap contributors, ODbL",
 "created": "2026-11-16T10:00:00Z"}
```

### API (port 8100, same token and base address as the rest of the sync API; needs `sync_enabled`)

- `GET /api/regions/catalogue` is JSON in the format of the car app's `regions.json` version 2. Each region has `id`, `name`
  (English, else German), `names` `{de, en}`, `bounds`, `version`, `updatedAt`, `source`, `license`, `routingGroup`,
  `state`, `install`, `remove`, `installRevision`, `removeRevision` and `files`; each file has `kind` (`map`, `routing`, `places`), `name` (the file name the app stores:
  `<id>.pmtiles`, `<id>-tiles.tar`, `<id>-places.sqlite`; the routing file of a routing group is `<routingGroup>-<sha8>-tiles.tar`, see below), `url` (relative to the add-on base address, for example
  `/api/regions/files/oberpfalz/map.pmtiles`), `bytes` (`sizeBytes` repeats it), `sha256`. Only regions with a map file are listed.
  The SHA-256 of a file is computed once in a background thread and stored in `files.json`; until then the region has
  `"state": "hashing"`, `"hashing": [{"kind": "map", "progress": 0.42}]` and an empty `files` list, so a client never offers a
  file whose hash is not known (a file whose modification time is younger than 30 seconds, or whose size changed between two scans, waits for the copy to finish first and is neither listed nor served). The answer
  is `503` while `region_storage_dir` cannot be read.
- **Install / remove requests (pull model).** The phone or car box cannot be reached from Home Assistant, so the owner's wish is
  published in the catalogue and the app acts on it when it checks in. Every catalogue region has two booleans:
  `"install": true` after **Install on car app** was pressed on the Regions page (only while the region is `ready`: a region
  that is still hashing is offered when its hashes are known) and `"remove": true` after **Remove from car app** (cleared when
  every known device reported the region `absent` or `removed` after the request, else after 30 days; choosing install again
  cancels it). Both are `false` otherwise; old clients ignore the fields. A client installs what has `install: true` and removes
  what has `remove: true`; it must not delete a region merely because `install` is `false`.
  `installRevision` and `removeRevision` (strings, additive) identify the request: `installRevision` is the `wantedAt` time of the
  current install request (`""` while none is active), `removeRevision` the time of the current remove request. Each click on
  **Install on car app** / **Remove from car app** produces a new value, so an app can tell a NEW request from an old flag and let
  a user's local delete or cancel win until the revision changes.
- **Install queue and cancel (additive fields).** The car app installs one region at a time. `installOrder` (integer) is the
  position of the region among the regions that are wanted, in the order of the install requests (1, 2, 3 ...; `0` = not wanted);
  take the region with the smallest `installOrder` first and queue the others behind it. A region that is asked for again keeps its
  place, a new request (also after a cancel) goes to the end, a cancelled one drops out and the others move up; the queue lives in
  `want.json` (`wantedSeq`) and survives an add-on restart. **Install all** (`POST /regions/api/install` with `{"all": true}` or
  `{"ids": [...]}` in the page API) accepts many requests at once. **Cancel install** is a state of its own: `"cancel": true` and
  `"cancelRevision"` (time of the cancel, `""` otherwise) with `install: false` and an empty `installRevision`. The app must stop
  installing that region (a running download is aborted, a queued one is dropped; completed files stay, nothing is deleted unless
  `remove` is true). It stays until a new install (`install: true` with a new `installRevision`) or removal request replaces it.
  An install request never expires because a device is silent (devices are only forgotten after 90 days; the request stays).
  A region that gets a new file kind by an upload or import right now has `"state": "updating"`, `"updating": [{"kind"}]`,
  `files: []` and `install: false` (like `hashing`); a file that is replaced stays listed and served until the atomic rename.
- `POST /api/regions/devices/report` (bearer token, `Content-Type: application/json`, body at most 8 KB; answers `{"ok": true}`,
  `400` bad content, `413` too large, `415` wrong type, `429` more than 20 devices) lets an app say what it has. Body:

  ```json
  {"deviceId": "0f8c2a9e-5b1d-4c7e-9a31-2d6b7e4a1c55", "name": "Car box", "appVersion": "1.4.0",
   "regions": [{"id": "oberpfalz", "state": "downloading", "version": "2026-11", "progress": 43.5,
                "message": "", "bytesDone": 1800000000, "bytesTotal": 4200000000}]}
  ```

  `deviceId` matches `[A-Za-z0-9_-]{8,64}` (a UUID the app keeps), `name` at most 60 characters, `appVersion` at most 40, at
  most 100 regions per report. Per region `id` (valid region id), `state` (`installed`, `downloading`, `queued`, `failed`,
  `removed`, `absent`; `paused`/`interrupted` (stored as `paused`) and `cancelled` are accepted too), `version`, `progress`
  (0-100), `message` (at most 200 characters, truncated), `bytesDone`, `bytesTotal`
  (whole numbers); wrong types or an unknown `state` give `400`. The add-on keeps the largest byte count a device reported for a
  region, so a `queued` or network-`failed` report after a wake-up (which carries no byte counts) still shows how far it got. A report replaces the previous one of the device, so list every
  region of the catalogue the app knows (`absent` for those it does not have). Devices are stored one file each
  (atomic writes), at most 20, and dropped after 90 days without a report.
- `GET /api/regions/devices` (bearer token) returns `{"devices": [{"deviceId", "name", "appVersion", "lastSeen", "ageSeconds",
  "running", "queue": [{"id", "position"}], "regions": [{"id", "state", "phase", "version", "progress", "message", "bytesDone",
  "bytesTotal", "doneBytes", "totalBytes", "ageSeconds", "queuePosition", "cancelAcknowledged", "updatedAt"}]}]}` for tooling.
  `state` is what the app reported; `phase` is what to show: `paused` for a `downloading` report that is older than 3 minutes (the car
  is asleep) or a `failed` report with a connection-type message ("Connection lost", "HTTP 5xx", "Queued until Wi-Fi") from an app that
  made progress, and `cancelled` for a region whose install the owner cancelled (`cancelAcknowledged` once the device reported
  after it). `doneBytes`/`totalBytes` are the best known progress. `running` is the region being downloaded, `queue` the queued ones
  in install order.
  **Estimated sync time.** An app may add `bytesPerSecond` (number 0 to 1e9, optional, missing = unknown) to a region of its report;
  without it the add-on derives the rate from consecutive `downloading` reports of the same region (bytesDone delta over the report
  times, exponential average with a 60 s time constant; a restart from zero, a gap longer than 3 minutes, another state or version
  restart the measurement; the rate is stored with the device and forgotten after 10 minutes). Per region the devices JSON then has
  `bytesPerSecond`, `rateSource` (`app`, `derived` or `""`), `etaSeconds` / `etaMinutes` (only while the device is downloading and
  the rate is at least 50 KB/s; minutes are rounded to whole minutes under 2 h, else to 5 minutes) and, for queued regions,
  `startsInSeconds` (the bytes left of the running region and of the queued regions ahead of it, divided by the running region's rate).
  Per device `estimate` is `{state, count, bytesLeft, bytesPerSecond, etaSeconds, etaMinutes}` with `state` `eta`, `measuring` (rate
  unknown or too low, or a size is unknown: no number), `paused` (nothing is moving) or `idle`. The page shows
  "all N queued regions: about 3 h 20 min (X GB left at Y MB/s)". Regions of one `routingGroup` share one routing archive, which the app
  downloads once under the region that runs first: the add-on counts that file once per device (the region that moves carries it, the
  others only count their own part; an installed region of the group means the file is on the device), and a region that still has
  0 bytes while another region of the group downloads gets `waitingForShared: true` and `sharedRouting` `{group, bytes, doneBytes,
  carrier}` (the page says "waiting for the shared routing file (X of Y GB done)" instead of 0 %); `leftBytes` is the remaining
  size per region.
  **Done and pending on the page.** A wanted region that every known device reports `installed` (in the catalogue version, or with no
  version) shows "Installed on the car app (n of m devices)" without queue wording; the request stays in `want.json` (the app needs it).
  Regions that are not done are pending: `installOrder` on the page API and `queue` / `queuePosition` in `/api/regions/devices` count
  only them, dense, in request order (the catalogue's `installOrder` still counts all wanted regions, `requestOrder` on the page API
  repeats it). A wanted region installed in an older version than the catalogue is "update pending" (`updatePending`, with a queue
  position among the pending ones); `current` tells per device region whether the installed version is the catalogue's. A device with
  nothing pending says "no installs pending".
  **Which code runs.** The Regions page ends with a grey line `build <id> · started <time> · v<version>`, and `/regions/api/list` and
  `/api/regions/devices` carry the same as a top-level `build: {id, started, version}`; the add-on logs `car_media_hub build <id>` at
  start. `id` is the first 10 hex characters of the SHA-256 over every `*.py` file below `app/car_media_hub` (sorted by relative path;
  per file: the path, a zero byte, the bytes, a zero byte). `python tools/addon_build_id.py <path>/app/car_media_hub` prints the id of
  a folder (compare it with what the page shows). `version` is read from `config.yaml` (copied to `/app/config.yaml` by the Dockerfile).
  The page also shows the note that it is an estimate that depends on Wi-Fi and on
  the car being awake (English or German).
- `GET` and `HEAD /api/regions/files/<id>/<name>` serve `map.pmtiles`, `routing.tar` or `places.sqlite` and nothing else (only a file whose SHA-256 is known and whose size and modification time still match it; a file that is being written or has just been replaced answers `503` with `Retry-After`) (the
  id must match the pattern above; links, folders and any other name give `404`). One byte range is supported (`bytes=a-b`,
  `a-`, `-n`, answer `206`; a range behind the end gives `416`; several ranges or a malformed header are ignored and the whole
  file is sent), with `ETag` (the strong ETag is the file's SHA-256 in quotes, `"<64 hex>"`, the same value as the catalogue's `sha256`), `Last-Modified` and
  `If-Range` (a range request is only honoured when `If-Range` names the current ETag or date, else the whole file is sent). The hash comes from `files.json`
  (valid while size and modification time match), so a resumed request right after an add-on restart is served without reading the file again. Files are sent with `sendfile` (in 256 KiB steps); one client may run 8 downloads at once and the add-on 16 in total (more get
  `503` with `Retry-After: 2`; every file response says `X-Max-Streams: 8`; a download that has not accepted data for 30 s, for example of a car that fell asleep, gives up its slot to the next request). Like all of the sync API the requests need `Authorization: Bearer <sync_token>` and only
  `GET`/`HEAD` are accepted.
- `GET` and `HEAD /api/regions/files/<id>/routing.tar.gz` and `.../places.sqlite.gz` serve the gzip copies exactly like the plain
  files (range, `If-Range`, strong `ETag` = the SHA-256 of the `.gz` bytes, `X-Max-Streams`, the same stream budget); only a copy the
  catalogue offers is served, else `404` or `503`.
- Neighbouring regions can share one routing archive (Valhalla routes cannot cross the border of separate archives): give the
  regions the same `routingGroup` in their manifests and put `routing.tar` into one of them (the first by id that has one;
  the files of the others are ignored). Every region of the group then lists the same routing file (`name`
  `<routingGroup>-<first 8 hex digits of its sha256>-tiles.tar`, same `url`, `bytes` and `sha256`), so the app downloads it once. The name
  depends on the content, so two different routing files never share a name (a region that has its own `routing.tar` and no group still
  uses `<id>-tiles.tar`, which can therefore never equal a group's name). The name is plain and ends with `-tiles.tar`; the app only needs
  it to be unique and to carry the extension of its kind.

### Compressed downloads (gzip copies)

With `region_transfer_gzip` (default on) the add-on keeps a gzip copy next to every `routing.tar` and `places.sqlite` of at least 1 MiB
(maps are not compressed: their tiles are gzip inside already). A single background thread builds them one after the other once the
plain file is hashed and no upload or import writes it (gzip level 6, one member, `mtime` 0 and no file name in the header, so the same
file always gives the same bytes). The catalogue then has an extra, optional `transfer` object in that file entry:

```json
{"kind": "routing", "name": "oberpfalz-tiles.tar", "url": "/api/regions/files/oberpfalz/routing.tar", "bytes": 107581440,
 "sizeBytes": 107581440, "sha256": "<sha of the plain .tar>",
 "transfer": {"encoding": "gzip", "url": "/api/regions/files/oberpfalz/routing.tar.gz", "bytes": 41234567, "sha256": "<sha of the .gz bytes>"}}
```

- `url`, `bytes` and `sha256` still describe the plain file the app installs; a car app that does not know `transfer` ignores it and
  downloads the plain file as before. A current app downloads the `.gz` (resumable, parallel ranges like any region file), checks its
  size and SHA-256, unpacks it and checks the plain size and SHA-256 before it installs the file.
- For a routing group the `transfer` url points to the copy in the providing region, like the plain url.
- A copy is offered and served only while it was built from the plain file with the listed `sha256` (`transfer.json` records the
  source hash and size). When the plain file is replaced (upload, import, Samba copy) the old copy is no longer offered, it is deleted
  once the new file is hashed, and a new one is built. Deleting a file on the Regions page deletes its copy too. Unfinished copies
  (`.gzip-<kind>-<hex>.part`) are removed at the start and after 24 hours.
- A file that does not get below 95 % of its size (random-like data) is recorded as `skipped` in `transfer.json` and sent plain; it is
  not tried again until the file changes. A build needs free space for one more copy of the plain size plus `min_free_space_gb`; without
  it the file is sent plain and the build is tried again at a later rescan.
- Disk: the copies take about 30-50 % of the routing and places files (a places file measured 29 %: 50.5 MB -> 15.1 MB). The Regions
  page shows per file `compressed 15.1 MB`, `compressing 40%` or `sent uncompressed (does not compress)`.
- Note that `region_max_file_mb` (default 8192 MB) limits uploads and imports: a routing file above 8 GB needs a higher value.

### The Regions page

The **Regions** button (next to **Trips**) lists the regions with sizes, hash state and version and lets you:

- **create** and **delete** a region and **edit** its name, bounds, version, source, license and routing group;
- **upload** a file (a streamed upload into a temporary file in the same folder, checked and renamed atomically; the header
  must be a complete 127 byte `PMTiles` version 3 header, an `SQLite format 3` header with a valid page size, or a tar whose first
  header has `ustar` and a correct checksum; limit: option `region_max_file_mb`; the free space reserve `min_free_space_gb` is kept
  also while streaming, and a failed upload leaves no part file);
- **import from URL** (https only, no address of a private, local or internal network, none of this add-on's ports, no
  credentials in the address; the size limit applies; an optional expected SHA-256; it runs in the background with a progress
  bar and a **Cancel** button; a cancelled or broken import resumes with the same address, but only when the first answer had a strong
  ETag or a `Last-Modified` and the server confirms it (`If-Range`), else it restarts at 0; an import stops after
  `region_import_max_hours`; after a full disk the partial file is deleted). Imports run one at a time in the order requested (the others show
  `queued (queue position n)` and can be cancelled); the queue is kept in `.import-queue.json` in the regions folder (it holds the
  addresses, so keep that folder private) and an add-on restart continues the running and the waiting imports by itself, a part that
  is cut off by a lost connection resumes after a pause (5 s up to 5 min) without a click, and a part is cut back to its last fsynced
  length before it resumes. A cancelled import does not come back. The SHA-256 computed while streaming is stored at once. Partial imports and unfinished uploads are listed under
  the region with a **Discard** button and are deleted automatically after 24 hours (at start and on every rescan);
- press **Install all** to request every region with a map at once (the car app installs one after the other in this order);
- press **Install on car app** (becomes **Cancel install**) per region: the car app installs it the next time it checks in, no
  clicking in the app is needed; **Remove from car app** (with a confirmation) makes the app delete it again (the files stay on
  the add-on). Under each region a line per known device shows `installed <version>`, `downloading 43 %`, `queued`,
  `failed: <message>`, `paused - last seen 12 min ago, 3.4 GB of 12.1 GB (continues when the car app runs again)`, `install cancelled` or `waiting for the app to check in (last seen 12 min ago)`, with the queue position of waiting regions (the page speaks German when the browser language is German); **Car app devices** lists name, last seen
  and app version, with a **Forget** button per device and **Forget all devices** (a device that reinstalled the app would otherwise
  keep a removal request pending; devices not seen for 90 days are dropped automatically; a forgotten device is listed again when it reports); The status refreshes every 5 s while a wanted region is not yet installed (otherwise every 30 s);
- **rescan** after you copied folders by hand (the add-on also rescans every 10 minutes, no restart is needed).

Like the other pages, it is reachable only through Home Assistant ingress, and every change needs the page's own request
header (CSRF protection). Uploads and imports never leave a half-written file under its real name.

### Charger database

The EV trip planner of the car app needs a charger database. The add-on builds it by itself: every `charger_refresh_days` days (default 7;
the first build starts about 2 minutes after the add-on starts when there is none yet) it downloads the Bundesnetzagentur
Ladesaeulenregister CSV (CC BY 4.0, about 55 MB, streamed to a temp file), converts it in a worker thread (only "In Betrieb" sites with a
position in Germany and a power value; the same converter and the same SQLite schema as `tools/build_charger_db.py` of the app repo),
checks the result (schema, integrity, at least 20000 sites, not below 60 % of the current file) and replaces
`<region_storage_dir>/.resources/chargers/chargers-de.sqlite` atomically. On any failure the old file stays and the Regions page shows the
reason. The sync API (`GET /api/resources/manifest`, `GET /api/resources/files/chargers/<name>`, same token) lists the file with its
SHA-256 (the file response carries it as strong `ETag`, supports one byte range and is sent with `sendfile`; the hash is cached by size, modification time, inode and ctime of the file,
never recomputed per request; a file replaced with the same size and a preserved modification time is still noticed); the car app checks it weekly and downloads it when the hash changed. A register with the same date as the current database does
not replace it (the hash and the app's download stay the same).

On the **Regions** page the line **Charger database** shows the last successful update (time, number of charge points, date of the register),
the next automatic run and the last error, and **Refresh now** starts a run at once (only one build runs at a time). Messages you may see:
"no connection to the source (is the add-on online?) - ..." (see *Troubleshooting* below; retried after 15 minutes, after further failures every 6 hours), "the source changed its format ..."
or "no register CSV link found ..." (the Bundesnetzagentur changed the file or the page: set `charger_source_url` to the CSV address, or build
the file by hand with `tools/build_charger_db.py` and copy it to the folder above), "refused as probably broken" (the new file was much smaller
than the current one). Memory use is small (rows are inserted in batches; about 20 MB of disk for the result, 55 MB temporarily for the
download, in `.resources/tmp/`). A file you copy there by hand with another name is served as well; the app uses each charger id once.

**Troubleshooting the charger download.** Every error names the step that failed, the host, the seconds spent, the bytes received so far,
the attempt and the exception class, for example `no connection to the source (is the add-on online?) - landing page
(www.bundesnetzagentur.de) failed after 95.3 s, 0 bytes received (attempt 3 of 3): SocketTimeoutError: Timeout on reading data from socket`.
The steps are *landing page* (the Bundesnetzagentur page that links to the CSV), *CSV download*, *convert* and *validate*; the same text
is in the add-on log at WARNING level, and while a run is active the Regions line shows the step, host, attempt and MB received.
A timeout while reading means the host connected but the other side sent nothing (a firewall, a proxy, a broken IPv6 route or the
Bundesnetzagentur site blocking the request); it is not a problem with the file. The add-on already uses IPv4 only and a browser-like
User-Agent, retries each HTTP step 3 times (pauses of 5 s and 20 s), retries a failed run after 15 minutes (later failures after 6 hours),
and, when the landing page cannot be used, falls back to the address found in the last successful run and then to the dated file name
(`.../Ladesaeulenregister_BNetzA_<YYYY-MM-DD>.csv`, the last 60 days, newest first). If it still fails, open the CSV address in a browser
(copy the current link from the Bundesnetzagentur page "Ladesaeulenkarte") and enter it in the option `charger_source_url`: the add-on then
skips the landing page and downloads only that file (`data.bundesnetzagentur.de`). Remove the option later to follow new registers
automatically; a fixed address keeps delivering the same register until you change it.

### Preparing a region offline (recommended)

Building a map needs gigabytes of RAM and hours of CPU, so build the pack on a PC and copy it:

1. In the CarNavigationHelper project, run `tools/build_region_pack.py` (see `tools/README_REGION_PACKS.md`): it builds or
   collects `map` (planetiler), `routing` (Valhalla `tiles.tar`) and `places` (`tools/build_address_db.py`) for the region.
2. Copy the folder to the share as `/share/car_media_hub/regions/<id>/` (build with `--no-compress`, or do not copy the
   `*.gz` files the tool also writes: the add-on makes its own compressed copies and ignores those) (Samba `\\<ha>\share\car_media_hub\regions\<id>`). The
   names `<id>.pmtiles`, `<id>-tiles.tar` and `<id>-places.sqlite` that the tool writes are renamed to `map.pmtiles`,
   `routing.tar` and `places.sqlite` by the add-on; add a `manifest.json` (or create the region on the page first and copy
   the files into its folder).
3. Press **Rescan** (or wait up to 10 minutes). The catalogue lists the region once its files are hashed and the car app downloads
   it the next time the add-on is reachable.

Each region in the catalogue should carry the attribution of its sources in `license`; OpenStreetMap data needs
"(c) OpenStreetMap contributors" (ODbL).

### Phase 2 (design only): building regions inside the add-on

An optional builder (Geofabrik PBF download, `osmium extract`, planetiler, Valhalla, address database) is only designed, not
implemented, and would not be enabled by default: a Raspberry-class Home Assistant host would need several GB of RAM and
tens of GB of disk for a region of Germany's size and hours of full CPU load, which can starve Home Assistant itself. See
[docs/REGION-BUILDER-DESIGN.md](docs/REGION-BUILDER-DESIGN.md).

## Trips page

The trips that the car app uploads can be browsed in the add-on's own web UI. Open it from the Home
Assistant sidebar: click **Car Media and Trips** (if the panel is not shown, go to
*Settings > Add-ons > Car Media and Trips* and switch on **Show in sidebar**), then choose
**Trips** in the navigation at the top of the page.

- **Cards**: number of trips, total km, total kWh, average kWh/100 km (energy over the km of the trips
  that have energy) and the last 30 days.
- **Overview map**: all trips of the selected month (or all trips) on one map, like the GPS logger.
  A trip with an uploaded track is drawn as a line, otherwise as a dashed straight line between start
  and end. Colour is the consumption from green (low) to red (high) with a legend; grey means unknown.
  Click or tap a line to see date, distance, duration, consumption and a **Details** link. Hovering a
  row of the list highlights the trip on the map, clicking a row zooms to it. To stay light, the newest
  200 trips keep their full track and older ones are thinned to about 40 points.
- **Consumption chart**: kWh/100 km of the last 20 trips (plain SVG).
- **List**: newest first, 50 per page, month filter, SOC start to end, source (sources such as
  `e2e-test` or `mock` get a *test* badge). Times are shown in the browser's time zone.
- **Trip page**: all facts, a route map and a **Delete trip** button (asks for confirmation; the trip
  and its samples are removed). When no track was uploaded the page says so: enable
  *Include route track* in the car app.
- **CSV**: `CSV` downloads all trips, one row per trip, UTF-8 with BOM, separated by semicolons;
  `CSV (1,5)` uses a decimal comma for German Excel.

Map tiles come from OpenStreetMap (© OpenStreetMap contributors): the add-on fetches each tile once
and caches it in `/data/tile_cache`. Without internet access on the Home Assistant host the maps show
the routes on a plain background. Everything else works offline; there are no external scripts.

### Troubleshooting a bare 401

If a page of the add-on shows only `401` (or the page stays empty and a banner says *Session expired - reload this page*), the add-on
did not refuse you: it only ever answers `403` to requests that do not come from Home Assistant. The `401` comes from Home Assistant's
ingress layer, which checks a short-lived ingress session cookie before it forwards a request. The cookie expires after a while, for
example in a tab that was left idle, in the companion app after it was in the background, or when a page was opened from a bookmark
or browser history of `/api/hassio_ingress/<token>/` (such a link carries no fresh session). A long Browser viewer session on the
Media page can run into it as well.

The pages notice this themselves: a `401` on a request of the page (including the polling and the Browser viewer connection) shows the
banner with a **Reload** button and stops the page's requests. Press **Reload**; Home Assistant then issues a new session. Text typed
into a form stays on the page until you reload, so copy it first if you need it.

To avoid it: always open the add-on from the Home Assistant sidebar entry *Car Media and Trips* (or *Settings > Add-ons > Car Media and
Trips > Open Web UI*), not from a bookmark of the ingress address. Open the Media page once more after a long idle time. (This
description of Home Assistant's behaviour is taken from observed symptoms and not verified against its source; the add-on code itself
is checked: it has no `401` for ingress pages.)

## Writing a source plugin

Add a new website without touching the add-on: drop a Python file into the add-on's config
folder and describe the source in a JSON file. The add-on maps `addon_config` to `/config`
(on the host: `/addon_configs/<slug>/`, the folder whose name ends in `car_media_hub`, also
reachable with the File editor or Samba add-ons).

- `/config/plugins/*.py` - every file is imported at start-up; `@register_source("name")` classes
  become available as source types. A broken plugin is logged and skipped.
- `/config/sources.json` - list of sources to run in addition to the built-in options
  (`inbox_enabled`, `rss_*`). YAML (`sources.yaml`) is only read if PyYAML is installed,
  which the add-on image does not ship, so use JSON.

Subclass `RemoteSource` and implement two methods; polling, change detection, size limit,
downloading with checksum verification, retries and failure bookkeeping come for free:

```python
from car_media_hub.sources import RemoteFile, RemoteRelease, RemoteSource, register_source

@register_source("my_site")
class MySite(RemoteSource):
    default_api_base = "https://music.example.com"

    async def list_releases(self) -> list[RemoteRelease]:
        data = await self.http.get_json(f"{self.api_base}/api/albums", "albums")
        return [RemoteRelease(str(a["id"]), a["title"], {"url": a["page"]}) for a in data]

    async def list_files(self, release: RemoteRelease) -> list[RemoteFile]:
        data = await self.http.get_json(f"{self.api_base}/api/albums/{release.id}", "files")
        return [RemoteFile(f["path"], f["url"], f.get("bytes"), f.get("sha256"), "sha256")
                for f in data["files"]]
```

- `RemoteRelease(id, title, meta)`: `meta` is copied to the release report; `url`, `license` and
  `creator` are shown in the review UI. `list_files` may update `release.title`/`release.meta`.
- `RemoteFile(name, url, size, checksum, checksum_algo)`: `name` is a relative path (unsafe names
  are rejected); `size` and the checksum (`sha1`, `md5` or `sha256`) are optional but recommended.
- Override `select_file(f)` to change which files are downloaded (default: audio files and archives).
- Settings: every key of a spec except `type` is available as `self.settings`; the base class reads
  `name` (letters, digits, `_ . -`; needed when a type is used twice), `poll_minutes` (default 60),
  `check_time` (`HH:MM`, daily check instead of the interval), `run_on_start` (default true),
  `max_items_per_scan` (default 3) and `auto_approve` (default false). Validate your own settings in
  `__init__` and raise `ValueError` - the source is then skipped with an error in the log.
- Use `self.http.get_json(...)` for API calls (User-Agent, retries) and raise nothing special on
  failures; one failing release never stops the others. Free-space checks, the Stop button,
  download progress and the status bar entry also come from the base class. Finalising, state keys
  (`<name>:sig|fail|failsig|failat:<id>`, `<name>:lastpoll`) and download folders (`_downloads/<name>/<id>`) are handled by the base class.
- Need something different from "download files"? Subclass `Source` directly (`poll` and
  `finalize`) and register it the same way.

Example `/config/sources.json`:

```json
[
  {"type": "my_site", "poll_minutes": 120, "max_items_per_scan": 2},
  {"type": "rss", "name": "rss-extra", "feed_url": "https://example.org/feed.xml"},
  {"type": "inbox", "dir": "/share/more_music"}
]
```

Every source needs a unique name. A second `rss` source therefore needs its own `name` (the built-in one
is called `rss`); it keeps its own poll schedule, item state and download folder. An `inbox` entry takes an absolute `dir` and an optional `name` (default `inbox`
for `inbox_dir`, else `inbox-<folder name>`). Inbox folders must not overlap each other,
`staging_dir` or `library_dir`; an entry that does is skipped with an error in the Log.

**Security:** plugins are ordinary Python code that runs inside the add-on with its permissions
(Home Assistant API token, `/share`, `/media`, network). Only install plugins you have read and trust.

## Folders

The add-on maps `/share` and `/media` read-write. Keep `inbox_dir`, `staging_dir` and `library_dir`
under those paths, as three separate folders: the inbox deletes what it imports, so it must not be
`/share` or `/media` itself and must not contain or lie inside the library or staging folder; staging
must not be the library and may lie inside it only under a hidden (dot) folder. The add-on refuses to
start with such a layout and says why in the Log. A staging folder inside the library only works
while the library exists: with the sync API on, nothing is downloaded or imported while it is
missing (see *Missing library*), so prefer a sibling folder such as the default `/media/.car_media_hub/staging`.

`/share` and `/media` are separate mounts (of the same data disk), and a file cannot be renamed
from one to the other. The default `staging_dir`, `/media/.car_media_hub/staging`, is therefore on
the library's mount but outside `library_dir` (a dot folder: hidden in the media browser and never
served by the sync API), so an approval is an instant rename that needs no extra space. With staging
in `/share` (the default of version 0.1) and the library in `/media`, every approval **copies** the
release and briefly needs its size free a second time, until the staging copy is removed. The
default is an instant rename only while `library_dir` is on the local `/media` storage, as the
default `/media/music` is: a library under `/share` or on network storage mounted in `/media` is
another mount, so put `staging_dir` next to it on the same storage, or accept the copy. After an
update from 0.1, Home Assistant keeps only the options you saved: if you saved the Configuration tab
with 0.1, `staging_dir` stays `/share/car_media_hub/staging` until you change it; if you never saved
it, the new default applies at once. Releases that are pending in the old folder at that moment are
still approved from there (by a copy); once none is left, the old folder can be deleted. The inbox
can stay in `/share`.

With `delete_source` on, inbox releases need no net space for an approval, because the inbox
original is removed once the release is pending. Downloaded releases (RSS, plugins),
`delete_source` off, and a disk that fills up while a release is pending need the extra room.

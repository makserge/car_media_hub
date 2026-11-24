# Car Media and Trips

Home Assistant add-on that watches an inbox folder and RSS feeds for new music
releases, extracts and cleans them, and asks you in Home Assistant to **Approve** or **Decline**.
Approved releases are moved into your music library; declined ones are deleted. The library can be
served to the CarNavigationHelper car app (and from it to cyp) over the local network.

- Safe extraction of zip, tar, rar (bsdtar) and 7z archives
- Removes tracks with Cyrillic names or tags, MP3s when FLAC is present, and tracks already in your library
- Makes file names safe for phones and FAT/exFAT cards; hidden files and `__MACOSX` are removed
- Approve/Decline from the Ingress panel, a persistent notification, or a mobile actionable notification;
  optional auto-approve per source
- Optional (option `browser_enabled`, **off by default**): feed items open in a Chromium that runs inside the add-on, as an unprivileged user behind a filter for private networks and Home Assistant (Chromium is Debian's package, fixed at image build time (rebuild/update regularly), and the browser refuses to start when it is too old; a compromised browser is NOT contained by the filter: the browser does not start unless Chromium's sandbox works, or you set `browser_allow_unsandboxed`); downloads need your click and are never auto-approved. Read *Security of the browser* in the Documentation before enabling it
- When enabled, feed items open in a Chromium that runs inside the add-on (shown live on the Media page); its downloads go to the inbox and are picked up, cleaned and reviewed
  - A click loads the page plus up to 3 redirects (`ALLOWED_REDIRECTS` in `browser.py`); the next redirect is refused before it loads and the viewer says so
  - The picture always matches the page, so the mouse cursor and clicks land on the link you see: moving frames that are shifted or of another size are replaced by screenshots, and a still picture of the wrong size is not shown (the log says when)
  - Every click opens the page of the item you clicked, and the viewer's "Media" link steps back in the history, so Home Assistant's Back button returns to the Home Assistant page before it
  - An item is marked **done** when its download is complete (opening it does not mark it)
- RSS/Atom source: downloads enclosures (for sites where the file is behind a page, use the add-on browser)
- Sync API on port 8100 for the car app, and cyp if you want (token protected)
- Free-space guard, Stop button and live status in the web UI. The **Media** page (the first button at the top, next to **Trips**) has two columns; on a narrow screen they stack with the RSS feed first:
  - **Right:** the RSS feed, a plain list of all items (finished ones marked done)
  - **Left**, every block a panel with list rows in the same style: the status with a Sources table (the inbox row shows the last and next check and what it sees: waiting, still downloading, ignored, last import), browser downloads, **Pending review** (each release is a row with its details, **Approve** and **Decline**), **Recently decided**, and the **Library**
  - The **Library** is a list of clickable items: click a folder to open or close the list of its files (sub-folders open the same way, and stay open when the list is refreshed); the top level also has **Delete**. It is listed again when a release is approved or something is deleted
- The inbox takes archives (`.zip .rar .7z .tar` ...) and folders; an entry is imported once it is unchanged for two scans in a row and for `settle_seconds` (so about 1-2 minutes after the file is complete). It logs what it finds and why it ignores a file
- Trip API on the same port: the car app uploads trips and energy consumption; Home Assistant shows them as `sensor.car_media_hub_*` entities (see docs/TRIPS_API.md)
- Offline map regions for the car app: the add-on hosts `map.pmtiles`, `routing.tar` and `places.sqlite` per region under `/share/car_media_hub/regions`, lists them at `/api/regions/catalogue` (SHA-256 computed in the background) and serves them with resumable range requests; the **Regions** page creates, uploads, imports (https URL) and edits regions and has **Install on car app** / **Remove from car app** buttons (the app picks the wish up from the catalogue and reports its progress back). Prepare packs on a PC and copy them to the share (see *Offline map regions* in the Documentation)


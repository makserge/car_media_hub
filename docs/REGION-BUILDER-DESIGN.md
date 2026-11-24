# Region builder inside the add-on (phase 2, design only)

Status: **not implemented.** Phase 1 (this add-on version) hosts and syncs regions that were prepared elsewhere (see
"Offline map regions" in the Documentation). This note describes an optional in-add-on builder, what it would cost on a
small Home Assistant host and why it is not on by default.

## Goal

From a region name or bounding box, produce the three files of a region folder without a PC:

| Stage | Tool | Output |
|---|---|---|
| 1. Source | Geofabrik extract download (`<region>-latest.osm.pbf`, https, `.md5` check), or a country PBF plus `osmium extract -b` / `-p` for a custom box | `source.osm.pbf` |
| 2. Map | planetiler (Java, OpenMapTiles profile, zoom 0-14) | `map.pmtiles` |
| 3. Routing | `valhalla_build_config`, `valhalla_build_tiles`, `valhalla_build_extract` (pyvalhalla) | `routing.tar` |
| 4. Places | `tools/build_address_db.py` (pyosmium) | `places.sqlite` |
| 5. Publish | write files into a temporary folder, `manifest.json`, hash, atomic rename into `<regionId>/` | region folder |

## Shape of the implementation

- A separate, opt-in option `region_builder_enabled` (default off) and a second container image layer (or a separate add-on)
  carrying a JRE, planetiler, osmium-tool, pyvalhalla and pyosmium. The main add-on image stays small; it must not grow by about
  600 MB for a feature few people use.
- One build at a time, a work folder below `region_storage_dir/.build/<id>/` (hidden, ignored by the scanner), a job with stages,
  progress, a log and **Cancel**, resumable per stage (a finished stage is not repeated if its output exists and the inputs did not
  change).
- Runs as an unprivileged user with `nice 19`/`ionice idle`, a hard memory limit (`RLIMIT_AS` or the container's cgroup),
  a wall-clock limit per stage and a free-space check before every stage (as the downloads do).
- Downloads use the same guard as "Import from URL": https only, the host is limited to `download.geofabrik.de`, size cap.
- Result and attribution go into `manifest.json` (`source`: Geofabrik file and date, `license`: ODbL + the OpenMapTiles
  and Natural Earth notices).

## Estimates for a Raspberry Pi 4/5 class host (4-8 GB RAM, SD or USB SSD)

Figures are order-of-magnitude, taken from the packs built for this project on a PC (Oberpfalz, about 86 MB PBF: map 66 MB,
routing 108 MB, places 49 MB; the routing build took about 8 minutes on 4 threads of a PC) and scaled by size and CPU speed.
They have **not** been measured on a Raspberry.

| Region (PBF) | RAM peak | Temporary disk | Time on a Pi 5 (4 cores) | Time on a Pi 4 | Result |
|---|---|---|---|---|---|
| Regierungsbezirk, 80-150 MB | planetiler 2-3 GB; valhalla 1-2 GB; pyosmium 0.5 GB | 3-6 GB (planetiler node cache and Natural Earth/water polygon sources, ~1.5 GB once) | 1-2 h | 2-4 h | ~250 MB |
| Bundesland (Bayern, ~700 MB) | planetiler 6-8 GB (off-heap node map; needs `--storage=mmap`, then slow); valhalla 4+ GB | 15-25 GB | 6-12 h | does not fit in practice | ~1.5 GB |
| Germany (~4 GB) | 16+ GB | 80+ GB | days | no | ~7-8 GB |

Home Assistant OS on 2-4 GB devices (Pi 3, small x86 boxes) cannot do this at all without swapping to death.

## Why it is not enabled by default

- A build keeps all cores busy for hours and can use more RAM than the host has free. Home Assistant core, the database and other
  add-ons run on the same machine; an out-of-memory kill or I/O starvation takes the house automation down, not just this add-on.
- Writing tens of GB of temporary data wears SD cards.
- The toolchain (JRE, planetiler, Valhalla, pyosmium) is large and a security surface of its own; it processes downloaded data.
- The packs are built rarely (OSM data is only worth refreshing every few months), so the convenience does not justify the load.

## Alternative (recommended): build on the PC

Run `tools/build_region_pack.py` in the CarNavigationHelper project on a PC (it takes minutes to a few hours and any amount of RAM)
and copy the finished folder to `/share/car_media_hub/regions/<id>/`; or upload / import the files on the Regions page. This is what
phase 1 supports. A builder could still be added later as a separate, clearly marked add-on for users with a strong host (x86 mini PC
with 16 GB RAM or more), reusing the manifest and publishing code of phase 1.

## Open questions for a later implementation

- Valhalla tile extract for several neighbouring regions in one archive (`routingGroup`): build from a merged PBF.
- Which OpenMapTiles profile version matches the app's map style (the app is the reference).
- Whether to ship planetiler's source data pre-downloaded in the builder image.

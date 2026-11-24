"""Add-on configuration loading and validation."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aac", ".ape", ".wv", ".aiff", ".aif"}
ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")

_DUPLICATE_ACTIONS = ("delete", "keep")
_LOG_LEVELS = ("debug", "info", "warning", "error")
_PENDING_TIMEOUT_ACTIONS = ("decline", "approve")
_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class Config:
    """Immutable add-on configuration."""

    inbox_dir: Path
    staging_dir: Path
    library_dir: Path
    scan_interval: int = 60
    settle_seconds: int = 30
    duplicate_action: str = "delete"  # "delete" | "keep"
    delete_source: bool = True
    dedupe_tracks: bool = True
    strip_all_tags: bool = False
    check_directory_names: bool = False
    notify_service: str = ""  # e.g. "mobile_app_pixel_8" (without "notify.")
    pending_timeout_hours: int = 0
    log_level: str = "info"
    inbox_enabled: bool = True
    max_release_size_mb: int = 5000
    data_dir: Path = Path("/data")  # sqlite lives at data_dir / "car_media_hub.db"
    plugins_dir: Path = Path("/config/plugins")  # *.py source plugins (trusted code)
    sources_file: Path = Path("/config/sources.json")  # extra source specs (JSON; YAML if PyYAML is installed)
    min_free_space_gb: float = 50.0  # downloads, staging and extraction keep this much free
    pending_timeout_action: str = "decline"  # "decline" | "approve" (after pending_timeout_hours)
    sync_enabled: bool = False
    sync_token: str = ""  # stripped; validated by sync.py, never by load_config
    sync_dscp: bool = True  # mark the sync API's file and probe traffic DSCP CS1 (background class on Wi-Fi)
    rss_feed_url: str = ""  # empty = no builtin RSS source
    rss_check_time: str = "03:00"  # "" = use rss_poll_minutes; else HH:MM
    rss_poll_minutes: int = 1440
    rss_run_on_start: bool = True
    rss_max_items_per_scan: int = 10
    rss_auto_approve: bool = False
    browser_enabled: bool = False  # the embedded browser (Chromium, Xvfb, filtering proxy): nothing of it starts unless on
    browser_allow_unsandboxed: bool = False  # start Chromium also when its sandbox cannot work here (a compromised page is then NOT contained)
    region_storage_dir: Path = Path("/share/car_media_hub/regions")  # offline map regions served to the car app
    region_max_file_mb: int = 8192  # largest single region file (upload or import from URL)
    region_import_max_hours: float = 6.0  # overall time limit of one import from a URL (0 = no limit)
    region_transfer_gzip: bool = True  # keep gzip copies of routing/places for a compressed download by the car app
    charger_refresh_days: int = 7  # the add-on rebuilds the charger database this often (0 = never, only the button)
    charger_source_url: str = ""  # direct address of the register CSV; "" = find it on the Bundesnetzagentur page

    @property
    def browser_profile_dir(self) -> Path:
        """Chromium's profile (owned by the unprivileged browser user)."""
        return self.data_dir / "browser-profile"

    @property
    def browser_incoming_root(self) -> Path:
        """Root-owned parent (mode 0711) of the download folder: outside the browser's profile, so the browser user
        can neither replace the folder nor plant links next to it."""
        return self.data_dir / "browser-incoming"

    @property
    def browser_incoming_dir(self) -> Path:
        """Where Chromium saves downloads (the only folder the browser user owns there, never watched)."""
        return self.browser_incoming_root / "dl"

    @property
    def browser_ready_dir(self) -> Path:
        """Finished, checked browser downloads (root-owned): the inbox of the "browser" source, never auto-approved."""
        return self.data_dir / "browser-downloads"


def _int(raw: dict[str, Any], key: str, default: int, lo: int, hi: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer, got {value!r}")
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be between {lo} and {hi}, got {value}")
    return value


def _number(raw: dict[str, Any], key: str, default: float, lo: float, hi: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        raise ValueError(f"{key} must be a number, got {value!r}")
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}, got {value}")
    return float(value)


def _bool(raw: dict[str, Any], key: str, default: bool) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be true or false, got {value!r}")
    return value


def _str(raw: dict[str, Any], key: str, default: str) -> str:
    value = raw.get(key, default)
    if value is None:
        return default
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string, got {value!r}")
    return value.strip()


def _choice(raw: dict[str, Any], key: str, default: str, choices: tuple[str, ...]) -> str:
    value = _str(raw, key, default).lower()
    if value not in choices:
        raise ValueError(f"{key} must be one of {', '.join(choices)}, got {value!r}")
    return value


def _path(raw: dict[str, Any], key: str, default: str) -> Path:
    value = _str(raw, key, default)
    if not value:
        raise ValueError(f"{key} must not be empty")
    return Path(value)


INTERVAL_SENTINEL = "interval"  # for the HA form, which cannot save an empty field: same as ""


def parse_hhmm(value: str, what: str = "time") -> tuple[int, int] | None:
    """Parse ``HH:MM`` (00:00-23:59) into ``(hour, minute)``.

    An empty string (or the word ``interval``) gives None: no fixed time, use the poll interval.
    """
    text = value.strip() if isinstance(value, str) else value
    if text == "" or (isinstance(text, str) and text.lower() == INTERVAL_SENTINEL):
        return None
    match = _HHMM_RE.match(text) if isinstance(text, str) else None
    if match is None:
        raise ValueError(f"{what} must be empty or HH:MM between 00:00 and 23:59, got {value!r}")
    return int(match.group(1)), int(match.group(2))


def _http_url(raw: dict[str, Any], key: str) -> str:
    value = _str(raw, key, "")
    if value:
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ValueError(f"{key} must be empty or an http(s) URL, got {value!r}")
    return value


def _check_time(raw: dict[str, Any]) -> str:
    value = _str(raw, "rss_check_time", "03:00")
    if parse_hhmm(value, "rss_check_time") is None:
        return ""  # "" or "interval": poll every rss_poll_minutes
    return value


# ------------------------------------------------------------------ folder layout
# Folders that can never be the inbox: it deletes everything it imports, and every other
# non-hidden folder in it is offered as a release.
_MOUNT_ROOTS = frozenset(
    {"/", "/share", "/media", "/config", "/data", "/backup", "/ssl", "/addons", "/addon_configs"}
)


def _same_folder(a: str, b: str) -> bool:
    """True when two resolved paths are the same folder (also on a case-insensitive file system)."""
    if os.path.normcase(a) == os.path.normcase(b):
        return True
    try:
        return os.path.exists(a) and os.path.exists(b) and os.path.samefile(a, b)
    except OSError:
        return False


def _with_parents(path: str) -> list[str]:
    chain = [path]
    while True:
        parent = os.path.dirname(chain[-1])
        if not parent or parent == chain[-1]:
            return chain
        chain.append(parent)


def path_relation(a: Path | str, b: Path | str) -> str | None:
    """How folder ``a`` relates to folder ``b``: ``"same"``, ``"inside"`` (a is below b),
    ``"contains"`` (b is below a) or None.

    Symlinks are resolved; ``os.path.samefile`` on the existing ancestors also catches bind
    mounts and case-insensitive network shares. Linux paths stay case-sensitive.
    """
    ra, rb = os.path.realpath(a), os.path.realpath(b)
    if _same_folder(ra, rb):
        return "same"
    if any(_same_folder(parent, rb) for parent in _with_parents(ra)[1:]):
        return "inside"
    if any(_same_folder(parent, ra) for parent in _with_parents(rb)[1:]):
        return "contains"
    return None


def _is_hidden_below(path: Path, base: Path) -> bool:
    """True when ``path`` (below ``base``) has a dot-named segment, which the sync API never lists."""
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(base))
    except ValueError:
        return False
    return any(part.startswith(".") for part in rel.replace("\\", "/").split("/") if part not in ("", ".", ".."))


def _posix_text(path: Path | str) -> str:
    return str(path).replace("\\", "/").rstrip("/") or "/"


def check_inbox_dir(inbox: Path, staging: Path, library: Path, what: str = "inbox_dir") -> None:
    """Raise ``ValueError`` unless ``inbox`` is a dedicated folder apart from staging and library.

    The inbox imports every non-hidden folder or archive in it and then deletes (or moves) it, so
    it must not be a mount root, nor equal, contain or lie inside the library or the staging folder
    (an inbox inside staging is harmless: staging code only touches ``<12 hex>`` and ``_downloads``).
    """
    if _posix_text(inbox) in _MOUNT_ROOTS or _posix_text(os.path.realpath(inbox)) in _MOUNT_ROOTS:
        raise ValueError(f"{what} must be a dedicated folder, not {inbox}: everything in it is deleted after import")
    for other_name, other in (("library_dir", library), ("staging_dir", staging)):
        rel = path_relation(inbox, other)
        if rel == "same":
            raise ValueError(f"{what} must not be the same folder as {other_name} ({inbox})")
        if rel == "contains":
            raise ValueError(
                f"{what} ({inbox}) must not contain {other_name} ({other}): it would be imported and deleted"
            )
    if path_relation(inbox, library) == "inside":
        raise ValueError(f"{what} ({inbox}) must not be inside library_dir ({library})")


# Folders a region folder may lie below. ``/addon_config`` is not a mount inside the container (a path there lands in
# the container layer and is lost on update). ``/data`` is part of every add-on backup, so only ``/data/regions`` is
# allowed: config.yaml excludes ``regions/**`` from backups. ``/share`` and ``/media`` are in full backups (documented).
_REGION_PARENTS = ("/share", "/media", "/config", "/data/regions")


def check_region_dir(region: Path, inbox: Path, staging: Path, library: Path, check_inbox: bool = True) -> None:
    """Raise ``ValueError`` unless ``region`` is a dedicated folder for the offline map regions.

    It must be an absolute path below a folder the add-on maps (``/share``, ``/media``, ...), not a mount
    root, free of ``..`` and never the same as, containing or inside the library, staging or inbox folder
    (the inbox deletes what it imports, the library is listed to the music clients).
    """
    text = _posix_text(region)
    if not text.startswith("/") or ".." in text.split("/"):
        raise ValueError(f"region_storage_dir must be an absolute path without '..', got {region}")
    if text in _MOUNT_ROOTS or _posix_text(os.path.realpath(region)) in _MOUNT_ROOTS:
        raise ValueError(f"region_storage_dir must be a dedicated folder, not {region}")
    if not any(text == base or text.startswith(base + "/") for base in _REGION_PARENTS):
        raise ValueError(
            f"region_storage_dir must lie below one of {', '.join(_REGION_PARENTS)} (folders mapped into the add-on), got {region}"
        )
    others = [("library_dir", library), ("staging_dir", staging)]
    if check_inbox:
        others.append(("inbox_dir", inbox))
    for other_name, other in others:
        if path_relation(region, other) is not None:
            raise ValueError(
                f"region_storage_dir ({region}) must not be, contain or lie inside {other_name} ({other})"
            )


def check_directory_layout(inbox: Path, staging: Path, library: Path, check_inbox: bool = True) -> None:
    """Validate how the folders relate (``ValueError`` for a layout that can lose or expose files).

    Staging and library must be separate folders; staging may sit inside the library only below a
    hidden (dot-named) folder, which the sync API never lists. A library inside staging is harmless.
    """
    rel = path_relation(staging, library)
    if rel == "same":
        raise ValueError(f"staging_dir and library_dir must be different folders ({staging})")
    if rel == "inside" and not _is_hidden_below(staging, library):
        raise ValueError(
            f"staging_dir ({staging}) must not be inside library_dir ({library}): pending releases would be "
            "listed by the sync API (use a folder outside library_dir, such as /media/.car_media_hub/staging; "
            "a hidden folder inside it only works while library_dir exists)"
        )
    if check_inbox:
        check_inbox_dir(inbox, staging, library)


# db state keys of the "empty library is intended" rule (the same strings as pipeline.STATE_LAST_DELETE /
# STATE_LAST_NONEMPTY and sync.STATE_LAST_*)
_STATE_LAST_DELETE = "sync:last_delete_at"
_STATE_LAST_NONEMPTY = "sync:last_nonempty_at"


def library_confirmed(get_state: Callable[[str], str | None]) -> bool:
    """True when the user lifted the missing-library guard ("Confirm": a recorded deletion that came
    after the last time the library had files). Same rule as the controller's approval check."""
    try:
        deleted = get_state(_STATE_LAST_DELETE)
        if deleted is None:
            return False
        return float(deleted) >= float(get_state(_STATE_LAST_NONEMPTY) or 0.0)
    except (TypeError, ValueError):
        return False


def library_blocks(cfg: Config, path: Path | str, get_state: Callable[[str], str | None]) -> bool:
    """True while path must not be created because that would also create a missing library_dir.

    That is the case when the sync API is on, path is library_dir or lies inside it (a hidden
    staging or inbox folder below the library), the library does not exist and the user has not
    confirmed it. Creating it would turn the 503 of a missing or unmounted library into an empty
    folder that is served as 200 [], and the car app mirrors that list.
    """
    if not cfg.sync_enabled:
        return False
    try:
        if os.path.isdir(cfg.library_dir) or path_relation(path, cfg.library_dir) not in ("same", "inside"):
            return False
    except OSError:
        return False
    return not library_confirmed(get_state)


def load_config(path: Path = Path("/data/options.json"), data_dir: Path | None = None) -> Config:
    """Load and validate options from ``path`` (the Supervisor ``options.json``).

    Raises ``ValueError`` with a clear message on invalid values.
    """
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"options file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"options file {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"options file {path} must contain a JSON object")

    notify = _str(raw, "notify_service", "")
    if notify.startswith("notify."):
        notify = notify[len("notify."):]

    kwargs: dict[str, Any] = dict(
        inbox_dir=_path(raw, "inbox_dir", "/share/car_media_hub/inbox"),
        # On the library's mount (approval is a rename, not a copy), outside library_dir, hidden (dot folder)
        staging_dir=_path(raw, "staging_dir", "/media/.car_media_hub/staging"),
        library_dir=_path(raw, "library_dir", "/media/music"),
        scan_interval=_int(raw, "scan_interval", 60, 10, 86400),
        settle_seconds=_int(raw, "settle_seconds", 30, 0, 3600),
        duplicate_action=_choice(raw, "duplicate_action", "delete", _DUPLICATE_ACTIONS),
        delete_source=_bool(raw, "delete_source", True),
        dedupe_tracks=_bool(raw, "dedupe_tracks", True),
        strip_all_tags=_bool(raw, "strip_all_tags", False),
        check_directory_names=_bool(raw, "check_directory_names", False),
        notify_service=notify,
        pending_timeout_hours=_int(raw, "pending_timeout_hours", 0, 0, 8760),
        log_level=_choice(raw, "log_level", "info", _LOG_LEVELS),
        inbox_enabled=_bool(raw, "inbox_enabled", True),
        max_release_size_mb=_int(raw, "max_release_size_mb", 5000, 1, 1_000_000),
        plugins_dir=_path(raw, "plugins_dir", "/config/plugins"),
        sources_file=_path(raw, "sources_file", "/config/sources.json"),
        min_free_space_gb=_number(raw, "min_free_space_gb", 50.0, 0, 100000),
        pending_timeout_action=_choice(raw, "pending_timeout_action", "decline", _PENDING_TIMEOUT_ACTIONS),
        sync_enabled=_bool(raw, "sync_enabled", False),
        sync_token=_str(raw, "sync_token", ""),
        sync_dscp=_bool(raw, "sync_dscp", True),
        rss_feed_url=_http_url(raw, "rss_feed_url"),
        rss_check_time=_check_time(raw),
        rss_poll_minutes=_int(raw, "rss_poll_minutes", 1440, 5, 10080),
        rss_run_on_start=_bool(raw, "rss_run_on_start", True),
        rss_max_items_per_scan=_int(raw, "rss_max_items_per_scan", 10, 1, 1000),
        rss_auto_approve=_bool(raw, "rss_auto_approve", False),
        browser_enabled=_bool(raw, "browser_enabled", False),
        browser_allow_unsandboxed=_bool(raw, "browser_allow_unsandboxed", False),
        region_storage_dir=_path(raw, "region_storage_dir", "/share/car_media_hub/regions"),
        region_max_file_mb=_int(raw, "region_max_file_mb", 8192, 1, 200_000),
        region_import_max_hours=_number(raw, "region_import_max_hours", 6.0, 0, 720),
        region_transfer_gzip=_bool(raw, "region_transfer_gzip", True),
        charger_refresh_days=_int(raw, "charger_refresh_days", 7, 0, 365),
        charger_source_url=_http_url(raw, "charger_source_url"),
    )
    if data_dir is not None:
        kwargs["data_dir"] = Path(data_dir)
    check_directory_layout(
        kwargs["inbox_dir"], kwargs["staging_dir"], kwargs["library_dir"], check_inbox=kwargs["inbox_enabled"]
    )
    check_region_dir(
        kwargs["region_storage_dir"], kwargs["inbox_dir"], kwargs["staging_dir"], kwargs["library_dir"],
        check_inbox=kwargs["inbox_enabled"],
    )
    return Config(**kwargs)

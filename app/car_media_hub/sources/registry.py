"""Source plugin registry: registration, lookup, construction from specs and plugin loading."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, TypeVar

from ..config import path_relation
from .base import Source

if TYPE_CHECKING:
    import aiohttp

    from ..config import Config
    from ..db import Database

log = logging.getLogger(__name__)

_REGISTRY: dict[str, type[Source]] = {}
S = TypeVar("S", bound=type[Source])


def register_source(type_name: str) -> Callable[[S], S]:
    """Class decorator registering a Source subclass under ``type_name``."""
    if not isinstance(type_name, str) or not type_name.strip():
        raise ValueError("source type name must be a non-empty string")

    def decorator(cls: S) -> S:
        existing = _REGISTRY.get(type_name)
        if existing is not None and (existing.__module__, existing.__qualname__) != (
            cls.__module__,
            cls.__qualname__,
        ):
            raise ValueError(f"source type {type_name!r} is already registered by {existing.__module__}")
        cls.type_name = type_name  # type: ignore[attr-defined]
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def unregister_source(type_name: str) -> None:
    _REGISTRY.pop(type_name, None)


def get_source_class(type_name: str) -> type[Source]:
    try:
        return _REGISTRY[type_name]
    except KeyError:
        raise KeyError(f"unknown source type {type_name!r}; available: {', '.join(available_sources())}") from None


def available_sources() -> list[str]:
    return sorted(_REGISTRY)


def _check_watched_folder(source: Source, built: list[Source]) -> None:
    """Reject a folder-watching source (``inbox_dir`` attribute) whose folder overlaps another one's:
    a folder inside another inbox would be imported as part of a release and then deleted."""
    folder = getattr(source, "inbox_dir", None)
    if folder is None:
        return
    for other in built:
        other_folder = getattr(other, "inbox_dir", None)
        if other_folder is not None and path_relation(folder, other_folder) is not None:
            raise ValueError(
                f"the inbox folder {folder} is, contains or lies inside the folder {other_folder} of source "
                f"{other.name!r}; inbox folders must be separate"
            )


def build_sources(
    cfg: Config, db: Database, session: aiohttp.ClientSession, specs: list[dict[str, Any]]
) -> list[Source]:
    """Instantiate sources from specs; invalid specs are logged and skipped, never raised."""
    built: list[Source] = []
    for index, spec in enumerate(specs, start=1):
        label = spec.get("type") if isinstance(spec, dict) else spec
        try:
            if not isinstance(spec, dict):
                raise ValueError(f"spec must be a mapping, got {type(spec).__name__}")
            type_name = spec.get("type")
            if not isinstance(type_name, str):
                raise ValueError("spec has no 'type'")
            cls = get_source_class(type_name)
            settings = {k: v for k, v in spec.items() if k != "type"}
            source = cls.create(cfg, db, session, settings)
            if any(other.name == source.name for other in built):
                raise ValueError(f"duplicate source name {source.name!r}; give the source a unique 'name'")
            _check_watched_folder(source, built)
            built.append(source)
        except Exception as exc:  # noqa: BLE001 - one bad spec must not stop the add-on
            msg = exc.args[0] if isinstance(exc, KeyError) and exc.args else exc
            log.error("Skipping source #%d (%s): %s", index, label, msg)
    return built


def load_plugins(directory: Path) -> list[str]:
    """Import every ``*.py`` in ``directory`` so their ``@register_source`` decorators run.

    Returns the loaded module names. Errors are logged, never raised.
    """
    directory = Path(directory)
    loaded: list[str] = []
    if not directory.is_dir():
        log.debug("No plugin directory at %s", directory)
        return loaded
    for path in sorted(directory.glob("*.py")):
        digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:8]
        module_name = f"car_media_hub_plugin_{path.stem}_{digest}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError("cannot create import spec")
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module  # needed by dataclasses & friends
            try:
                spec.loader.exec_module(module)
            except BaseException:
                sys.modules.pop(module_name, None)
                raise
            loaded.append(module_name)
            log.info("Loaded source plugin %s", path.name)
        except Exception as exc:  # noqa: BLE001
            log.error("Failed to load plugin %s: %s", path, exc, exc_info=True)
        except SystemExit:
            log.error("Plugin %s called sys.exit()", path)
    return loaded


def builtin_specs(cfg: Config) -> list[dict[str, Any]]:
    """Translate the classic add-on options into source specs."""
    specs: list[dict[str, Any]] = []
    if cfg.inbox_enabled:
        specs.append({"type": "inbox"})
    if cfg.browser_enabled:
        # files saved by the embedded browser: their own inbox, and a human always decides (manual_only)
        specs.append({"type": "inbox", "name": "browser", "dir": str(cfg.browser_ready_dir), "manual_only": True})
    if cfg.rss_feed_url:
        specs.append(
            {
                "type": "rss",
                "name": "rss",
                "feed_url": cfg.rss_feed_url,
                "check_time": cfg.rss_check_time,
                "poll_minutes": cfg.rss_poll_minutes,
                "run_on_start": cfg.rss_run_on_start,
                "max_items_per_scan": cfg.rss_max_items_per_scan,
                "auto_approve": cfg.rss_auto_approve,
            }
        )
    return specs


def load_sources_file(path: Path) -> list[Any]:
    """Read extra source specs (a list of mappings) from a JSON file, or YAML if PyYAML exists.

    A missing file means no extra sources; an unreadable one is logged and ignored.
    """
    path = Path(path)
    if not path.is_file():
        return []
    try:
        text = path.read_text(encoding="utf-8")
        if path.suffix.lower() in (".yaml", ".yml"):
            import yaml  # optional dependency

            data = yaml.safe_load(text)
        else:
            data = json.loads(text)
        if data is None:
            return []
        if isinstance(data, dict):
            data = data.get("sources")
        if not isinstance(data, list):
            raise ValueError("expected a list of source specs (or {'sources': [...]})")
        return list(data)
    except Exception as exc:  # noqa: BLE001
        log.error("Could not read sources file %s: %s", path, exc)
        return []

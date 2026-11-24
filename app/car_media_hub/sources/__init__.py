"""Release sources and the plugin system that lets new websites be added with little code."""
from __future__ import annotations

from ..diskspace import NoSpaceError
from .base import Candidate, Outcome, Source
from .fetch import (
    API_TIMEOUT,
    DOWNLOAD_TIMEOUT,
    MAX_FAILURES,
    RETRY_AFTER,
    USER_AGENT,
    DownloadCancelled,
    HttpClient,
    PermanentError,
    SeenStore,
    check_url,
    download_file,
    normalise_relative_name,
)
from .registry import (
    available_sources,
    build_sources,
    builtin_specs,
    get_source_class,
    load_plugins,
    load_sources_file,
    register_source,
)
from .remote import RemoteFile, RemoteRelease, RemoteSource

# Importing the built-ins registers them.
from .inbox import PROCESSED_DIR, InboxSource  # isort: skip
from .rss import RssSource  # isort: skip

__all__ = [
    "API_TIMEOUT",
    "DOWNLOAD_TIMEOUT",
    "MAX_FAILURES",
    "PROCESSED_DIR",
    "RETRY_AFTER",
    "USER_AGENT",
    "Candidate",
    "DownloadCancelled",
    "HttpClient",
    "InboxSource",
    "NoSpaceError",
    "Outcome",
    "PermanentError",
    "RemoteFile",
    "RemoteRelease",
    "RemoteSource",
    "RssSource",
    "SeenStore",
    "Source",
    "available_sources",
    "build_sources",
    "builtin_specs",
    "check_url",
    "download_file",
    "get_source_class",
    "load_plugins",
    "load_sources_file",
    "normalise_relative_name",
    "register_source",
]

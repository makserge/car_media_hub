"""Core source contract: candidates, outcomes and the abstract Source."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, ClassVar, Literal

if TYPE_CHECKING:
    import aiohttp

    from ..config import Config
    from ..db import Database

# "deferred": not processed (no space / stopped); retry later, no failure counted.
Outcome = Literal["done", "duplicate", "failed", "empty", "deferred"]


@dataclass(frozen=True)
class Candidate:
    """A release ready to be processed by the pipeline."""

    path: Path
    name: str
    is_dir: bool
    key: str = ""  # source-specific id, e.g. "rss:<item id>"
    meta: dict = field(default_factory=dict, compare=False, hash=False)
    disposable: bool = False  # the pipeline may move ``path`` instead of copying it


class Source(ABC):
    """A producer of release candidates."""

    name: str
    type_name: ClassVar[str] = ""  # set by @register_source
    auto_approve: bool = False  # approve releases without asking
    manual_only: bool = False  # releases are never approved without a person (no auto_approve, no pending timeout approval)
    notify_empty: bool = True  # post a "nothing new" notification for empty releases
    # True when a deferred candidate is offered again at every poll until it is gone (the inbox):
    # the controller then forgets its free-space need as soon as it is no longer offered
    offers_deferred_again: bool = False

    @classmethod
    def create(
        cls, cfg: Config, db: Database, session: aiohttp.ClientSession, settings: dict[str, Any]
    ) -> Source:
        """Build an instance from a source spec. Raise ``ValueError`` on bad settings."""
        return cls(cfg, db, session, settings)  # type: ignore[call-arg]

    @abstractmethod
    async def poll(self) -> list[Candidate]:
        """Return candidates that are ready for processing."""

    @abstractmethod
    async def finalize(self, candidate: Candidate, outcome: Outcome) -> None:
        """Called exactly once per polled candidate after processing."""

    def status(self) -> dict[str, Any]:
        """A JSON-serialisable snapshot for the status page."""
        return {
            "name": self.name,
            "type": self.type_name or type(self).__name__.lower(),
            "last_poll_at": None,
            "next_poll_at": None,
            "last_result": None,
            "waiting_for_space": False,
            "auto_approve": self.auto_approve,
        }

    def cancel(self) -> bool:
        """Stop the download in progress; return whether there was one to stop."""
        return False

    def set_progress(self, callback: Callable[[dict[str, Any]], None] | None) -> None:
        """Register a callback that receives download progress (default: ignored)."""

    def force_poll(self) -> None:
        """Make the next ``poll()`` run regardless of its schedule (default: no-op)."""

    def set_space_check(self, callback: Callable[[str], int | None] | None) -> None:
        """Register ``callback(candidate_key)``: the free bytes a deferred candidate waits for, or None
        (default: ignored). Sources that download can skip such a release until the space is there."""

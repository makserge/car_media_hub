"""Stream budget of the sync API: how many file or probe streams one client and the whole add-on may run at once.

Music files, region files, resource files and ``/api/probe`` share one :class:`StreamBudget` (per app). A client is
identified by its remote address. Up to ``MAX_STREAMS_PER_CLIENT`` streams per client and ``MAX_STREAMS_TOTAL`` in total are
accepted; a request above that gets ``503`` with ``Retry-After`` (``RETRY_AFTER_BUSY`` seconds). Every file response
announces the per-client limit in the ``X-Max-Streams`` header, so a client can size its worker pool by it.

A client that comes back after its car slept often finds its old connection still open (the add-on waits for it to accept
data, up to the write timeout). When a limit is reached, a stalled stream (no byte accepted for ``STALL_SECONDS``) of the
same client, else the one stalled longest, is dropped to make room; a stream whose connection is already closing counts as
stalled. A stream that still moves is never dropped to make room for the same client. Only when the add-on is at its total
limit and one client holds clearly more streams than the asking one (``FAIR_GAP`` or more), the oldest stream of that client
is given up, so two busy clients cannot lock out a third (the dropped transfer resumes with a ranged request).

A dropped stream is never torn down by closing its socket from outside: it may be inside ``loop.sendfile``, whose selector
registration would then outlive the socket and break the next connection that reuses the file descriptor. Instead the
handler task of the stream is cancelled; the handler unwinds (``SendfileResponse.send`` and the probe) and aborts its own
transport afterwards (:func:`dropped_by_budget`). The slot is free the moment the stream is dropped.

The client key is the remote address of the connection. Port 8100 is published directly, so LAN clients have distinct
addresses; behind a reverse proxy, a VPN add-on or Docker's userland proxy all clients look like one (``X-Forwarded-For`` is
deliberately not trusted: any client could fake it to dodge the limit). They then share one budget of 8 streams, and the
fairness rule above keeps the total from being used up by one of them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

log = logging.getLogger(__name__)

MAX_STREAMS_PER_CLIENT = 8
MAX_STREAMS_TOTAL = 16
STALL_SECONDS = 30.0  # no byte accepted for this long: the stream is stalled (progress is marked per sent segment)
DROP_ABORT_AFTER = 2.0  # safety net: a dropped stream whose handler never closed its transport is aborted this much later
FAIR_GAP = 2  # at the total limit the heaviest client gives up a stream when it holds this many more than the asking one
RETRY_AFTER_BUSY = "2"
MAX_STREAMS_HEADER = "X-Max-Streams"

STREAMS = web.AppKey("stream_budget", object)


@dataclass
class Stream:
    """A running stream: who (client address), what (free-form key), when it last accepted data."""

    id: int
    client: str
    key: tuple
    transport: Any = field(default=None, repr=False)
    progress_at: float = field(default_factory=time.monotonic)
    dropped: bool = False
    task: Any = field(default=None, repr=False)  # the handler task (set for a stream with a transport)
    finishing: bool = False  # sending its last segment: its slot is already free (see ``finishing_now``)
    budget: Any = field(default=None, repr=False)

    def moved(self) -> None:
        self.progress_at = time.monotonic()

    def finishing_now(self) -> None:
        """The last segment is about to go out: give its slot back now (the stream stays known until ``end``).

        With ``loop.sendfile`` the client has every byte before the handler resumes, so a client that asks for its next
        file at once, with exactly 8 workers, must not find this slot taken. The tails are bounded: at most ``total``
        of them at a time (``begin`` refuses above that), a stalled one can still be dropped, and each ends within
        the write timeout."""
        self.finishing = True

    def finish(self) -> None:
        """Give the slot back now (the last byte is out); ``StreamBudget.end`` afterwards is harmless."""
        if self.budget is not None:
            self.budget.end(self)


def _closing(transport: Any) -> bool:
    check = getattr(transport, "is_closing", None)
    try:
        return bool(check()) if check is not None else False
    except Exception:  # noqa: BLE001 - an odd transport object must not break the budget
        return False


def _abort_quietly(transport: Any) -> None:
    try:
        transport.abort()
    except Exception:  # noqa: BLE001
        log.debug("Sync API: abort of a dropped stream failed", exc_info=True)


def _current_task() -> Any:
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop (unit tests)
        return None


def dropped_by_budget(stream: Stream | None) -> bool:
    """Call in ``except CancelledError``: True when the cancellation was the budget dropping ``stream``.

    The cancellation is then cleared (``uncancel``) so the handler may abort its own connection and return; it stays
    pending (False) when something else cancelled the task too (shutdown).
    """
    if stream is None or not stream.dropped:
        return False
    task = _current_task()
    if task is not None and task.uncancel() > 0:
        return False
    return True


class StreamBudget:
    """Counts running streams per client and in total (event loop only, no locking needed)."""

    def __init__(
        self, per_client: int = MAX_STREAMS_PER_CLIENT, total: int = MAX_STREAMS_TOTAL, stall_seconds: float = STALL_SECONDS,
    ) -> None:
        self.per_client = per_client
        self.total = total
        self.stall_seconds = stall_seconds
        self._streams: dict[int, Stream] = {}
        self._seq = 0

    @property
    def active(self) -> int:
        """Slots in use (streams that are sending their last segment have given theirs back)."""
        return sum(1 for s in self._streams.values() if not s.finishing)

    @property
    def finishing(self) -> int:
        return sum(1 for s in self._streams.values() if s.finishing)

    def active_for(self, client: str) -> int:
        return sum(1 for s in self._streams.values() if s.client == client and not s.finishing)

    def begin(self, client: str, key: tuple, transport: Any = None) -> Stream | None:
        """Take a slot (None: the client or the add-on is at its limit with streams that make progress).

        A stream with a ``transport`` belongs to the calling handler task: that task is cancelled if the stream is dropped.
        """
        sending = [s for s in self._streams.values() if not s.finishing]
        if self.active_for(client) >= self.per_client:
            self._drop_stalled([s for s in sending if s.client == client], client, key)
        if self.active >= self.total:
            self._drop_stalled([s for s in self._streams.values() if not s.finishing], client, key)
        if self.active >= self.total:
            self._drop_for_fairness(client)
        if self.finishing >= self.total:  # too many tails of stalled clients: they only wait for the write timeout
            self._drop_stalled([s for s in self._streams.values() if s.finishing], client, key)
        if self.active_for(client) >= self.per_client or self.active >= self.total or self.finishing >= self.total:
            return None
        self._seq += 1
        task = _current_task() if transport is not None else None
        stream = Stream(self._seq, client, key, transport, task=task, budget=self)
        self._streams[stream.id] = stream
        return stream

    def end(self, stream: Stream) -> None:
        self._streams.pop(stream.id, None)

    def _drop_stalled(self, candidates: list[Stream], client: str, key: tuple) -> None:
        now = time.monotonic()
        stalled = [s for s in candidates if now - s.progress_at >= self.stall_seconds or _closing(s.transport)]
        if not stalled:
            return
        victim = min(stalled, key=lambda s: (s.client != client, s.key != key, s.progress_at))
        self._drop(victim, "stalled")

    def _drop_for_fairness(self, client: str) -> None:
        """At the total limit: the client with the most streams gives up its oldest one for a client with clearly fewer."""
        counts: dict[str, int] = {}
        for s in self._streams.values():
            if not s.finishing:
                counts[s.client] = counts.get(s.client, 0) + 1
        mine = counts.get(client, 0)
        if mine >= self.per_client:
            return
        heaviest = max((n for c, n in counts.items() if c != client), default=0)
        if heaviest == 0 or heaviest < mine + FAIR_GAP:
            return
        victims = [s for s in self._streams.values() if s.client != client and not s.finishing and counts[s.client] == heaviest]
        self._drop(min(victims, key=lambda s: s.id), "fair share")

    def _drop(self, victim: Stream, why: str) -> None:
        self._streams.pop(victim.id, None)
        victim.dropped = True
        task = victim.task
        if task is not None and not task.done() and task is not _current_task():
            task.cancel()  # the handler unwinds (also out of loop.sendfile) and aborts its transport itself
            if victim.transport is not None:
                try:
                    asyncio.get_running_loop().call_later(DROP_ABORT_AFTER, _abort_quietly, victim.transport)
                except RuntimeError:
                    pass
        elif victim.transport is not None:
            victim.transport.abort()
        log.info("Sync API: dropped a stream (%s, %s) for a new request", why, victim.key[1:] or victim.key)


def busy_response(headers: dict[str, str] | None = None) -> web.Response:
    """The 503 answered when a stream budget is used up (a JSON error body like every other error of the sync API)."""
    resp = web.Response(status=503, body=b'{"error":"busy"}', content_type="application/json", charset="utf-8")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Retry-After"] = RETRY_AFTER_BUSY
    for name, value in (headers or {}).items():
        resp.headers[name] = value
    return resp

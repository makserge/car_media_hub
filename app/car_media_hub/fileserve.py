"""Shared file serving of the sync API: range parsing, ``If-Range`` and zero-copy sending of an already opened file.

``SendfileResponse`` sends a regular file that the caller opened safely (no symlink, ``fstat`` checked) with
``loop.sendfile`` (``os.sendfile`` on a plain TCP socket, the same mechanism as ``aiohttp.web.FileResponse``), in segments
of adaptive size. Between segments it enforces the write timeout and tells the stream budget that the client is still
reading. A segment is at most ``SEND_SEGMENT`` (1 MiB, which costs the least CPU on a fast link) and at least
``SEND_SEGMENT_MIN``; it doubles while a segment takes less than ``SEGMENT_FAST_SECONDS`` and halves when one takes more than
``SEGMENT_SLOW_SECONDS``, so a slow link settles on segments of about a second each and a stream that moves at all keeps
marking progress: a stream is stalled only after ``STALL_SECONDS`` without a single finished segment (below about
``SEND_SEGMENT_MIN / STALL_SECONDS`` = 1 KB/s). When the budget drops the stream, the handler
task is cancelled: ``loop.sendfile`` unwinds and removes its selector registration while the socket is still open, and only
then is the connection aborted (see ``streams.py``). It is used instead of ``web.FileResponse`` itself because that one opens the path a second time (a rename
could swap the file after the safety checks) and only understands ``If-Range`` with a date, never with an ETag.
"""
from __future__ import annotations

import asyncio
import logging
import re
import socket
import sys
import time
from typing import Any

from aiohttp import web

from .streams import Stream, dropped_by_budget

log = logging.getLogger(__name__)

SEND_SEGMENT = 1 << 20  # largest segment (bytes per loop.sendfile call): the write timeout and the progress mark apply per segment
SEND_SEGMENT_MIN = 32 << 10  # smallest segment, where a slow link settles
SEGMENT_FAST_SECONDS = 0.25  # a segment faster than this doubles the next one
SEGMENT_SLOW_SECONDS = 1.0  # a segment slower than this halves the next one
READ_CHUNK = 1 << 20  # chunk (and segment) of the copy path (Windows, tests, a loop without sendfile): progress is marked per chunk
WRITE_TIMEOUT = 120.0  # abort a download when the client takes this long to accept one segment
DSCP_CS1_TOS = 0x20  # DSCP CS1 ("background", scavenger class) in the IP TOS byte: Wi-Fi maps it to the background queue

# loop.sendfile everywhere except Windows, which only runs the tests and the benchmark here: its TransmitFile serves just a
# few requests at a time on a workstation edition, the others wait for a free slot (or for ever behind slow readers)
USE_SENDFILE = sys.platform != "win32"

SYNC_DSCP = web.AppKey("sync_dscp", bool)  # option sync_dscp (default on): mark file and probe connections DSCP CS1

_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")


class Unsatisfiable(Exception):
    pass


def _num(digits: str) -> int:
    """An ASCII digit string as a number, capped (``int()`` refuses more than 4300 digits)."""
    stripped = digits.lstrip("0")
    return min(int(stripped), 1 << 62) if 0 < len(stripped) <= 18 else (0 if not stripped else 1 << 62)


def parse_range(value: str, size: int) -> tuple[int, int] | None:
    """The ``(first, last)`` bytes of a single ``bytes=a-b``, ``bytes=a-`` or ``bytes=-n`` range.

    None means "ignore the header and send everything" (several ranges, other units, bad syntax, last < first);
    :class:`Unsatisfiable` means 416 (the range starts behind the end, or a suffix of length 0).
    """
    text = value.strip()
    if "," in text:
        return None
    match = _RANGE.fullmatch(text)
    if match is None:
        return None
    first, last = match.group(1), match.group(2)
    if not first and not last:
        return None
    if not first:  # suffix: the last n bytes
        n = _num(last)
        if n == 0:
            raise Unsatisfiable
        return max(0, size - n), size - 1
    start = _num(first)
    if last:
        end = _num(last)
        if end < start:
            return None
    else:
        end = size - 1
    if start >= size:
        raise Unsatisfiable
    return start, min(end, size - 1)


def if_range_ok(header: str | None, etag: str, last_modified: str, mtime_ns: int | None = None) -> bool:
    """A Range request is honoured only when If-Range (if present) names the current version (RFC 9110 section 13.1.5).

    Only a strong validator matches: the exact ``ETag`` (a ``W/`` prefixed one never does), or the exact ``Last-Modified``
    string as served (whole seconds). A date is a strong validator only when the file was last modified at least one
    second ago (section 8.8.2.2: a rewrite within the same second would leave the same date); with ``mtime_ns`` given a
    younger file (or one dated in the future) never matches by date. The ETag is the safe choice and what the clients send.
    """
    if header is None:
        return True
    value = header.strip()
    if value == etag:
        return True
    if value == last_modified:
        return mtime_ns is None or time.time_ns() - mtime_ns >= 1_000_000_000
    return False


def mark_background(request: web.Request) -> None:
    """Mark the connection's traffic DSCP CS1 so Wi-Fi gives it the background class (best effort, never raises).

    Does nothing when the option ``sync_dscp`` is off (``SYNC_DSCP`` of the app is False).
    """
    if not request.app.get(SYNC_DSCP, True):
        return
    transport = request.transport
    sock = transport.get_extra_info("socket") if transport is not None else None
    if sock is None:
        return
    try:
        if sock.family == socket.AF_INET:
            if sock.getsockopt(socket.IPPROTO_IP, socket.IP_TOS) != DSCP_CS1_TOS:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, DSCP_CS1_TOS)
        elif sock.family == socket.AF_INET6 and hasattr(socket, "IPV6_TCLASS"):
            if sock.getsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS) != DSCP_CS1_TOS:
                sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_TCLASS, DSCP_CS1_TOS)
    except (OSError, AttributeError, ValueError):
        log.debug("Sync API: cannot set the traffic class", exc_info=True)


def abort(request: web.Request) -> None:
    transport = request.transport
    if transport is not None:
        transport.abort()


class SendfileResponse(web.StreamResponse):
    """A response whose body is ``count`` bytes of ``fobj`` from ``offset``, sent without copying through Python.

    ``send(request)`` does everything (``prepare``, body, end of response); the handler returns the response afterwards.
    Errors in the body abort the connection (never a short body that ends cleanly): a client that went away or stopped
    reading ends quietly, any other error raises ``ConnectionResetError`` after the abort.
    """

    def __init__(
        self, fobj: Any, offset: int, count: int, *, status: int = 200, headers: dict[str, str] | None = None,
        stream: Stream | None = None, timeout: float | None = None, what: str = "file",
    ) -> None:
        super().__init__(status=status, headers=headers)
        self._fobj = fobj
        self._offset = offset
        self._count = count
        self._stream = stream
        self._timeout = timeout
        self._what = what
        self.content_length = count

    async def send(self, request: web.Request) -> None:
        timeout = WRITE_TIMEOUT if self._timeout is None else self._timeout
        mark_background(request)
        offset, left = self._offset, self._count
        size = SEND_SEGMENT_MIN  # grows fast on a fast link (slow start); the copy path always works in READ_CHUNK
        try:
            await self.prepare(request)
            if request.method == "HEAD" or self._count == 0:
                await self.write_eof()
                return
            while left:
                transport = request.transport
                if transport is None:
                    raise ConnectionResetError("connection lost")
                part = min(size if USE_SENDFILE else READ_CHUNK, left)
                if part == left and self._stream is not None:
                    self._stream.finishing_now()
                began = time.monotonic()
                async with asyncio.timeout(timeout):
                    sent = await self._send_part(request, offset, part)
                took = time.monotonic() - began
                if took < SEGMENT_FAST_SECONDS:
                    size = min(size * 2, SEND_SEGMENT)
                elif took > SEGMENT_SLOW_SECONDS:
                    size = max(size // 2, SEND_SEGMENT_MIN)
                if sent != part:
                    raise OSError("file is shorter than announced")
                offset += part
                left -= part
                if self._stream is not None:
                    self._stream.moved()
            if self._stream is not None:
                self._stream.finish()  # every byte is out: the slot is free before the client can ask for the next file
            await self.write_eof()
        except ConnectionError:
            log.debug("Sync API: client went away during %s", self._what)
            abort(request)
        except TimeoutError:
            # the box went to sleep or lost its link: nothing to report, it resumes from its partial file
            log.info("Sync API: client stopped reading %s at about byte %d, connection closed", self._what, offset)
            abort(request)
        except asyncio.CancelledError:
            if dropped_by_budget(self._stream):  # the slot went to a newer request: unwind done, now close the socket
                log.info("Sync API: stream of %s given up for a newer request, at byte %d", self._what, offset)
                abort(request)
                return
            raise
        except Exception as exc:  # noqa: BLE001 - never end a short body cleanly
            abort(request)
            if self._stream is not None and self._stream.dropped:  # the slot went to a newer request of the same client
                return
            log.warning("Sync API: aborting download of %s: %s", self._what, exc)
            raise ConnectionResetError(str(exc)) from exc

    async def _send_part(self, request: web.Request, offset: int, count: int) -> int:
        if not USE_SENDFILE:
            return await self._copy_part(offset, count)
        loop = asyncio.get_running_loop()
        try:
            return int(await loop.sendfile(request.transport, self._fobj, offset, count))
        except NotImplementedError:  # loop.sendfile falls back by itself; this covers a transport that cannot do either
            return await self._copy_part(offset, count)

    async def _copy_part(self, offset: int, count: int) -> int:
        """The body without sendfile: 1 MiB reads in a worker thread, written through the response."""
        loop = asyncio.get_running_loop()
        left = count
        await loop.run_in_executor(None, self._fobj.seek, offset)
        while left:
            chunk = await loop.run_in_executor(None, self._fobj.read, min(READ_CHUNK, left))
            if not chunk:
                break
            await self.write(chunk)
            left -= len(chunk)
            if self._stream is not None:
                self._stream.moved()
        return count - left

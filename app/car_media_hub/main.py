"""Add-on entry point: wires config, database, sources, HA client, controller, web UI and sync API."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiohttp
from aiohttp import web

from car_media_hub import archive
from car_media_hub.browser import BROWSER_KEY
from car_media_hub.config import Config, load_config, path_relation
from car_media_hub.db import Database
from car_media_hub.ha import HAClient
from car_media_hub.diskspace import min_free_bytes
from car_media_hub.build_info import build_info
from car_media_hub.charger_refresh import ChargerRefresher
from car_media_hub.regions import RegionManager
from car_media_hub.pipeline import DEFAULT_REVIEW_URL, EVENT_DECISION, EVENT_MOBILE_ACTION, Controller
from car_media_hub.sources import Source, build_sources, builtin_specs, load_plugins, load_sources_file
from car_media_hub.sync import SyncServer, start_sync_server
from car_media_hub.web import create_app

logger = logging.getLogger(__name__)

WEB_HOST = "0.0.0.0"
WEB_PORT = 8099
SUPERVISOR_INFO_URL = "http://supervisor/addons/self/info"
PANELS_TIMEOUT = 10.0  # seconds for the one-shot "which frontend panels exist" websocket query
# How long start-up waits for the library folder to answer. A dead network mount (Supervisor
# network storage behind a systemd automount) blocks about 30 s per access before it errors.
LIBRARY_PROBE_TIMEOUT = 60.0
# s6-overlay's copy of the container environment. The base image's s6 init starts run.sh with an empty
# environment, and with-contenv (run.sh's shebang) brings the Supervisor's variables back from here.
S6_ENV_DIR = Path("/run/s6/container_environment")


def supervisor_token() -> str:
    """The Supervisor API token, or "" if there is none.

    Read from the environment; if run.sh was started without with-contenv, from s6's copy of it.
    """
    token = os.environ.get("SUPERVISOR_TOKEN", "").strip()
    if token:
        return token
    try:
        return (S6_ENV_DIR / "SUPERVISOR_TOKEN").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


async def review_link_for(ha: HAClient | None, slug: str) -> str:
    """Frontend link to the add-on's Ingress panel, in the form the running Home Assistant serves.

    Current Home Assistant (add-ons renamed "apps") has an ``app`` panel and the panel is at
    ``/app/<slug>``; older versions have a ``hassio`` panel and ``/hassio/ingress/<slug>``. The
    panels are asked with the websocket command ``get_panels``; if that fails or shows neither,
    the current form is used.
    """
    if ha is not None:
        try:
            panels = await ha.ws_command({"type": "get_panels"}, timeout=PANELS_TIMEOUT)
            if isinstance(panels, dict) and "app" not in panels and "hassio" in panels:
                return f"/hassio/ingress/{slug}"
        except Exception as exc:  # noqa: BLE001 - best effort only
            logger.warning("Could not ask Home Assistant for its panels: %s", exc)
    return f"/app/{slug}"


async def fetch_review_url(session: aiohttp.ClientSession, token: str,
                           url: str = SUPERVISOR_INFO_URL, ha: HAClient | None = None) -> str:
    """Best-effort lookup of the frontend link to this add-on's ingress panel.

    Prefers a panel link built from the add-on's real slug (repository add-ons get a hash prefix;
    see ``review_link_for``), falls back to ``data.ingress_url`` and finally to the default.
    """
    try:
        async with session.get(
            url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        data = payload.get("data") or {}
        slug = data.get("slug")
        if isinstance(slug, str) and slug:
            return await review_link_for(ha, slug)
        ingress_url = data.get("ingress_url")
        if isinstance(ingress_url, str) and ingress_url:
            return ingress_url
    except Exception as exc:  # noqa: BLE001 - best effort only
        logger.warning("Could not fetch add-on info from Supervisor: %s", exc)
    return DEFAULT_REVIEW_URL


def warn_library_missing(cfg: Config) -> bool:
    """True when ``library_dir`` is missing or cannot be read (blocking: run it off the loop).

    A WARNING is logged when it is missing and the sync API is on, and always when it cannot be
    read: a network share that is down answers ENODEV/EIO/ETIMEDOUT/EHOSTDOWN, which Python 3.12's
    ``Path.is_dir`` re-raises. Start-up must go on then (the sync API answers 503 for it).
    """
    try:
        if cfg.library_dir.is_dir():
            return False
    except OSError as exc:
        logger.warning(
            "library_dir %s cannot be read (%s); the sync API answers 503 and approvals wait "
            "until it can", cfg.library_dir, exc.strerror or exc)
        return True
    if cfg.sync_enabled:
        logger.warning(
            "library_dir %s does not exist; the sync API answers 503 until it does "
            "(create it or fix the library_dir option)", cfg.library_dir)
    return True


_PENDING = object()


async def run_bounded(func: Callable[[], object], timeout: float) -> object:
    """Run the blocking ``func`` in a daemon thread; its result, or ``_PENDING`` after ``timeout``.

    Unlike ``asyncio.to_thread`` a stat that hangs on a dead mount cannot keep the interpreter from
    exiting (the default executor is joined at shutdown). Exceptions of ``func`` propagate.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[object] = loop.create_future()
    future.add_done_callback(lambda f: f.cancelled() or f.exception())  # a late failure is not "never retrieved"

    def deliver(setter: Callable[[object], None], value: object) -> None:
        if not future.done():
            setter(value)

    def work() -> None:
        try:
            result = func()
        except BaseException as exc:  # noqa: BLE001 - handed to the awaiting task
            message: tuple[Callable[[object], None], object] = (future.set_exception, exc)
        else:
            message = (future.set_result, result)
        try:
            loop.call_soon_threadsafe(deliver, *message)
        except RuntimeError:  # the loop is closed: nobody waits any more
            pass

    threading.Thread(target=work, name="mw-bounded", daemon=True).start()
    await asyncio.wait([future], timeout=timeout)
    if not future.done():
        return _PENDING
    return future.result()


def prepare_folders(cfg: Config, library_missing: bool) -> None:
    """Create the inbox and staging folders (blocking: run it off the loop).

    Never creates ``library_dir`` as a parent: with a missing or unmounted library the folders that lie
    inside it are left alone (a WARNING names them), because creating them would create the library and
    the sync API would answer an empty list (``200 []``, which clients mirror) instead of 503. The inbox
    folder is only created while the inbox is enabled. A folder that cannot be created is logged, not fatal.
    """
    for directory, wanted in ((cfg.inbox_dir, cfg.inbox_enabled), (cfg.staging_dir, True)):
        if not wanted:
            continue
        if library_missing and path_relation(directory, cfg.library_dir) in ("inside", "same"):
            logger.warning(
                "Not creating %s: it lies inside library_dir %s, which is missing or cannot be read. "
                "Creating it would create the library. It is created once the library is back.",
                directory, cfg.library_dir)
            continue
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Cannot create %s: %s", directory, exc.strerror or exc)


async def check_library(cfg: Config) -> bool:
    """``warn_library_missing`` without blocking start-up for long: True also when it never answers."""
    try:
        missing = await run_bounded(lambda: warn_library_missing(cfg), LIBRARY_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001 - start-up must go on whatever the share does
        logger.exception("Checking library_dir %s failed", cfg.library_dir)
        return True
    if missing is _PENDING:
        logger.warning("library_dir %s did not answer within %.0f s; treating it as unavailable "
                       "(the sync API answers 503 until it answers)", cfg.library_dir, LIBRARY_PROBE_TIMEOUT)
        return True
    return bool(missing)


async def supervise(name: str, factory: Callable[[], Awaitable[None]], stop: asyncio.Event, *,
                    min_delay: float = 10.0, max_delay: float = 600.0) -> None:
    """Run ``factory()`` until ``stop`` is set; restart it with backoff when it fails or ends early.

    A crashed controller must not take the healthy sync API and web UI down with it (the process
    would exit 0 and nothing restarts it). The controller runs ``recover()`` first on every start,
    so a restart is also a retry of the recovery, before any scan.
    """
    loop = asyncio.get_running_loop()
    delay = min_delay
    while not stop.is_set():
        started = loop.time()
        try:
            await factory()
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or current.cancelling():
                raise  # really cancelled (shutdown)
            logger.error("Task %s was cancelled unexpectedly; restarting in %.0f s", name, delay)
        except Exception:  # noqa: BLE001
            logger.exception("Task %s crashed; restarting in %.0f s", name, delay)
        else:
            if stop.is_set():
                return
            logger.error("Task %s ended unexpectedly; restarting in %.0f s", name, delay)
        if loop.time() - started > max_delay:
            delay = min_delay  # it ran for a good while: a fresh failure, not a crash loop
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=delay)
        delay = min(delay * 2, max_delay)


def _install_signal_handlers(stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError, ValueError):  # Windows / non-main thread
            logger.debug("Signal handler for %s not supported here", sig)


def build_all_sources(cfg: Config, db: Database, session: aiohttp.ClientSession) -> list[Source]:
    """Load source plugins and build builtin + file sources."""
    load_plugins(cfg.plugins_dir)
    specs = [*builtin_specs(cfg), *load_sources_file(cfg.sources_file)]
    return build_sources(cfg, db, session, specs)


def make_region_manager(cfg: Config) -> RegionManager:
    """The offline map regions manager: uploads and imports keep ``min_free_space_gb`` free, like the music pipeline."""
    return RegionManager(
        cfg.region_storage_dir, cfg.region_max_file_mb * 1024 * 1024, min_free=min_free_bytes(cfg),
        import_timeout=cfg.region_import_max_hours * 3600, transfer_gzip=cfg.region_transfer_gzip,
    )


def make_charger_refresher(cfg: Config, regions: RegionManager) -> ChargerRefresher:
    """Weekly build of the charger database into <region_storage_dir>/.resources/chargers (served by resources_api)."""
    return ChargerRefresher(regions.root, cfg.charger_refresh_days, cfg.charger_source_url)


async def start_sync(cfg: Config, db: Database, controller: Controller, regions: RegionManager | None = None) -> SyncServer:
    """Start the sync API (never raises) and connect it to the controller."""
    server = await start_sync_server(cfg, db, trips=db, ha=controller.ha, regions=regions)
    controller.on_library_changed.append(server.invalidate)
    # a refused approval re-lists, so the status bar shows "paused" / the 503 reason without a client request
    controller.on_library_problem.append(server.invalidate)
    controller.sync_status = server.status
    return server


async def stop_browser(runner: web.AppRunner | None) -> None:
    """Stop the embedded browser (if it is enabled and running) and kill what is left of it; never raises."""
    browser = runner.app.get(BROWSER_KEY) if runner is not None else None
    if browser is None:
        return
    try:
        await asyncio.wait_for(browser.stop(), 12)
    except Exception:  # noqa: BLE001
        logger.warning("Stopping the browser failed; killing it", exc_info=True)
    browser.kill_now()


async def amain() -> None:
    """Run the add-on until SIGTERM/SIGINT."""
    try:
        cfg = load_config()
    except ValueError as exc:  # a bad option (e.g. overlapping folders): say so, exit non-zero
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
        logger.error("Invalid configuration: %s", exc)
        raise SystemExit(1) from None
    logging.basicConfig(
        level=getattr(logging, cfg.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )
    logger.info("car_media_hub build %s", build_info()["id"])
    archive.kill_extractor_leftovers()  # nothing of the 7z/bsdtar user survives a restart
    token = supervisor_token()
    if not token:  # e.g. run outside Home Assistant: no notifications, events or review link without it
        logger.error("SUPERVISOR_TOKEN is not set: Car Media and Trips runs only as a Home Assistant app, "
                     "started by its run.sh through with-contenv")
        raise SystemExit(1)
    # library_dir is deliberately NOT created here (SYNC_PROTOCOL section 4): a missing or
    # unmounted library must answer 503, not turn into an empty folder that is served as "200 []"
    # (the car app mirrors the list and would delete its copies). The first approval creates it.
    # Nor may creating the staging or inbox folder create it as their parent (they may lie inside it).
    cfg.data_dir.mkdir(parents=True, exist_ok=True)  # the database must exist: a failure here is fatal
    library_missing = await check_library(cfg)
    try:
        done = await run_bounded(lambda: prepare_folders(cfg, library_missing), LIBRARY_PROBE_TIMEOUT)
    except Exception:  # noqa: BLE001
        logger.exception("Preparing the inbox and staging folders failed")
    else:
        if done is _PENDING:
            logger.warning("Creating the inbox and staging folders is taking too long; going on without it")

    db = Database(cfg.data_dir / "car_media_hub.db")
    stop = asyncio.Event()
    _install_signal_handlers(stop)

    runner: web.AppRunner | None = None
    sync_server: SyncServer | None = None
    regions = make_region_manager(cfg)
    chargers = make_charger_refresher(cfg, regions)
    tasks: list[asyncio.Task[None]] = []
    try:
        async with aiohttp.ClientSession() as session:
            ha = HAClient(session, token)
            review_url = await fetch_review_url(session, token, ha=ha)
            logger.info("Notification review link: %s", review_url)

            sources = build_all_sources(cfg, db, session)
            if not sources:
                logger.warning("No sources enabled; only the review UI will be available")

            controller = Controller(cfg, db, ha, sources, review_url=review_url)
            try:
                await regions.start()
            except Exception:  # noqa: BLE001 - the regions are optional: never stop the add-on
                logger.exception("Starting the offline map regions failed")
            try:
                await chargers.start()
            except Exception:  # noqa: BLE001 - optional too
                logger.exception("Starting the charger database refresh failed")
            sync_server = await start_sync(cfg, db, controller, regions)

            runner = web.AppRunner(create_app(controller, regions, chargers), access_log=None)  # the UI polls api/status
            await runner.setup()
            await web.TCPSite(runner, WEB_HOST, WEB_PORT).start()
            logger.info("Car Media and Trips started (web UI on port %d)", WEB_PORT)

            tasks = [
                asyncio.create_task(
                    supervise("controller", lambda: controller.run(stop), stop), name="controller"),
                asyncio.create_task(
                    supervise(
                        "ha-events",
                        lambda: ha.listen_events([EVENT_MOBILE_ACTION, EVENT_DECISION],
                                                 controller.handle_event, stop),
                        stop),
                    name="ha-events",
                ),
            ]
            stop_task = asyncio.create_task(stop.wait(), name="stop")
            done, _ = await asyncio.wait([*tasks, stop_task], return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task is stop_task:
                    continue
                if task.cancelled():
                    logger.error("Task %s was cancelled unexpectedly", task.get_name())
                elif task.exception():
                    logger.error("Task %s crashed", task.get_name(), exc_info=task.exception())
            stop.set()
            stop_task.cancel()
            await stop_browser(runner)  # first: Chromium and Xvfb must not outlive the add-on
            # Give tasks a moment to observe ``stop`` before cancelling them.
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            for task in pending:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            # the sync server's requests use the HA session: stop it BEFORE that session closes (leaving this block)
            if sync_server is not None:
                with contextlib.suppress(Exception):
                    await sync_server.stop()
                sync_server = None
            with contextlib.suppress(Exception):
                await chargers.stop()
            with contextlib.suppress(Exception):
                await regions.stop()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await stop_browser(runner)
        if sync_server is not None:
            await sync_server.stop()
        with contextlib.suppress(Exception):
            await chargers.stop()
        with contextlib.suppress(Exception):
            await regions.stop()
        if runner is not None:
            await runner.cleanup()
        db.close()
        logger.info("Car Media and Trips stopped")


def main() -> None:
    """Console entry point."""
    asyncio.run(amain())


if __name__ == "__main__":
    main()

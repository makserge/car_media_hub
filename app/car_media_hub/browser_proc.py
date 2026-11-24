"""How the embedded Chromium (and Xvfb) is started: an unprivileged user, a clean environment, the Chromium
sandbox when the container allows it, resource limits and the cleanup of leftover processes.

Everything that can be decided without starting a process is a pure function (env, command line, sysctl
parsing, /proc scanning) so it is covered by tests.
"""
from __future__ import annotations

import asyncio
import errno
import logging
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping

try:  # imported before any fork: an import inside preexec_fn can deadlock on the import lock in a threaded process
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

log = logging.getLogger(__name__)

BROWSER_USER = "browser"  # created in the Dockerfile: no shell, home = the profile folder
PATH_VALUE = "/usr/local/bin:/usr/bin:/bin"
# Nothing else may ever reach Chromium: no SUPERVISOR_TOKEN, no HA / hassio variables, no sync token.
ENV_ALLOWED = frozenset({
    "PATH", "HOME", "DISPLAY", "LANG", "LC_ALL", "TMPDIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR",
    "GOOGLE_API_KEY", "GOOGLE_DEFAULT_CLIENT_ID", "GOOGLE_DEFAULT_CLIENT_SECRET",
})
_SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "SUPERVISOR", "HASSIO", "HASSOS", "HOMEASSISTANT", "HA_", "API_TOKEN", "KEY")

WEBRTC_POLICY = "disable_non_proxied_udp"
RENDERER_LIMIT = 2
JS_HEAP_MB = 384
MAX_LIFETIME_SECONDS = 4 * 3600  # a session is ended after this long, viewer or not


def is_wrapper_script(binary: str) -> bool:
    """True when ``binary`` is a shell script (Debian's /usr/bin/chromium wrapper) rather than the real executable."""
    try:
        with open(binary, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def build_browser_env(profile: Path | str, display: str | None = None, *, wrapper: bool = False) -> dict[str, str]:
    """The complete environment of Chromium and Xvfb: an explicit minimal dict, never a copy of ``os.environ``.

    With ``wrapper`` (the browser is started through a shell script such as Debian's /usr/bin/chromium, which looks up
    init files below HOME and honours CHROMIUM_FLAGS) HOME is a root-owned, empty location instead of the profile, so
    the browser user cannot plant anything the script would run. Chromium keeps its data in the XDG folders below the
    profile either way."""
    profile = str(profile)
    env = {
        "PATH": PATH_VALUE,
        "HOME": "/nonexistent" if wrapper else profile,
        "LANG": "C.UTF-8",
        "TMPDIR": f"{profile}/tmp",
        "XDG_CONFIG_HOME": f"{profile}/.config",
        "XDG_CACHE_HOME": f"{profile}/.cache",
        # no "Google API keys are missing" bar (it pushes the page down in the picture and clicks land on the wrong link)
        "GOOGLE_API_KEY": "no",
        "GOOGLE_DEFAULT_CLIENT_ID": "no",
        "GOOGLE_DEFAULT_CLIENT_SECRET": "no",
    }
    if display:
        env["DISPLAY"] = display
    assert_env_clean(env)
    return env


def assert_env_clean(env: Mapping[str, str]) -> None:
    """Raise when ``env`` holds anything that is not on the allowlist or looks like a secret."""
    for key in env:
        upper = key.upper()
        if key not in ENV_ALLOWED:
            raise ValueError(f"environment variable {key} must not be passed to the browser")
        if key not in ("GOOGLE_API_KEY", "GOOGLE_DEFAULT_CLIENT_ID", "GOOGLE_DEFAULT_CLIENT_SECRET") and any(
                word in upper for word in _SECRET_WORDS):
            raise ValueError(f"environment variable {key} looks like a secret and must not be passed to the browser")


def chromium_args(binary: str, profile: Path | str, *, headless: bool, sandbox: bool, proxy_port: int,
                  window: tuple[int, int]) -> list[str]:
    """The command line. The sandbox flag is off only when the caller found no sandbox support; every request
    goes through the local filtering proxy (also loopback), and Chromium itself may resolve nothing."""
    args = [
        binary, *(["--headless=new"] if headless else []), *([] if sandbox else ["--no-sandbox"]),
        "--disable-gpu", "--disable-dev-shm-usage",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions", "--mute-audio", "--disk-cache-size=33554432",
        "--test-type", "--disable-infobars", "--force-color-profile=srgb", "--enable-features=OverlayScrollbar",
        "--disable-smooth-scrolling", "--disable-background-networking", "--disable-sync", "--disable-component-update",
        "--disable-breakpad", "--disable-crash-reporter", "--disable-domain-reliability", "--disable-print-preview",
        "--disable-features=Translate,MediaRouter,BackForwardCache,DnsOverHttps",
        # WebRTC may only use the proxy (no direct UDP / STUN / TURN to the LAN); the value belongs on the switch itself.
        # mDNS hiding of local addresses stays on (it is not disabled above).
        f"--force-webrtc-ip-handling-policy={WEBRTC_POLICY}", f"--webrtc-ip-handling-policy={WEBRTC_POLICY}",
        "--enforce-webrtc-ip-permission-check",
        "--disable-blink-features=AutomationControlled", "--force-device-scale-factor=1", "--lang=en-US",
        # resource limits
        f"--renderer-process-limit={RENDERER_LIMIT}", f"--js-flags=--max-old-space-size={JS_HEAP_MB}",
        "--disable-hang-monitor",
        # every request through the filtering proxy; the browser itself resolves nothing but the proxy's own address
        f"--proxy-server=http://127.0.0.1:{proxy_port}", "--proxy-bypass-list=<-loopback>",
        "--host-resolver-rules=MAP * ~NOTFOUND , EXCLUDE 127.0.0.1",
        f"--window-size={window[0]},{window[1]}", f"--user-data-dir={profile}", "--remote-debugging-port=0", "about:blank",
    ]
    return args


# ---------------------------------------------------------------- privilege drop
def unprivileged_user() -> tuple[int, int] | None:
    """(uid, gid) of the browser user. None when the add-on does not run as root (a development machine):
    the browser then runs as the current user. Raises when running as root without the user (a broken image)."""
    if os.name != "posix" or os.geteuid() != 0:
        return None
    import pwd  # POSIX only

    try:
        entry = pwd.getpwnam(BROWSER_USER)
    except KeyError:
        raise RuntimeError(
            f"the user '{BROWSER_USER}' does not exist in this image: the browser is not started as root") from None
    if entry.pw_uid == 0:
        raise RuntimeError(f"the user '{BROWSER_USER}' must not be root")
    return entry.pw_uid, entry.pw_gid


NPROC_LIMIT = 1024  # processes + threads of the browser user (Chromium needs a few hundred threads; a fork bomb is capped)


def _limit_nproc() -> None:  # runs in the child, after the user switch
    try:
        resource.setrlimit(resource.RLIMIT_NPROC, (NPROC_LIMIT, NPROC_LIMIT))
    except (AttributeError, ValueError, OSError):
        pass


def popen_drop_kwargs(ids: tuple[int, int] | None) -> dict[str, Any]:
    """Arguments for ``create_subprocess_exec`` that drop root (user, group and all supplementary groups)
    and cap the number of processes the browser user may own (RLIMIT_NPROC)."""
    if ids is None:
        return {"start_new_session": True}
    return {"user": ids[0], "group": ids[1], "extra_groups": [], "umask": 0o077, "start_new_session": True,
            "preexec_fn": _limit_nproc}


# ---------------------------------------------------------------- sandbox support
def sysctl_allows_userns(max_namespaces: str | None, unprivileged_clone: str | None) -> bool:
    """The kernel switches that must not forbid user namespaces (None = the file does not exist)."""
    try:
        if max_namespaces is not None and int(max_namespaces.strip()) <= 0:
            return False
        if unprivileged_clone is not None and int(unprivileged_clone.strip()) == 0:
            return False
    except ValueError:
        return False
    return True


def _read(path: str) -> str | None:
    try:
        return Path(path).read_text()
    except OSError:
        return None


# runs as the browser user in a throw-away interpreter: the same unshare calls Chromium's sandbox makes
USERNS_PROBE = (
    "import ctypes,sys;l=ctypes.CDLL(None,use_errno=True);"
    "sys.exit(0 if l.unshare(0x10000000)==0 and l.unshare(0x20000000|0x40000000)==0 else 1)"
)  # CLONE_NEWUSER, then CLONE_NEWPID|CLONE_NEWNET inside it


async def detect_sandbox_support(ids: tuple[int, int] | None, timeout: float = 10.0) -> bool:
    """True when the Chromium sandbox can work for the (unprivileged) browser user in this container.

    Docker's default seccomp profile and HA's AppArmor usually forbid user namespaces, then this is False and
    the browser runs with ``--no-sandbox`` (still as the unprivileged user, with a clean environment)."""
    if not sys.platform.startswith("linux"):
        return False
    if not sysctl_allows_userns(_read("/proc/sys/user/max_user_namespaces"), _read("/proc/sys/kernel/unprivileged_userns_clone")):
        return False
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", USERNS_PROBE, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, env={"PATH": PATH_VALUE}, **{k: v for k, v in popen_drop_kwargs(ids).items()
                                                                                if k != "start_new_session"})
        try:
            return (await asyncio.wait_for(proc.wait(), timeout)) == 0
        except asyncio.TimeoutError:
            proc.kill()
            return False
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------- Chromium age
# The image's Chromium (Debian package) is fixed when the image is built, so it ages until the add-on is rebuilt
# or updated. The browser refuses to start when the installed major version is lower than this constant. Raise it with
# every add-on release (roughly: the major version that was current about a year ago).
MIN_CHROMIUM_MAJOR = 140


def parse_chromium_major(version_output: str | None) -> int | None:
    """Major version from the output of ``chromium --version`` (e.g. "Chromium 138.0.7204.100 built on Debian")."""
    import re

    match = re.search(r"\b(\d{2,3})\.\d+\.\d+\.\d+", version_output or "")
    return int(match.group(1)) if match else None


def chromium_version_problem(version_output: str | None, minimum: int = MIN_CHROMIUM_MAJOR) -> str | None:
    """Why this Chromium must not be started (too old), or None. An unreadable version is not a reason to refuse."""
    major = parse_chromium_major(version_output)
    if major is None:
        log.warning("browser: cannot read the Chromium version (%r); the age check was skipped", (version_output or "")[:80])
        return None
    if major < minimum:
        return (f"the installed Chromium {major} is older than the minimum {minimum} this add-on accepts: the image's "
                "Chromium was fixed when the image was built and has aged. Update or rebuild the add-on; "
                "the browser stays off until then")
    return None


async def read_chromium_version(binary: str, ids: tuple[int, int] | None, timeout: float = 15.0) -> str | None:
    """Output of ``binary --version`` (as the unprivileged user, clean environment), or None."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary, "--version", stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env={"PATH": PATH_VALUE, "HOME": "/nonexistent"}, cwd="/",
            **{k: v for k, v in popen_drop_kwargs(ids).items() if k != "start_new_session"})
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode("utf-8", "replace").strip()
    except (OSError, ValueError, asyncio.TimeoutError):
        return None


# ---------------------------------------------------------------- orphans
def find_user_pids(uid: int, proc_root: str = "/proc") -> list[int]:
    """Pids of every process that runs as ``uid`` (real uid), read from /proc."""
    pids = []
    try:
        names = os.listdir(proc_root)
    except OSError:
        return pids
    for name in names:
        if not name.isdigit():
            continue
        try:
            for line in Path(proc_root, name, "status").read_text().splitlines():
                if line.startswith("Uid:"):
                    if line.split()[1] == str(uid):
                        pids.append(int(name))
                    break
        except (OSError, IndexError, ValueError):
            continue
    return pids


KILL_ALL_CODE = "import os, signal; os.kill(-1, signal.SIGKILL)"


def kill_all_as_user(ids: tuple[int, int], timeout: float = 10.0) -> bool:
    """``kill(-1, SIGKILL)`` issued *as* the browser user: the kernel then signals every process that user owns in
    one atomic step, so a fork loop cannot keep ahead of a /proc scan. Done in a short child that has really
    switched to that uid (it can touch nothing else; ``-1`` never reaches processes of other users)."""
    if os.name != "posix" or ids[0] == 0 or ids[0] == os.getuid():
        return False
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-c", KILL_ALL_CODE], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env={"PATH": PATH_VALUE}, cwd="/", timeout=timeout,
            user=ids[0], group=ids[1], extra_groups=[])
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    # the helper kills itself too (SIGKILL), so a return code of -9 is the success case
    return proc.returncode in (0, -signal.SIGKILL)


def kill_user_processes(ids: tuple[int, int] | None, proc_root: str = "/proc", *, rounds: int = 50,
                        pause: float = 0.1) -> int:
    """SIGKILL every process of the browser user (orphaned Chromium helpers and Xvfb), again and again until the
    /proc scan finds none (a forking renderer outlives a single pass), at most ``rounds`` passes. Only when the
    browser runs as its own user, so the add-on never kills itself. Returns how many signals were sent."""
    if ids is None or os.name != "posix" or ids[0] == os.getuid():
        return 0
    killed = 0
    if proc_root == "/proc":
        kill_all_as_user(ids)  # atomic for the whole uid; the capped loop below stays as the backup
    for _ in range(max(1, rounds)):
        pids = find_user_pids(ids[0], proc_root)
        if not pids:
            return killed
        for pid in pids:
            try:
                os.kill(pid, signal.SIGKILL)
                killed += 1
            except (ProcessLookupError, PermissionError):
                pass
        time.sleep(pause)
    if find_user_pids(ids[0], proc_root):
        log.warning("browser: processes of the user '%s' are still running after %d kill rounds", BROWSER_USER, rounds)
    return killed


# ---------------------------------------------------------------- folders in a tree the browser user controls
# The add-on runs as root. Everything below the profile belongs to the browser user, who can replace any entry by a
# symbolic link at any time. Root therefore never uses a path inside such a tree: each folder is opened with
# O_NOFOLLOW|O_DIRECTORY relative to its (already opened) parent, and owner and mode are set on that descriptor.
_FD_OK = (os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY") and hasattr(os, "fchown")
          and os.open in os.supports_dir_fd and os.mkdir in os.supports_dir_fd and os.stat in os.supports_dir_fd)
_DIR_FLAGS = getattr(os, "O_RDONLY", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)


def _remove_entry(parent_fd: int, name: str) -> None:
    """Remove ``name`` (a link, a file or a whole folder) from the folder ``parent_fd`` without following a link."""
    info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if stat.S_ISDIR(info.st_mode):
        shutil.rmtree(name, dir_fd=parent_fd)
    else:
        os.unlink(name, dir_fd=parent_fd)


def open_private_dir(parent_fd: int, name: str, owner: tuple[int, int] | None, mode: int) -> int:
    """Open the folder ``name`` inside ``parent_fd`` (creating it; a link or a file in its place is removed first),
    never following a link, and give it ``owner`` and ``mode`` through the descriptor. Returns the new descriptor."""
    if not name or "/" in name or name in (".", ".."):
        raise ValueError(f"bad folder name {name!r}")
    for _ in range(3):
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            try:
                os.mkdir(name, 0o700, dir_fd=parent_fd)
            except FileExistsError:
                pass
        else:
            if not stat.S_ISDIR(info.st_mode):  # a link, a file ...: never the target of a link
                _remove_entry(parent_fd, name)
                continue
        try:
            fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
        except OSError as exc:  # ELOOP / ENOTDIR: swapped for a link in the meantime
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                try:
                    _remove_entry(parent_fd, name)
                except FileNotFoundError:
                    pass
                continue
            raise
        try:
            if not stat.S_ISDIR(os.fstat(fd).st_mode):
                raise NotADirectoryError(name)
            if owner is not None:
                os.fchown(fd, owner[0], owner[1])
            os.fchmod(fd, mode)
        except BaseException:
            os.close(fd)
            raise
        return fd
    raise OSError(f"could not create {name} safely")


def _fix_owner_tree(top_fd: int, ids: tuple[int, int]) -> None:
    """Hand files that an older run left behind (root-owned) to the browser user. Descriptor based and never
    following a link (``os.fwalk``); a file with more than one hard link is left alone."""
    for _root, dirs, files, root_fd in os.fwalk(".", dir_fd=top_fd, follow_symlinks=False):
        for entry in (*dirs, *files):
            try:
                info = os.stat(entry, dir_fd=root_fd, follow_symlinks=False)
                if info.st_uid != ids[0] and (stat.S_ISDIR(info.st_mode) or info.st_nlink == 1):
                    os.chown(entry, ids[0], ids[1], dir_fd=root_fd, follow_symlinks=False)
            except OSError:
                pass


def prepare_profile(profile: Path, ids: tuple[int, int] | None) -> None:
    """Create the profile folder (and its tmp/config/cache folders) owned by the browser user, mode 0700, without
    ever following a link the browser user planted. Blocking: run it off the loop.

    The parent of ``profile`` must be trusted (the add-on's /data); everything below is opened with O_NOFOLLOW."""
    if not _FD_OK:  # a development machine without these calls (Windows): nothing here is privileged
        profile.mkdir(parents=True, exist_ok=True)
        for sub in ("tmp", ".config", ".cache"):
            (profile / sub).mkdir(exist_ok=True)
        return
    profile.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(profile.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        profile_fd = open_private_dir(parent_fd, profile.name, ids, 0o700)
        try:
            for sub in ("tmp", ".config", ".cache"):
                os.close(open_private_dir(profile_fd, sub, ids, 0o700))
            if ids is not None:
                _fix_owner_tree(profile_fd, ids)
        finally:
            os.close(profile_fd)
    finally:
        os.close(parent_fd)


def prepare_incoming(root: Path, ids: tuple[int, int] | None, name: str = "dl") -> Path:
    """The download folder of the browser. ``root`` belongs to root (mode 0711: the browser user can enter it but
    neither list it nor replace anything in it) and holds one folder ``name`` that the browser user owns (0700).
    It lies outside the browser's profile and is emptied at every start. Returns ``root / name``. Blocking."""
    if not _FD_OK:
        shutil.rmtree(root, ignore_errors=True)
        (root / name).mkdir(parents=True)
        return root / name
    root.parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(root.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        root_fd = open_private_dir(parent_fd, root.name, (0, 0) if ids is not None else None, 0o711)
        try:
            for entry in os.listdir(root_fd):
                try:
                    _remove_entry(root_fd, entry)  # unfinished files of an earlier run
                except OSError:
                    log.warning("browser: could not remove %s from the download folder", entry)
            os.close(open_private_dir(root_fd, name, ids, 0o700))
        finally:
            os.close(root_fd)
    finally:
        os.close(parent_fd)
    return root / name


def read_small_file(path: Path, limit: int = 4096) -> str | None:
    """Read a small regular file that the browser user may have replaced, as root, without following a link."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_BINARY", 0))
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        return os.read(fd, limit).decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def harden_data_dir(data_dir: Path) -> None:
    """The browser user can enter /data (mode 0711: no listing) but must not read the add-on's own files."""
    try:
        os.chmod(data_dir, 0o711)
    except OSError:
        pass
    try:
        names = os.listdir(data_dir)
    except OSError:
        return
    for name in names:
        if name == "options.json" or name.startswith("car_media_hub.db"):
            try:
                os.chmod(data_dir / name, 0o600)
            except OSError:
                pass


def ensure_sticky_dir(path: str, mode: int = 0o1777) -> None:
    """Create ``path`` (when missing) and set its mode through a descriptor opened with O_NOFOLLOW, so a link planted
    by the browser user in a world-writable parent is never followed by root."""
    try:
        os.mkdir(path)
    except FileExistsError:
        pass
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise NotADirectoryError(path)
        os.fchmod(fd, mode)
    finally:
        os.close(fd)

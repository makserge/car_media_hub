"""Re-review 3 of B1: R1 (descriptor-based take-back), R2 (kill after every run), F1/F2 (modes, nested archives),
D1 (apt packages, no armv7), D2 (Chromium wrapper), D4 (no import in preexec_fn)."""
import inspect
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import pytest

from car_media_hub import archive as ar
from car_media_hub import browser as br
from car_media_hub import browser_proc as bp

ROOT = Path(__file__).resolve().parents[1]
posix = pytest.mark.skipif(os.name != "posix", reason="POSIX only")
is_root = hasattr(os, "geteuid") and os.geteuid() == 0


def _user(name):
    try:
        import pwd

        e = pwd.getpwnam(name)
        return e.pw_uid, e.pw_gid
    except (ImportError, KeyError):
        return None


needs_users = pytest.mark.skipif(not (is_root and _user("browser") and _user("extract")),
                                 reason="needs root and the users 'browser' and 'extract'")


def _mess(base: Path) -> Path:
    """A tree as a compromised extractor could leave it."""
    d = base / "content"
    (d / "a" / "b").mkdir(parents=True)
    (d / "a" / "song.mp3").write_bytes(b"x")
    (d / "a" / "b" / "deep.mp3").write_bytes(b"y")
    os.chmod(d / "a", 0o777)
    os.chmod(d / "a" / "b", 0o777)
    os.chmod(d / "a" / "song.mp3", 0o666)
    os.chmod(d / "a" / "b" / "deep.mp3", 0o4777)
    os.mkfifo(d / "a" / "pipe")
    os.symlink("/etc/passwd", d / "a" / "b" / "link")
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(d / "sock"))
    sock.close()
    return d


# ---------------------------------------------------------------- R1
@posix
def test_scrub_normalises_modes_and_deletes_everything_that_is_not_a_file_or_folder(tmp_path):
    d = _mess(tmp_path)
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        ar._scrub_dir(fd, (os.getuid(), os.getgid()), 1)
    finally:
        os.close(fd)
    assert stat.S_IMODE(os.stat(d / "a").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(d / "a" / "b").st_mode) == 0o755
    assert stat.S_IMODE(os.stat(d / "a" / "song.mp3").st_mode) == 0o644
    assert stat.S_IMODE(os.stat(d / "a" / "b" / "deep.mp3").st_mode) == 0o644  # also no setuid bit
    assert not os.path.lexists(d / "a" / "pipe") and not os.path.lexists(d / "a" / "b" / "link")
    assert not os.path.lexists(d / "sock")


@posix
def test_scrub_never_follows_a_link_and_leaves_its_target_alone(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    os.chmod(outside, 0o700)
    (outside / "f").write_text("keep")
    os.chmod(outside / "f", 0o600)
    d = tmp_path / "content"
    d.mkdir()
    os.symlink(outside, d / "dirlink")
    os.symlink(outside / "f", d / "filelink")
    fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
    try:
        ar._scrub_dir(fd, (os.getuid(), os.getgid()), 1)
    finally:
        os.close(fd)
    assert stat.S_IMODE(os.stat(outside).st_mode) == 0o700 and stat.S_IMODE(os.stat(outside / "f").st_mode) == 0o600
    assert (outside / "f").read_text() == "keep" and list(d.iterdir()) == []


@posix
def test_scrub_removes_extended_attributes_when_the_filesystem_has_them(tmp_path):
    if not hasattr(os, "setxattr"):
        pytest.skip("no xattr support")
    f = tmp_path / "f"
    f.write_text("x")
    try:
        os.setxattr(f, "user.cmh", b"1")
    except OSError:
        pytest.skip("filesystem without user xattrs")
    fd = os.open(f, os.O_RDONLY)
    try:
        ar._scrub_fd(fd, 0o644, (os.getuid(), os.getgid()))
        assert os.listxattr(fd) == []
    finally:
        os.close(fd)


@posix
def test_verify_tree_refuses_a_fifo(tmp_path):
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ar.ArchiveError, match="special file"):
        ar._verify_tree(tmp_path)


@needs_users
def test_reclaim_after_a_hostile_extractor_leaves_a_root_owned_normal_tree_and_nested_targets_work():
    ids = ar.extract_user_ids()
    base = Path(tempfile.mkdtemp(prefix="cmh-reclaim-"))
    umask_before = os.umask(0)
    os.umask(umask_before)
    try:
        os.chmod(base, 0o755)
        dest = base / "content"
        dest.mkdir()
        code = ("import os,socket,sys;d=sys.argv[1];os.makedirs(d+'/s/t');os.chmod(d+'/s',0o777);os.chmod(d+'/s/t',0o777);"
                "open(d+'/s/t/x.mp3','w').write('x');os.mkfifo(d+'/s/fifo');os.symlink('/etc/passwd',d+'/s/ln');"
                "s=socket.socket(socket.AF_UNIX);s.bind(d+'/sock')")
        budget = ar.ExtractionBudget(limit=10_000)
        old = ar._give_dest(dest, ids)
        ar._run_extractor([sys.executable, "-I", "-c", code, str(dest)], dest, budget, "fake", ids, ())
        ar._reclaim(dest, ids, old)
        for p in (dest, dest / "s", dest / "s" / "t", dest / "s" / "t" / "x.mp3"):
            assert os.lstat(p).st_uid == 0
        assert stat.S_IMODE(os.stat(dest / "s").st_mode) == 0o755 and stat.S_IMODE(os.stat(dest / "s" / "t").st_mode) == 0o755
        assert stat.S_IMODE(os.stat(dest / "s" / "t" / "x.mp3").st_mode) == 0o644
        assert not any(os.path.lexists(dest / n) for n in ("sock", "s/fifo", "s/ln"))
        ar._verify_tree(dest, budget)
        # F2: a nested archive's target inside such a subfolder is reachable for the extract user again
        nested = dest / "s" / "t" / "inner"
        nested.mkdir()
        ar._give_dest(nested, ids)
        assert os.umask(umask_before) == umask_before  # F1: the add-on's own umask was never touched
    finally:
        shutil.rmtree(base, ignore_errors=True)


@needs_users
def test_reclaim_strips_posix_acls():
    if shutil.which("setfacl") is None:
        pytest.skip("setfacl not installed")
    ids = ar.extract_user_ids()
    base = Path(tempfile.mkdtemp(prefix="cmh-acl-"))
    try:
        os.chmod(base, 0o755)
        dest = base / "content"
        dest.mkdir()
        old = ar._give_dest(dest, ids)
        (dest / "d").mkdir()
        probe = subprocess.run(["setfacl", "-m", "u:1010:rwx", str(dest / "d")], capture_output=True)
        if probe.returncode != 0:
            pytest.skip("filesystem without ACLs")
        ar._reclaim(dest, ids, old)
        assert "1010" not in subprocess.run(["getfacl", "-p", str(dest / "d")], capture_output=True, text=True).stdout
    finally:
        shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------- R2
def test_every_run_of_7z_and_bsdtar_kills_the_extract_user_even_when_it_fails(monkeypatch):
    killed = []
    monkeypatch.setattr(ar, "kill_user_processes", lambda ids: killed.append(ids))
    monkeypatch.setattr(ar, "_sevenzip_exe", lambda: sys.executable)
    monkeypatch.setattr(ar, "_bsdtar_exe", lambda: sys.executable)
    ids = (1011, 1011)
    monkeypatch.setattr(ar, "extractor_kwargs", lambda i, f=(), fsize=0: {})
    ar._run_7z(["-c", "pass"], ids)
    ar._run_bsdtar(["-c", "pass"], ids)
    with pytest.raises(ar.ArchiveError):
        ar._run_7z(["-c", "import sys;sys.exit(2)"], ids)
    with pytest.raises(ar.ArchiveError):
        ar._run_bsdtar(["-c", "import sys;sys.exit(2)"], ids)
    assert killed == [ids] * 4


def test_run_extractor_kills_the_extract_user_after_success_and_failure(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(ar, "kill_user_processes", lambda ids: killed.append(ids))
    monkeypatch.setattr(ar, "extractor_kwargs", lambda i, f=(), fsize=0: {})
    ids = (1011, 1011)
    budget = ar.ExtractionBudget(limit=10_000)
    ar._run_extractor([sys.executable, "-c", "pass"], tmp_path, budget, "fake", ids)
    with pytest.raises(ar.ArchiveError):
        ar._run_extractor([sys.executable, "-c", "import sys;sys.exit(3)"], tmp_path, budget, "fake", ids)
    assert killed == [ids, ids]


def test_kill_extractor_leftovers_runs_at_start_and_never_raises(monkeypatch):
    killed = []
    monkeypatch.setattr(ar, "extract_user_ids", lambda: (1011, 1011))
    monkeypatch.setattr(ar, "kill_user_processes", lambda ids: killed.append(ids))
    ar.kill_extractor_leftovers()
    assert killed == [(1011, 1011)]
    monkeypatch.setattr(ar, "extract_user_ids", lambda: (_ for _ in ()).throw(ar.ArchiveError("no user")))
    ar.kill_extractor_leftovers()
    assert "kill_extractor_leftovers()" in (ROOT / "app" / "car_media_hub" / "main.py").read_text()


@needs_users
def test_a_daemon_left_by_a_lister_is_killed_after_the_run(monkeypatch):
    ids = ar.extract_user_ids()
    monkeypatch.setattr(ar, "_sevenzip_exe", lambda: sys.executable)
    code = ("import os,sys,time\nif os.fork()==0:\n    os.setsid()\n    os.close(1);os.close(2)\n    time.sleep(120);os._exit(0)\n")
    ar._run_7z(["-I", "-c", code], ids)
    time.sleep(0.3)
    assert bp.find_user_pids(ids[0]) == []


# ---------------------------------------------------------------- F2 (zip outer archive, depth 2) / F1 umask
def test_nested_archive_at_depth_two_is_extracted(tmp_path):
    inner = tmp_path / "inner.zip"
    with zipfile.ZipFile(inner, "w") as zf:
        zf.writestr("t.mp3", b"abc")
    outer = tmp_path / "outer.zip"
    with zipfile.ZipFile(outer, "w") as zf:
        zf.write(inner, "sub/deeper/inner.zip")
    dest = tmp_path / "out"
    ar.safe_extract(outer, dest)
    assert ar.extract_nested(dest) == 1
    assert (dest / "sub" / "deeper" / "inner" / "t.mp3").read_bytes() == b"abc"


@posix
def test_extractor_umask_is_only_passed_to_the_child():
    kw = ar.extractor_kwargs((1011, 1011))
    assert kw["umask"] == 0o077
    assert "umask" not in ar.extractor_kwargs(None)
    source = inspect.getsource(ar)
    assert "os.umask(" not in source  # the add-on never changes its own umask


# ---------------------------------------------------------------- D1
def test_image_uses_apt_python_packages_and_no_armv7():
    docker = (ROOT / "Dockerfile").read_text()
    assert "python3-aiohttp" in docker and "python3-mutagen" in docker
    assert "pip" not in docker.replace("# python3-aiohttp / python3-mutagen come from apt (no pip", "")
    for name in ("config.yaml", "build.yaml"):
        text = "\n".join(line for line in (ROOT / name).read_text().splitlines() if not line.lstrip().startswith("#"))
        assert "armv7" not in text, name
    assert "armv7" in (ROOT / "DOCS.md").read_text()


# ---------------------------------------------------------------- D2
def test_the_real_chromium_binary_is_preferred_over_the_wrapper(monkeypatch):
    monkeypatch.delenv("CAR_MEDIA_HUB_CHROMIUM", raising=False)
    monkeypatch.setattr(br, "REAL_CHROMIUM", sys.executable)
    assert br.find_binary() == sys.executable


def test_wrapper_scripts_get_a_root_owned_home(tmp_path):
    script = tmp_path / "chromium"
    script.write_text("#!/bin/sh\nexec true\n")
    binary = tmp_path / "real"
    binary.write_bytes(b"\x7fELF")
    assert bp.is_wrapper_script(str(script)) and not bp.is_wrapper_script(str(binary))
    assert not bp.is_wrapper_script(str(tmp_path / "missing"))
    assert bp.build_browser_env("/data/p", wrapper=True)["HOME"] == "/nonexistent"
    assert bp.build_browser_env("/data/p")["HOME"] == "/data/p"
    assert "CHROMIUM_FLAGS" not in bp.build_browser_env("/data/p", wrapper=True)


# ---------------------------------------------------------------- D4
def test_no_import_inside_preexec_functions():
    for func in (ar._limits_fn(1 << 20), bp._limit_nproc):
        code = func.__code__
        assert "IMPORT_NAME" not in [i.opname for i in __import__("dis").get_instructions(code)]
    if os.name == "posix":
        assert ar.resource is not None and bp.resource is not None

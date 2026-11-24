"""Re-review 2 of B1: Dockerfile (N1), archive extraction privilege and limits (N3), kill by uid (M5), Chromium age (N2),
/tmp/.X11-unix (N6)."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
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


# ---------------------------------------------------------------- N1: Dockerfile
def dockerfile_problems(text: str) -> list[str]:
    """Parse the Dockerfile the way the builder does: continuation lines are joined, every other line must start with an
    instruction, and the quotes of an instruction must be balanced (a quote cannot span a real line break)."""
    known = {"ARG", "FROM", "RUN", "COPY", "ADD", "ENV", "CMD", "ENTRYPOINT", "WORKDIR", "USER", "EXPOSE", "LABEL", "VOLUME",
             "SHELL", "HEALTHCHECK", "STOPSIGNAL", "ONBUILD"}
    problems, logical, cont = [], [], False
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not cont and (not line.strip() or line.lstrip().startswith("#")):
            continue
        if cont and line.lstrip().startswith("#"):
            continue
        if not cont and line.split()[0].upper() not in known:
            problems.append(f"line {number}: not an instruction: {line[:40]!r}")
        cont = line.endswith("\\")
        logical.append(line[:-1] if cont else line)
        if not cont:
            joined = " ".join(logical)
            logical = []
            if joined.split()[0].upper() == "RUN":
                if joined.count("'") % 2 or joined.count('"') % 2:
                    problems.append(f"line {number}: unbalanced quote in {joined[:60]!r}")
    return problems


def test_dockerfile_parses_and_has_no_quote_spanning_a_line_break():
    assert dockerfile_problems((ROOT / "Dockerfile").read_text()) == []


def test_the_checker_rejects_the_n1_breakage():
    broken = ("FROM x\nRUN mkdir -p /etc/chromium/policies/managed  && printf '{\"WebRtcIPHandlingPolicy\": \"disable_non_proxied_udp\"}\n"
              "' > /etc/chromium/policies/managed/cmh.json\n")
    assert dockerfile_problems(broken)


def test_policy_file_is_valid_json_and_copied_into_the_image():
    data = json.loads((ROOT / "policy" / "cmh.json").read_text())
    assert data == {"WebRtcIPHandlingPolicy": bp.WEBRTC_POLICY == "disable_non_proxied_udp" and "disable_non_proxied_udp"}
    docker = (ROOT / "Dockerfile").read_text()
    assert "COPY policy/cmh.json /etc/chromium/policies/managed/cmh.json" in docker


def test_dockerfile_is_debian_with_both_unprivileged_users():
    docker = (ROOT / "Dockerfile").read_text()
    assert "apk " not in docker and "adduser" not in docker
    assert "apt-get install -y --no-install-recommends" in docker and "chromium" in docker and "xvfb" in docker
    assert "rm -rf /var/lib/apt/lists" in docker
    assert "-u 1010" in docker and "-u 1011" in docker and docker.count("/usr/sbin/nologin") == 2
    assert bp.BROWSER_USER == "browser" and ar.EXTRACT_USER == "extract"
    assert "browser" in docker and "extract" in docker
    assert "__pycache__" in (ROOT / ".dockerignore").read_text()


# ---------------------------------------------------------------- N3: limits of the extraction
def make_zip(path, members):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            zf.writestr(name, data)
    return path


def test_zip_bomb_is_refused_by_the_declared_size(tmp_path):
    z = make_zip(tmp_path / "bomb.zip", [("a.flac", b"\0" * (4 * 1024 * 1024))])
    assert z.stat().st_size < 100_000  # tiny on disk, big when unpacked
    with pytest.raises(ar.ArchiveError):
        ar.safe_extract(z, tmp_path / "out", ar.ExtractionBudget(limit=1024 * 1024))
    assert not any((tmp_path / "out").rglob("a.flac")) if (tmp_path / "out").exists() else True


def test_zip_bomb_that_lies_about_its_size_is_stopped_while_copying(tmp_path):
    z = make_zip(tmp_path / "x.zip", [("a.bin", b"\0" * (3 * 1024 * 1024))])
    budget = ar.ExtractionBudget(limit=1024 * 1024)
    budget.check_declared = lambda declared: None  # the directory is not trusted: the copy loop counts the real bytes
    with pytest.raises(ar.ArchiveError):
        ar.safe_extract(z, tmp_path / "out", budget)


@pytest.mark.parametrize("name", ["../evil.mp3", "a/../../evil.mp3", "/abs/evil.mp3", "C:/evil.mp3", "a\\..\\..\\evil.mp3"])
def test_traversal_names_are_refused_in_zip_and_tar(tmp_path, name):
    z = make_zip(tmp_path / "t.zip", [(name, b"x")])
    with pytest.raises(ar.ArchiveError):
        ar.safe_extract(z, tmp_path / "o1")
    t = tmp_path / "t.tar"
    with tarfile.open(t, "w") as tf:
        info = tarfile.TarInfo(name)
        info.size = 1
        tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ar.ArchiveError):
        ar.safe_extract(t, tmp_path / "o2")
    assert not (tmp_path / "evil.mp3").exists()


def test_symlink_hardlink_and_device_members_are_refused(tmp_path):
    zp = tmp_path / "l.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        info = zipfile.ZipInfo("link.mp3")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(ar.ArchiveError):
        ar.safe_extract(zp, tmp_path / "o1")
    for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE, tarfile.FIFOTYPE):
        tp = tmp_path / f"k{kind.decode()}.tar"
        with tarfile.open(tp, "w") as tf:
            info = tarfile.TarInfo("x")
            info.type = kind
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        with pytest.raises(ar.ArchiveError):
            ar.safe_extract(tp, tmp_path / f"o{kind.decode()}")


def test_entry_count_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(ar, "MAX_ENTRIES", 3)
    z = make_zip(tmp_path / "many.zip", [(f"f{i}.txt", b"x") for i in range(10)])
    with pytest.raises(ar.ArchiveError, match="entries"):
        ar.safe_extract(z, tmp_path / "out")


def test_listing_of_7z_and_bsdtar_rejects_links_and_traversal(tmp_path, monkeypatch):
    budget = ar.ExtractionBudget(limit=10_000)
    arch = tmp_path / "a.7z"
    arch.write_bytes(b"x")
    listing = "Path = ../x\nSize = 1\n\n"
    monkeypatch.setattr(ar, "_run_7z", lambda args, ids=None, fds=(): listing)
    with pytest.raises(ar.ArchiveError):
        ar._validate_7z_listing(arch, budget)
    monkeypatch.setattr(ar, "_run_7z", lambda args, ids=None, fds=(): "Path = a\nSymbolic Link = b\nSize = 1\n\n")
    with pytest.raises(ar.ArchiveError):
        ar._validate_7z_listing(arch, budget)
    monkeypatch.setattr(ar, "_run_7z", lambda args, ids=None, fds=(): "Path = a\nSize = 99999999\n\n")
    with pytest.raises(ar.ArchiveError):
        ar._validate_7z_listing(arch, budget)
    monkeypatch.setattr(ar, "_run_bsdtar", lambda args, ids=None, fds=(): "lrwxrwxrwx  1 0 0 0 Jan  1 2020 evil -> /etc\n")
    with pytest.raises(ar.ArchiveError):
        ar._validate_bsdtar_listing(arch, budget)


def test_external_extractors_get_a_clean_environment_and_no_stdin(tmp_path, monkeypatch):
    monkeypatch.setenv("SUPERVISOR_TOKEN", "secret-token")
    seen = []

    def fake_run(cmd, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    monkeypatch.setattr(ar, "_sevenzip_exe", lambda: sys.executable)
    monkeypatch.setattr(ar, "_bsdtar_exe", lambda: sys.executable)
    ar._run_7z(["l"])
    ar._run_bsdtar(["-t"])
    assert len(seen) == 2
    for kw in seen:
        assert kw["env"] == ar.EXTRACT_ENV and "SUPERVISOR_TOKEN" not in kw["env"]
        assert kw["stdin"] == subprocess.DEVNULL
        if os.name == "posix":
            assert kw["start_new_session"] and kw["cwd"] == "/" and callable(kw["preexec_fn"])


@posix
def test_extractor_kwargs_drop_privileges_and_cap_resources():
    kw = ar.extractor_kwargs((1011, 1011), (5,), fsize=123 << 20)
    assert kw["user"] == 1011 and kw["group"] == 1011 and kw["extra_groups"] == [] and kw["pass_fds"] == (5,)
    assert "user" not in ar.extractor_kwargs(None)


@posix
def test_limits_are_applied_in_the_child():
    code = "import resource as r;print(*[r.getrlimit(getattr(r,n))[0] for n in ('RLIMIT_AS','RLIMIT_FSIZE','RLIMIT_NOFILE','RLIMIT_NPROC','RLIMIT_CORE')])"
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         **ar.extractor_kwargs(None, (), fsize=5 << 20)).stdout.split()
    assert [int(x) for x in out] == [ar.EXTRACT_AS_BYTES, 5 << 20, ar.EXTRACT_NOFILE, ar.EXTRACT_NPROC, 0]


@needs_users
def test_extractor_runs_as_the_extract_user_on_a_folder_it_owns_and_root_takes_it_back():
    ids = ar.extract_user_ids()
    base = Path(tempfile.mkdtemp(prefix="cmh-extract-"))
    try:
        os.chmod(base, 0o755)
        dest = base / "content"
        dest.mkdir()
        archive = base / "a.bin"
        archive.write_bytes(b"payload")
        os.chmod(archive, 0o600)  # root only: the descriptor hand-over makes it readable for the extract user
        code = ("import os,sys;d=sys.argv[1];open(d+'/f','w').write(str(os.getuid())+' '+repr(os.environ.get('SUPERVISOR_TOKEN')));"
                "os.symlink('/etc/passwd',d+'/ln');print(open(sys.argv[2]).read())")
        budget = ar.ExtractionBudget(limit=10_000)
        with ar._opened_archive(archive) as (arg, ids2, fds):
            assert ids2 == ids
            old = ar._give_dest(dest, ids)
            assert os.stat(dest).st_uid == ids[0]
            os.environ["SUPERVISOR_TOKEN"] = "must-not-leak"
            try:
                text = ar._run_extractor([sys.executable, "-I", "-c", code, str(dest), arg], dest, budget, "fake", ids, fds)
            finally:
                del os.environ["SUPERVISOR_TOKEN"]
                ar._reclaim(dest, ids, old)
        assert text == "payload"
        assert (dest / "f").read_text() == f"{ids[0]} None"
        assert os.stat(dest).st_uid == 0 and os.lstat(dest / "f").st_uid == 0 and stat.S_IMODE(os.stat(dest).st_mode) == old
        ar._verify_tree(dest, budget)
        assert not (dest / "ln").exists() and not (dest / "ln").is_symlink()  # links are removed afterwards
    finally:
        shutil.rmtree(base, ignore_errors=True)


@needs_users
def test_extraction_target_not_reachable_for_the_extract_user_is_refused():
    ids = ar.extract_user_ids()
    base = Path(tempfile.mkdtemp(prefix="cmh-extract-"))
    try:
        os.chmod(base, 0o700)
        (base / "d").mkdir()
        with pytest.raises(ar.ArchiveError, match="not reachable"):
            ar._give_dest(base / "d", ids)
    finally:
        shutil.rmtree(base, ignore_errors=True)


@pytest.mark.skipif(not is_root or _user("extract") is not None, reason="only when the user is missing (root)")
def test_missing_extract_user_means_no_extraction_as_root():
    with pytest.raises(ar.ArchiveError, match="extract"):
        ar.extract_user_ids()


# ---------------------------------------------------------------- M5: kill by uid, RLIMIT_NPROC
def test_browser_processes_get_an_nproc_limit():
    kw = bp.popen_drop_kwargs((1010, 1010))
    assert kw["preexec_fn"] is bp._limit_nproc and bp.NPROC_LIMIT >= 256
    assert "preexec_fn" not in bp.popen_drop_kwargs(None)


@posix
def test_kill_all_helper_runs_as_the_browser_uid_and_is_guarded(monkeypatch):
    calls = []
    monkeypatch.setattr(bp.subprocess, "run",
                        lambda cmd, **kw: calls.append((cmd, kw)) or subprocess.CompletedProcess(cmd, -9))
    assert bp.kill_all_as_user((0, 0)) is False  # never as root
    assert bp.kill_all_as_user((os.getuid(), 0)) is False  # never as ourselves
    assert not calls
    assert "kill(-1" in bp.KILL_ALL_CODE and "SIGKILL" in bp.KILL_ALL_CODE


@posix
def test_kill_user_processes_calls_the_uid_wide_kill_first(monkeypatch):
    order = []
    monkeypatch.setattr(bp, "kill_all_as_user", lambda ids: order.append("all"))
    monkeypatch.setattr(bp, "find_user_pids", lambda uid, root="/proc": order.append("scan") or [])
    assert bp.kill_user_processes((4242, 4242)) == 0
    assert order == ["all", "scan"]


@needs_users
def test_kill_all_as_user_kills_a_forking_survivor_in_one_step():
    ids = bp.unprivileged_user()
    code = ("import os,time\nfor _ in range(6):\n    if os.fork()==0:\n        time.sleep(60);os._exit(0)\ntime.sleep(60)")
    subprocess.Popen([sys.executable, "-I", "-c", code], user=ids[0], group=ids[1], extra_groups=[], start_new_session=True)
    for _ in range(50):
        if len(bp.find_user_pids(ids[0])) >= 7:
            break
        import time

        time.sleep(0.1)
    assert len(bp.find_user_pids(ids[0])) >= 7
    assert bp.kill_all_as_user(ids) is True
    import time

    time.sleep(0.3)
    left = bp.find_user_pids(ids[0])
    # zombies of the Popen above still show up until reaped: they have no cmdline
    assert all(Path(f"/proc/{p}/cmdline").read_bytes() == b"" for p in left)


# ---------------------------------------------------------------- N2: Chromium age
def test_min_chromium_major_is_a_documented_constant():
    assert isinstance(bp.MIN_CHROMIUM_MAJOR, int) and bp.MIN_CHROMIUM_MAJOR >= 100
    assert "MIN_CHROMIUM_MAJOR" in (ROOT / "DOCS.md").read_text()


@pytest.mark.parametrize("text,major", [("Chromium 138.0.7204.100 built on Debian GNU/Linux 13", 138),
                                        ("Chromium 99.0.4844.84 Alpine Linux", 99), ("Google Chrome 150.0.1.2 ", 150),
                                        ("", None), (None, None), ("no version here", None)])
def test_parse_chromium_major(text, major):
    assert bp.parse_chromium_major(text) == major


def test_old_chromium_is_refused_and_unknown_is_not():
    old = f"Chromium {bp.MIN_CHROMIUM_MAJOR - 1}.0.1.2 built on Debian"
    assert "older than the minimum" in bp.chromium_version_problem(old)
    assert bp.chromium_version_problem(f"Chromium {bp.MIN_CHROMIUM_MAJOR}.0.1.2") is None
    assert bp.chromium_version_problem("garbage") is None
    assert bp.chromium_version_problem("Chromium 120.0.1.2", minimum=121) and bp.chromium_version_problem("Chromium 121.0.1.2", minimum=121) is None


async def test_browser_refuses_to_start_when_chromium_is_too_old(tmp_path, monkeypatch, caplog):
    from test_browser_downloads import make

    browser, cfg, _ = make(tmp_path)
    monkeypatch.setattr(br, "find_binary", lambda: "/usr/bin/chromium")
    monkeypatch.setattr(br, "unprivileged_user", lambda: None)

    async def version(binary, ids, timeout=15.0):
        return "Chromium 90.0.1.2 built on Debian"

    async def no_probe(*a, **k):
        raise AssertionError("must stop before the sandbox probe")

    monkeypatch.setattr(br, "read_chromium_version", version)
    monkeypatch.setattr(br, "detect_sandbox_support", no_probe)
    with caplog.at_level("WARNING"):
        with pytest.raises(br.BrowserError, match="older than the minimum"):
            await browser.ensure_started()
    assert "REFUSING TO START" in caplog.text
    assert not cfg.browser_profile_dir.exists()


def test_docs_and_option_texts_state_the_chromium_age_and_haos_caveat():
    yaml = pytest.importorskip("yaml")
    docs = (ROOT / "DOCS.md").read_text(encoding="utf-8")
    assert "fixed when the image is built" in docs and "Home Assistant OS" in docs and "unavailable" in docs
    for lang, words in (("en", ("current", "Home Assistant OS", "unavailable")), ("de", ("aktuell", "Home Assistant OS", "nicht verfügbar"))):
        text = yaml.safe_load((ROOT / "translations" / f"{lang}.yaml").read_text(encoding="utf-8"))["configuration"]
        desc = text["browser_enabled"]["description"]
        assert all(w in desc for w in words), lang


# ---------------------------------------------------------------- N6
@posix
def test_ensure_sticky_dir_sets_the_mode_and_never_follows_a_link(tmp_path):
    target = tmp_path / ".X11-unix"
    bp.ensure_sticky_dir(str(target))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o1777
    victim = tmp_path / "victim"
    victim.mkdir()
    os.chmod(victim, 0o700)
    link = tmp_path / "linked"
    os.symlink(victim, link)
    with pytest.raises(OSError):
        bp.ensure_sticky_dir(str(link))
    assert stat.S_IMODE(os.stat(victim).st_mode) == 0o700

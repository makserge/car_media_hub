import os
import shutil
from pathlib import Path

import pytest

from car_media_hub import browser_proc as bp


def test_env_is_an_explicit_minimal_dict_without_secrets(monkeypatch):
    for key in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN", "HOMEASSISTANT_TOKEN", "SYNC_TOKEN", "HA_TOKEN"):
        monkeypatch.setenv(key, "very-secret")
    env = bp.build_browser_env("/data/browser-profile", ":99")
    assert set(env) <= bp.ENV_ALLOWED
    assert env["HOME"] == "/data/browser-profile" and env["DISPLAY"] == ":99"
    assert "very-secret" not in " ".join(env.values())
    assert not any("TOKEN" in k or "SUPERVISOR" in k for k in env)
    assert "DISPLAY" not in bp.build_browser_env("/p")


@pytest.mark.parametrize("key", ["SUPERVISOR_TOKEN", "HASSIO_TOKEN", "SYNC_TOKEN", "MY_PASSWORD", "FOO", "PYTHONPATH", "HA_URL"])
def test_assert_env_clean_rejects_anything_else(key):
    with pytest.raises(ValueError):
        bp.assert_env_clean({"PATH": "/bin", key: "x"})


def test_assert_env_clean_accepts_the_built_env():
    bp.assert_env_clean(bp.build_browser_env("/p", ":1"))


def args(**kw):
    base = dict(headless=False, sandbox=True, proxy_port=4321, window=(2560, 1600))
    base.update(kw)
    return bp.chromium_args("/usr/bin/chromium", "/data/browser-profile", **base)


def test_sandbox_stays_on_when_supported():
    assert "--no-sandbox" not in args(sandbox=True)


def test_no_sandbox_only_when_not_supported():
    assert "--no-sandbox" in args(sandbox=False)


def test_all_traffic_goes_through_the_proxy_and_nothing_resolves_in_chromium():
    a = args()
    assert "--proxy-server=http://127.0.0.1:4321" in a
    assert "--proxy-bypass-list=<-loopback>" in a  # localhost must not bypass the proxy
    assert any(x.startswith("--host-resolver-rules=MAP * ~NOTFOUND") for x in a)
    assert "--remote-debugging-port=0" in a  # DevTools only on loopback, random port


def test_resource_limits_are_set():
    a = args()
    assert f"--renderer-process-limit={bp.RENDERER_LIMIT}" in a
    assert any(x.startswith("--js-flags=--max-old-space-size=") for x in a)
    assert "--headless=new" not in a and "--headless=new" in args(headless=True)


def test_sysctl_parsing():
    assert bp.sysctl_allows_userns(None, None)
    assert bp.sysctl_allows_userns("15000\n", "1\n")
    assert not bp.sysctl_allows_userns("0\n", None)
    assert not bp.sysctl_allows_userns("15000", "0")
    assert not bp.sysctl_allows_userns("garbage", None)


async def test_sandbox_is_never_assumed_without_linux_userns(monkeypatch):
    monkeypatch.setattr(bp, "_read", lambda path: "0" if "max_user_namespaces" in path else None)
    monkeypatch.setattr(bp.sys, "platform", "linux")
    assert await bp.detect_sandbox_support(None) is False
    monkeypatch.setattr(bp.sys, "platform", "win32")
    assert await bp.detect_sandbox_support(None) is False


def test_privilege_drop_kwargs():
    assert bp.popen_drop_kwargs(None) == {"start_new_session": True}
    kw = bp.popen_drop_kwargs((1010, 1010))
    assert kw["user"] == 1010 and kw["group"] == 1010 and kw["extra_groups"] == [] and kw["start_new_session"]
    assert kw["umask"] == 0o077


def test_unprivileged_user_is_none_when_not_root():
    if os.name == "posix" and os.geteuid() == 0:
        pytest.skip("running as root")
    assert bp.unprivileged_user() is None


def fake_proc(tmp_path: Path):
    for pid, uid in ((100, 1010), (101, 0), (102, 1010), (103, 1000)):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "status").write_text(f"Name:\tx\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\nGid:\t0\t0\t0\t0\n")
    (tmp_path / "self").mkdir()
    (tmp_path / "cpuinfo").write_text("x")
    return str(tmp_path)


def test_find_user_pids_reads_proc(tmp_path):
    assert sorted(bp.find_user_pids(1010, fake_proc(tmp_path))) == [100, 102]
    assert bp.find_user_pids(5, str(tmp_path / "missing")) == []


def test_kill_user_processes_never_runs_without_a_separate_user(tmp_path, monkeypatch):
    killed = []
    root = fake_proc(tmp_path)

    def fake_kill(pid, sig):
        killed.append(pid)
        shutil.rmtree(Path(root) / str(pid))  # the process is gone

    monkeypatch.setattr(bp.os, "kill", fake_kill)
    assert bp.kill_user_processes(None, root) == 0 and killed == []
    if os.name == "posix":
        assert bp.kill_user_processes((1010, 1010), root, pause=0) == 2
        assert sorted(killed) == [100, 102]


def test_harden_data_dir_only_touches_own_files(tmp_path):
    (tmp_path / "options.json").write_text("{}")
    (tmp_path / "car_media_hub.db").write_text("x")
    (tmp_path / "other.txt").write_text("x")
    bp.harden_data_dir(tmp_path)  # must not raise, also on Windows
    if os.name == "posix":
        assert (tmp_path / "options.json").stat().st_mode & 0o077 == 0
        assert (tmp_path / "other.txt").stat().st_mode & 0o044 != 0


def test_prepare_profile_creates_private_folders(tmp_path):
    profile = tmp_path / "browser-profile"
    bp.prepare_profile(profile, None)
    for sub in ("tmp", ".config", ".cache"):
        assert (profile / sub).is_dir()
    if os.name == "posix":
        assert profile.stat().st_mode & 0o077 == 0

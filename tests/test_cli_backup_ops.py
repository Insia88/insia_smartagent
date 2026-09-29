"""``insia backup`` folder permissions and the user docs that go with backup, Docker and cron (offline, fast).

Regression tests for the final review's docs/Docker/backup findings:

- the backup folder follows the umask like the workspace (a 0700 folder written by the container's uid 10001
  locked the host user out of the Docker backup they must carry to USB);
- docs/operations.md stores the Instagram switch and media settings where the terminal ``insia publish``
  commands and the weekly ``publish refresh`` cron read them (they read only the environment, never
  ``serve --media-*``), and the documented values really turn Instagram on;
- the Docker steps exchange files with ``docker compose cp`` and keep cron logs out of the container-owned
  ``workspace/`` (a failed ``>>`` redirect means the command never runs);
- the shortened-id example is a real part of a real item id;
- the ``.env`` file the Docker setup asks for is git-ignored.

Follow-ups from the fix verification:

- configuration B (dashboard already on a public domain) saves ``INSIA_MEDIA_BASE_URL`` + ``INSIA_PUBLIC_HOSTS`` and
  never ``INSIA_MEDIA_PORT`` (a media port silently switches to the media-only listener, and the dashboard port then
  stops serving ``/pub/m/``); a server and the terminal started from the documented environment agree on it;
- §12-4 step 5 names the states the CLI really shows for each half-saved setup;
- the Docker (Linux) restore puts the files back with ``sudo`` and hands them back to the container user.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from insia_agents import cli
from insia_agents.cli import main
from insia_agents.config import Settings
from insia_agents.db import Workspace, pipeline_item_id
from insia_agents.models import PublishConnection
from insia_agents.pipeline import new_run_id
from insia_agents.publishers.media import PublicMediaHost
from insia_agents.publishers.settings import PublishSettings
from insia_agents.publishers.store import CredentialStore
from insia_agents.server import make_server

pytestmark = pytest.mark.usefixtures("no_network")  # loopback only (tests/conftest.py)

ROOT = Path(__file__).resolve().parents[1]
OPS = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
COMPOSE = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
IG_VARS = {"INSIA_PUBLISH_INSTAGRAM", "INSIA_MEDIA_PORT", "INSIA_MEDIA_BASE_URL"}
CONFIG_B_VARS = {"INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_PUBLISH_INSTAGRAM",
                 "INSIA_MEDIA_BASE_URL"}
SERVER_ENV = ("INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY", "INSIA_PORT")  # not in PUBLISH_ENV_VARS


def run(capsys, *argv):
    code = main([str(a) for a in argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "ws"
    monkeypatch.setenv("INSIA_HOME", str(path))
    monkeypatch.chdir(tmp_path)
    return path


def section(heading: str) -> str:
    """The text of one ``##``/``###`` section of docs/operations.md (up to the next heading of the same level)."""
    level = heading.split(" ", 1)[0]
    start = OPS.index(heading + "\n") if (heading + "\n") in OPS else OPS.index(heading)
    rest = OPS[start + len(heading):]
    stop = re.search(rf"^{level} ", rest, re.M)
    return rest[: stop.start()] if stop else rest


def code_blocks(text: str) -> list[tuple[str, str]]:
    return [(lang, body) for lang, body in re.findall(r"```(\w*)\n(.*?)```", text, re.S)]


def exports(text: str) -> dict[str, str]:
    """``export NAME=VALUE`` lines that are not commented out (quotes stripped)."""
    found = {}
    for line in text.splitlines():
        match = re.match(r"\s*export\s+([A-Z_]+)=(.*)$", line)
        if match:
            found[match.group(1)] = match.group(2).strip().strip('"').strip("'")
    return found


# ---------------------------------------------------------------------------
# backup folder permissions (finding 10)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
@pytest.mark.parametrize("umask, expected", [(0o022, 0o755), (0o077, 0o700)])
def test_backup_folder_follows_the_umask_so_the_host_user_can_copy_it(home, tmp_path, capsys, umask, expected):
    ws = Workspace(home)
    ws.close()
    (home / "uploads" / "회사소개서.txt").write_text("자료", encoding="utf-8")
    out_dir = tmp_path / "backups" / "2026-09-28"
    old = os.umask(umask)
    try:
        code, out, err = run(capsys, "backup", "--out", out_dir, "--json")
    finally:
        os.umask(old)
    assert code == 0, err
    assert json.loads(out)["ok"] is True
    # Like the workspace folder itself: with the usual umask 022 the Docker host user (another uid than the
    # container's 10001) can list and copy the backup; an owner-only umask still gives an owner-only folder.
    assert stat.S_IMODE(out_dir.stat().st_mode) == expected
    if umask == 0o022:
        assert all(p.stat().st_mode & stat.S_IROTH for p in out_dir.rglob("*"))


# ---------------------------------------------------------------------------
# Instagram settings for terminal commands and the refresh cron (findings 7 and 8)
# ---------------------------------------------------------------------------


def test_ops_docs_save_the_instagram_settings_where_terminal_commands_and_cron_read_them(tmp_path):
    ig = section("### 12-4. 인스타그램 연결하기 (시험 중)")
    # macOS/Linux: appended to ~/.insia.env (the file the cron sources) and loaded by new terminals
    [saved] = [body for lang, body in code_blocks(ig) if "cat >> ~/.insia.env" in body]
    saved_vars = exports(saved)
    assert set(saved_vars) == IG_VARS
    assert re.search(r'echo \'\. "\$HOME/\.insia\.env"\' >> ~/\.zshrc', saved)
    # Windows: setx for all three (persistent); Docker: .env
    [setx] = [body for lang, body in code_blocks(ig) if lang == "powershell" and "setx" in body]
    assert {m for m in re.findall(r"setx\s+([A-Z_]+)", setx)} == IG_VARS
    assert all(f"{name}=" in ig for name in IG_VARS)
    # the documented values really turn Instagram on with a valid media-only listener (what the CLI reads)
    settings = PublishSettings.from_env(saved_vars, tmp_path)
    assert settings.instagram_enabled and settings.instagram_enabled_by == "env"
    assert settings.media_mode == "listener" and settings.media_valid and settings.media_port == 8766

    # Nowhere is Instagram switched on only inline for one command, or the media only as serve flags
    assert not re.search(r"INSIA_PUBLISH_INSTAGRAM=1\s+(?:\S+/)?insia\b", OPS)
    assert not any(re.search(r"^\s*insia serve .*--media-port", body, re.M)
                   for _lang, body in code_blocks(OPS))
    assert "INSIA_MEDIA_PORT" in section("### 12-5. 이미지만 공개하기 (인스타그램)")

    # §8's ~/.insia.env names the three lines, and the §12-8 refresh cron sources that file and says the switch
    # must be in it (without it `publish refresh` only logs "꺼져 있어요" and the token lapses)
    cron_env = [body for _lang, body in code_blocks(section("## 8. 자동 실행 (cron · 작업 스케줄러)"))
                if "export INSIA_HOME=" in body]
    assert len(cron_env) == 1 and all(name in cron_env[0] for name in IG_VARS)
    assert not IG_VARS & set(exports(cron_env[0]))  # commented out: the beta stays opt-in
    refresh = section("### 12-8. 연결 해제·토큰 만료·갱신")
    [cron] = [body for lang, body in code_blocks(refresh) if lang == "cron"]
    assert ". $HOME/.insia.env;" in cron and "publish refresh" in cron
    assert "INSIA_PUBLISH_INSTAGRAM=1" in refresh

    # §8 file + the §12-4 lines, sourced by the cron, give the refresh command an enabled Instagram
    combined = exports(cron_env[0] + "\n" + saved)
    assert PublishSettings.from_env(combined, tmp_path).instagram_enabled


def test_publish_status_reads_the_documented_environment_without_serve_flags(home, capsys, monkeypatch):
    ig = section("### 12-4. 인스타그램 연결하기 (시험 중)")
    [saved] = [body for _lang, body in code_blocks(ig) if "cat >> ~/.insia.env" in body]
    for name, value in exports(saved).items():
        monkeypatch.setenv(name, value)
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0, err
    status = json.loads(out)
    assert status["platforms"]["instagram"]["enabled_by"] == "env"
    assert status["media"] == {**status["media"], "url": "https://media.example.com", "mode": "listener",
                               "port": 8766, "valid": True}


# ---------------------------------------------------------------------------
# Docker: files in and out, cron logs (finding 9)
# ---------------------------------------------------------------------------


def test_ops_docs_docker_steps_use_compose_cp_and_host_writable_cron_logs():
    docker = section("### 1-2. Docker로 설치 (늘 켜 두는 서버)")
    commands = "\n".join(body for _lang, body in code_blocks(docker))
    assert "docker compose cp 회사소개서.pdf insia:/tmp/회사소개서.pdf" in commands
    assert "docker compose exec insia insia docs add /tmp/회사소개서.pdf" in commands
    assert "docker compose exec -T insia insia profile edit-template --out - > profile.yaml" in commands
    assert "docker compose cp profile.yaml insia:/tmp/profile.yaml" in commands
    assert "profile import /tmp/profile.yaml" in commands
    assert "호스트의 workspace/ 폴더에 파일을 복사" not in OPS  # the host user cannot write there on Linux
    assert "docker compose cp insia:/data/backups/" in section("## 6. 백업과 복원")
    # every host cron line that calls into the container logs somewhere the host user can write
    lines = [line for line in (OPS + "\n" + COMPOSE).splitlines() if "docker compose exec" in line and ">>" in line]
    assert len(lines) >= 3, lines
    for line in lines:
        target = line.split(">>", 1)[1].split()[0]
        assert "workspace/" not in target and target.startswith("$HOME/"), line


def test_profile_template_on_stdout_round_trips_through_a_host_file(home, tmp_path, capsys):
    pytest.importorskip("yaml")  # the Docker image installs [docs], which brings PyYAML
    code, out, err = run(capsys, "profile", "edit-template", "--out", "-")
    assert code == 0, err
    assert out.startswith("# INSIA") and "새 워크스페이스" not in out  # messages go to stderr, not into the file
    host_file = tmp_path / "profile.yaml"
    host_file.write_text(out.replace('service_name: ""', 'service_name: "INSIA 테스트"'), encoding="utf-8")
    code, out, err = run(capsys, "profile", "import", host_file)
    assert code == 0, err
    code, out, err = run(capsys, "profile", "show", "--json")
    assert json.loads(out)["service_name"] == "INSIA 테스트"


# ---------------------------------------------------------------------------
# the shortened-id example (finding 12)
# ---------------------------------------------------------------------------


def test_ops_docs_short_id_example_is_part_of_a_real_item_id():
    bullet = next(line for line in section("### ③ 검토 · 수정 요청 · 승인").splitlines() if "겹치지 않는 일부만" in line)
    [full] = re.findall(r"`(it_[0-9a-z_-]+)`", bullet)
    [short] = re.findall(r"`items show ([^`]+)`", bullet)
    # the documented id has the shape the pipeline really makes (UTC time + 4 random hex + channel) …
    stamp = datetime.strptime(full[3:18], "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
    real = pipeline_item_id(new_run_id(stamp), "linkedin")
    assert re.fullmatch(r"it_\d{8}-\d{6}-[0-9a-f]{4}_linkedin", full) and real[:19] == full[:19] and len(real) == len(full)
    # … and the short form is its tail, which the CLI resolves; a MMDD-HHMM_channel form never can
    assert full.endswith(short) and short == full.rsplit("-", 1)[1]
    ids = [full, pipeline_item_id(new_run_id(stamp), "naver_blog"), "it_cc-sample-run_linkedin"]
    assert cli._match_id(short, ids, "콘텐츠", "insia items list") == full
    assert "UTC" in bullet
    assert not re.search(r"items show \d{4}-\d{4}_", OPS)


# ---------------------------------------------------------------------------
# .env stays out of git (finding 13)
# ---------------------------------------------------------------------------


def test_gitignore_keeps_env_files_out_of_git():
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in patterns and ".env.*" in patterns
    docker_patterns = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert ".env" in docker_patterns  # the image never gets it either
    git = shutil.which("git")
    if not git or not (ROOT / ".git").exists():
        return
    def ignored(name: str) -> bool:
        result = subprocess.run([git, "check-ignore", "--no-index", "-q", name], cwd=ROOT, timeout=20)
        assert result.returncode in (0, 1)
        return result.returncode == 0
    assert ignored(".env") and ignored(".env.local")
    assert not ignored(".env.example")  # a future template can still be committed


# ---------------------------------------------------------------------------
# configuration B, the step-5 states and the Docker restore (fix-verification follow-ups to 7, 8 and 9)
# ---------------------------------------------------------------------------


def config_b() -> str:
    media = section("### 12-5. 이미지만 공개하기 (인스타그램)")
    return media[media.index("**구성 B"):]


def step5() -> str:
    ig = section("### 12-4. 인스타그램 연결하기 (시험 중)")
    return ig[ig.index("5. 인스타그램 게시 설정을"):ig.index("\n6. ")]


def config_b_env(monkeypatch) -> dict[str, str]:
    """The §12-5 configuration B ``~/.insia.env`` lines, exported like a new terminal would (token filled in)."""
    [saved] = [body for _lang, body in code_blocks(config_b()) if "cat >> ~/.insia.env" in body]
    env = exports(saved)
    env["INSIA_ACCESS_TOKEN"] = env["INSIA_ACCESS_TOKEN"].replace("만든-토큰", secrets.token_urlsafe(24))
    for name in (*SERVER_ENV, "INSIA_MEDIA_PORT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return env


def test_ops_docs_config_b_saves_media_url_and_public_hosts_but_never_a_media_port(home, capsys, monkeypatch):
    text = config_b()
    env = config_b_env(monkeypatch)
    assert set(env) == CONFIG_B_VARS  # no INSIA_MEDIA_PORT
    assert "--media-base-url" not in text  # not only a serve flag, which the terminal commands never see
    assert "**`INSIA_MEDIA_PORT`는 넣지 마세요.**" in text and "setx" in text
    assert "`INSIA_MEDIA_BASE_URL=https://insia.example.com`" in text  # Docker .env line
    assert urlsplit(env["INSIA_MEDIA_BASE_URL"]).hostname == env["INSIA_PUBLIC_HOSTS"]  # the dashboard domain
    # the terminal reads configuration B from these variables alone
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0, err
    status = json.loads(out)
    assert status["platforms"]["instagram"]["enabled_by"] == "env"
    assert {k: status["media"][k] for k in ("url", "mode", "valid")} == {
        "url": env["INSIA_MEDIA_BASE_URL"], "mode": "main", "valid": True}
    code, out, err = run(capsys, "publish", "status")
    assert f"이미지 공개 주소: {env['INSIA_MEDIA_BASE_URL']} (대시보드 포트)" in out  # what the docs tell you to look for
    assert "(대시보드 포트)`이면 돼요" in text
    # the old step-5 table / 12-9 advice (save INSIA_MEDIA_PORT) silently turns it into configuration A
    monkeypatch.setenv("INSIA_MEDIA_PORT", "8766")
    code, out, err = run(capsys, "publish", "status", "--json")
    assert code == 0 and json.loads(out)["media"]["mode"] == "listener"
    # … which step 5, §8's ~/.insia.env and the 12-9 rows now warn about
    assert "`INSIA_MEDIA_PORT`는 **넣지 말고**" in step5()
    cron_env = next(body for _lang, body in code_blocks(section("## 8. 자동 실행 (cron · 작업 스케줄러)"))
                    if "export INSIA_HOME=" in body)
    assert "구성 B는 INSIA_MEDIA_PORT 줄을 그대로 두고" in cron_env
    rows = [line for line in section("### 12-9. API 게시 문제 해결").splitlines() if "공개 HTTPS 주소에서 가져가요" in line
            or "바깥에서 접속되지 않아요" in line]
    assert len(rows) == 2
    assert "구성 B는 `INSIA_MEDIA_BASE_URL`·`INSIA_PUBLIC_HOSTS`를(`INSIA_MEDIA_PORT` 없이)" in rows[0]
    assert "구성 B는 `insia serve`를 켜 두고 `INSIA_MEDIA_PORT`를 지우기" in rows[1]
    assert "INSIA_MEDIA_PORT는 비워 둬요" in COMPOSE


def test_server_started_from_the_config_b_environment_serves_images_on_the_dashboard_port(tmp_path, monkeypatch):
    env = config_b_env(monkeypatch)
    host = env["INSIA_PUBLIC_HOSTS"]
    base = Settings.from_env(env={}, mode="mock", speed=0.0, today="2026-09-29")
    settings = replace(base, out_dir=tmp_path / "outputs", sample_dir=None, web_dir=None, home=tmp_path / "ws")
    # an image staged the way a send stages it (a terminal `insia publish send` writes the same workspace folder);
    # staged before the start so the server's first clean-up pass cannot race a half-written folder
    image = b"\xff\xd8\xff\xe0 insia test image"
    preview_id, token = "pv_" + secrets.token_hex(12), secrets.token_hex(16)
    media = PublicMediaHost(settings.home / "publish")
    media.write_staging(preview_id, [image])
    media.publish(preview_id, token, [hashlib.sha256(image).hexdigest()])
    srv = make_server(settings, host="127.0.0.1", port=0)  # `insia serve` with no options (§12-5 configuration B)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
    thread.start()
    try:
        assert srv.publish.settings.media_mode == "main" and srv.publish.settings.media_port is None

        def get(path: str) -> tuple[int, bytes]:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            try:
                conn.request("GET", path, headers={"Host": host})
                resp = conn.getresponse()
                return resp.status, resp.read()
            finally:
                conn.close()

        assert srv.publish.media.public_root == media.public_root
        assert get(f"/pub/m/{token}/01.jpg") == (200, image)
        assert get("/api/items")[0] == 401  # the dashboard itself still needs the access token
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_ops_docs_step5_names_the_states_the_cli_shows_for_a_half_saved_setup(home, capsys, monkeypatch):
    text = step5()

    def instagram_lines() -> tuple[str, str]:
        code, out, err = run(capsys, "publish", "status")
        assert code == 0, err
        state = next(line for line in out.splitlines() if line.startswith("- 인스타그램:"))
        media = next((line for line in out.splitlines() if line.startswith("- 이미지 공개 주소:")), "")
        return state.split(":", 1)[1].split("·", 1)[0].strip(), media

    # the switch only inline on `insia serve`: a new terminal has nothing -> 꺼짐
    state, _media = instagram_lines()
    assert state == "꺼짐" and f"**{state}**" in text
    # the switch saved, the media only as serve flags: the media line says the URL is missing …
    monkeypatch.setenv("INSIA_PUBLISH_INSTAGRAM", "1")
    state, media = instagram_lines()
    assert "미디어 공개 주소(" in media and "\"미디어 공개 주소(…)가 없어요\"" in text
    # … and once an account is connected, Instagram reads 지금 쓸 수 없음 (not 꺼짐)
    now = datetime.now(timezone.utc)
    iso = lambda moment: moment.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    CredentialStore(home / "credentials").set_many("instagram", {
        "access_token": "IGAA-test-token-0123456789", "user_id": "17841400000000001", "username": "insia.kr",
        "account_type": "BUSINESS", "expires_at": iso(now + timedelta(days=60)), "issued_at": iso(now),
        "expires_estimated": "0"})
    ws = Workspace(home)
    try:
        ws.save_publish_connection(PublishConnection(platform="instagram", account_id="17841400000000001",
                                                     account_name="@insia.kr", status="connected"))
    finally:
        ws.close()
    state, _media = instagram_lines()
    assert state == "지금 쓸 수 없음" and f"**{state}**" in text
    assert "꺼진 것으로 보이고 토큰도 연장되지 않아요" not in text  # the old, inaccurate claim for serve flags


def test_ops_docs_docker_restore_uses_sudo_and_gives_the_files_back_to_the_container_user(tmp_path, capsys,
                                                                                         monkeypatch):
    restore = section("## 6. 백업과 복원")
    bullet = restore[restore.index("  - Docker(Linux): 호스트 사용자는"):]
    [block] = [body for _lang, body in code_blocks(bullet) if "sudo chown" in body]
    lines = [line.split("#", 1)[0].strip() for line in block.splitlines() if line.strip()]
    assert lines[0] == "docker compose stop" and lines[-1] == "docker compose start"
    assert lines[1] == "sudo rm -f workspace/insia.db-wal workspace/insia.db-shm"
    copy = re.fullmatch(r"sudo cp -r (\S+)/\. workspace/", lines[2])
    assert copy is not None
    assert lines[3] == "sudo chown -R 10001:10001 workspace"  # after the copy: the container must own what it writes
    assert "docker compose cp" in bullet.split("```", 1)[0]  # says why compose cp is not enough here
    assert "sudo cp -r <백업 폴더>/. workspace/" in COMPOSE and "sudo chown -R 10001:10001 workspace" in COMPOSE

    # the rm + cp lines really restore a backup over a workspace that moved on (sudo/chown need root: left out)
    app = tmp_path / "insia"  # the INSIA folder with ./workspace, as in docker-compose.yml
    home = app / "workspace"
    monkeypatch.setenv("INSIA_HOME", str(home))
    (tmp_path / "before.json").write_text(json.dumps({"service_name": "백업한 이름"}, ensure_ascii=False), encoding="utf-8")
    assert run(capsys, "profile", "import", tmp_path / "before.json")[0] == 0
    backup = app / copy.group(1)  # docker compose cp insia:/data/backups/<date> ./insia-backup-<date> (§6)
    code, out, err = run(capsys, "backup", "--out", backup)
    assert code == 0, err
    assert "Docker는 운영 안내 '6. 백업과 복원'" in out  # the backup's own restore hint points here
    (tmp_path / "after.json").write_text(json.dumps({"service_name": "백업 뒤에 바꾼 이름"}, ensure_ascii=False),
                                         encoding="utf-8")
    assert run(capsys, "profile", "import", tmp_path / "after.json")[0] == 0
    (home / "insia.db-wal").write_bytes(b"stale")  # left by a server that did not shut down cleanly
    (home / "insia.db-shm").write_bytes(b"stale")
    script = "\n".join(line.replace("sudo ", "", 1) for line in lines[1:3])
    subprocess.run(["sh", "-ec", script], cwd=app, check=True, timeout=20)
    assert not (home / "insia.db-wal").exists() and not (home / "insia.db-shm").exists()
    code, out, err = run(capsys, "profile", "show", "--json")
    assert code == 0 and json.loads(out)["service_name"] == "백업한 이름"

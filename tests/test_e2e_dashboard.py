"""End-to-end: the real dashboard (web/) against the real API server in mock mode, in a headless browser.

Covers the weekly journey a founder runs: 브랜드·자료 (profile + documents) → 스튜디오 run →
보관함 (blind check, review panel, edit, 재검수, 수정 요청, approval gate, schedule, publish) →
exports → 캘린더 (plan, generate, move, skip) → 사용량 → restart (replay + resume an interrupted
run) → token login/logout → phone width → agent results vs. a person's edit (kept in history or not),
an edit refused (409) while a job runs and restored afterwards, the bizplan blind preview, and API publishing
(publish.js): a workspace that never set it up renders the 보관함 detail exactly as before; the same server with
the fake-transport service (``INSIA_PUBLISH_FAKE=1``, temporary workspace) walks LinkedIn connect → approve →
confirm dialog → publish → "게시 완료 · API"; the Instagram button and 'AI 정보' choice (no default, locked while
the cards render) and the failed result's retry, driven in the page.

Needs Playwright and a Chromium build. The browser comes from ``INSIA_CHROMIUM``, else
``/opt/pw-browsers/chromium`` when it exists, else Playwright's own download. Without them the
whole module is skipped (never runs ``playwright install``). Set ``INSIA_E2E_SHOTS=<dir>`` to keep
a screenshot of every step.
"""

from __future__ import annotations

import io
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

try:  # a missing or broken Playwright install (e.g. no greenlet wheel) skips the module
    from playwright import sync_api
except ImportError as exc:  # includes ModuleNotFoundError
    if os.environ.get("INSIA_E2E_REQUIRED") == "1":  # CI: a missing browser must fail, not skip
        raise
    pytest.skip(f"Playwright가 없어서 대시보드 E2E를 건너뛰어요 ({exc})", allow_module_level=True)

from insia_agents import actions  # noqa: E402
from insia_agents.backends.mock_backend import MockBackend  # noqa: E402
from insia_agents.config import Settings, today_kst  # noqa: E402
from insia_agents.db import Workspace  # noqa: E402
from insia_agents.exporters import capabilities  # noqa: E402
from insia_agents.models import Draft  # noqa: E402
from insia_agents.publishers.service import PublishService  # noqa: E402
from insia_agents.publishers.settings import PublishSettings  # noqa: E402
from insia_agents.server import make_server  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "e2e-dashboard-token-0123"
TEAM = ("김하늘", "박바다")
TIMEOUT_MS = 30_000

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _chromium_path() -> str | None:
    env = (os.environ.get("INSIA_CHROMIUM") or "").strip()
    if env:
        return env
    default = Path("/opt/pw-browsers/chromium")
    return str(default) if default.exists() else None


class Server:
    """``insia serve`` in a thread on a temporary workspace (restartable on the same home)."""

    def __init__(self, settings: Settings, token: str | None = None) -> None:
        self.settings = settings
        self.token = token
        self.srv = None
        self.start()

    def start(self) -> None:
        self.srv = make_server(self.settings, host="127.0.0.1", port=0, web_dir=ROOT / "web", heartbeat=0.5,
                               token=self.token)
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()

    def stop(self) -> None:
        if self.srv is not None:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None

    def restart(self) -> None:
        self.stop()
        self.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.srv.server_address[1]}/"

    def api(self, path: str, body: dict | None = None, method: str | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path.lstrip("/"), data=data, method=method or ("POST" if data else "GET"),
                                     headers={"Content-Type": "application/json"} if data is not None else {})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def item(self, channel: str, run_id: str | None = None) -> dict:
        items = self.api(f"/api/items?channel={channel}")["items"]
        if run_id:
            items = [i for i in items if i["run_id"] == run_id]
        assert items, f"{channel} 콘텐츠가 없어요"
        return items[0]


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    root = tmp_path_factory.mktemp("e2e")
    mp = pytest.MonkeyPatch()
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "INSIA_ACCESS_TOKEN", "INSIA_PUBLIC_HOSTS", "INSIA_TRUST_PROXY",
                 "INSIA_MAX_LIVE_JOBS", "INSIA_MAX_MOCK_JOBS"):
        mp.delenv(name, raising=False)
    # Playwright finds its downloaded browsers under the user's cache dir; pin it before HOME moves.
    if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        cache = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
        mp.setenv("PLAYWRIGHT_BROWSERS_PATH", str(Path(cache) / "ms-playwright"))
    mp.setenv("HOME", str(root))
    mp.setenv("INSIA_RENDER", "0")  # carousel zip → slides.html (no second browser inside the server)
    base = Settings.from_env(env={}, mode="mock", speed=0.0)
    settings = replace(base, home=root / "workspace", out_dir=root / "outputs", web_dir=ROOT / "web",
                       sample_dir=ROOT / "examples" / "sample-run")
    pw = sync_api.sync_playwright().start()
    try:
        browser = pw.chromium.launch(executable_path=_chromium_path(), headless=True)
    except Exception as exc:  # noqa: BLE001 - no browser on this machine
        pw.stop()
        mp.undo()
        if os.environ.get("INSIA_E2E_REQUIRED") == "1":
            raise
        pytest.skip(f"Chromium을 띄울 수 없어서 대시보드 E2E를 건너뛰어요: {str(exc).splitlines()[0]}")
    server = Server(settings)
    shots = os.environ.get("INSIA_E2E_SHOTS")
    state = {"server": server, "browser": browser, "settings": settings, "root": root, "done": set(),
             "shots": Path(shots) if shots else None, "pages": []}
    if state["shots"]:
        state["shots"].mkdir(parents=True, exist_ok=True)
    try:
        yield state
    finally:
        for page in state["pages"]:
            try:
                page.context.close()
            except Exception:  # noqa: BLE001
                pass
        server.stop()
        browser.close()
        pw.stop()
        mp.undo()


def new_page(e2e, **ctx):
    ctx.setdefault("viewport", {"width": 1280, "height": 900})
    context = e2e["browser"].new_context(accept_downloads=True, **ctx)
    context.set_default_timeout(TIMEOUT_MS)
    # offline CI: never wait for Google Fonts or the 3D viewer CDN
    context.route(re.compile(r"^https?://(?!127\.0\.0\.1)"), lambda route: route.abort())
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    page.js_errors = errors  # type: ignore[attr-defined]
    e2e["pages"].append(page)
    return page


@pytest.fixture(scope="module")
def page(e2e):
    return new_page(e2e)


def shot(e2e, page, name: str) -> None:
    if e2e["shots"] is not None:
        page.screenshot(path=str(e2e["shots"] / f"{name}.png"))


def need(e2e, *steps: str) -> None:
    missing = [s for s in steps if s not in e2e["done"]]
    if missing:
        pytest.skip(f"앞 단계가 실패해서 건너뛰어요: {', '.join(missing)}")


def nav(page, view: str) -> None:
    page.click(f'#appnav a[data-view="{view}"]')
    page.wait_for_selector(f"#view-{view}:not([hidden])")


def wait_job(page, timeout: int = TIMEOUT_MS) -> str:
    # the banner lives in the studio view, which may be hidden while 보관함 is on screen
    page.wait_for_selector('#jobBanner[data-kind="done"], #jobBanner[data-kind="failed"]', state="attached", timeout=timeout)
    return page.get_attribute("#jobBanner", "data-kind")


def no_js_errors(page) -> None:
    assert not page.js_errors, page.js_errors


def wait_run(srv: Server, run_id: str, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    run = srv.api(f"/api/runs/{run_id}")
    while run["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.1)
        run = srv.api(f"/api/runs/{run_id}")
    assert run["status"] != "running", f"실행 {run_id}가 끝나지 않았어요"
    return run


def fresh_item(srv: Server, channel: str, topic: str) -> dict:
    """A new single-channel mock run (speed 0) → its content item."""
    started = srv.api("/api/runs", {"topic": topic, "channels": [channel], "options": {"speed": 0}})
    assert wait_run(srv, started["run_id"])["status"] == "completed"
    return srv.item(channel, started["run_id"])


def start_revise(page, instructions: str) -> str:
    page.click('[data-key="act-revise"]')
    page.fill("#reviseText", instructions)
    page.click(".action-panel .btn--primary")
    page.wait_for_function("location.hash === '#/studio'")  # the banner now shows this job (running)
    return wait_job(page)


# ---------------------------------------------------------------------------
# the journey (tests run in file order and share one workspace)
# ---------------------------------------------------------------------------


def test_01_profile_and_documents(e2e, page, tmp_path):
    srv = e2e["server"]
    page.goto(srv.base + "#/brand")
    page.wait_for_selector("#pf-company_name")
    fields = {
        "company_name": "인시아랩", "service_name": "스마트에이전트",
        "one_liner": "1인 창업자의 사업계획서와 SNS 글을 AI 에이전트 셋이 함께 써요",
        "description": "브리프 하나로 사업계획서·블로그·링크드인·인스타그램 초안을 만들어요.",
        "target_customers": "콘텐츠를 혼자 만드는 1인 창업자", "problem": "글쓰기에 주당 10시간 이상을 써요",
        "solution": "세 에이전트가 초안을 만들고 사람이 승인해요", "business_model": "월 구독",
        "pricing": "월 29,000원 (가정)", "tone": "친근한 전문가 톤", "cta": "무료 체험은 프로필 링크에서",
        "contact": "hello@insia.example",
        "differentiators": "사람 최종 승인\n출처 기반 사실 확인", "banned_words": "혁신적",
        "required_phrases": "#광고", "default_hashtags": "1인창업\n#AI마케팅", "brand_colors": "#3B5BDB\n14b8a6",
    }
    for key, value in fields.items():
        page.fill(f"#pf-{key}", value)
    for i, (role, name) in enumerate([("대표", TEAM[0]), ("CTO", TEAM[1])]):
        page.click("#teamAdd")
        page.fill(f"#tm-{i}-role", role)
        page.fill(f"#tm-{i}-name", name)
    assert page.inner_text(".meter-value").startswith("100%")
    page.click("#profileSave")
    page.wait_for_selector("#profileState:has-text('저장했어요')")
    shot(e2e, page, "01_profile_saved")

    page.reload()
    page.wait_for_selector("#pf-company_name")
    page.wait_for_function("document.getElementById('pf-company_name').value === '인시아랩'")
    assert page.input_value("#pf-pricing") == "월 29,000원 (가정)"
    assert page.input_value("#pf-default_hashtags") == "#1인창업\n#AI마케팅"
    assert page.input_value("#pf-brand_colors") == "#3B5BDB\n#14B8A6"
    assert page.input_value("#tm-1-name") == TEAM[1]
    assert page.inner_text(".meter-value").startswith("100%")
    profile = srv.api("/api/profile")
    assert profile["profile_complete"] is True and profile["profile"]["required_phrases"] == ["#광고"]

    md = tmp_path / "회사소개.md"
    md.write_text("# 회사 소개서\n\n베타 사용자 120명 (2026-08 기준).\n", encoding="utf-8")
    page.set_input_files("#docFile", str(md))
    page.wait_for_selector('#brandDocs .doc-title:has-text("회사 소개서")')
    page.click("#pasteBox summary")
    page.fill("#pasteTitle", "IR 요약")
    page.fill("#pasteText", "고객 인터뷰 30건 진행.")
    page.click("#pasteSubmit")
    page.wait_for_selector('#brandDocs .doc-title:has-text("IR 요약")')
    page.click('#brandDocs [data-key="prev-u1"]')
    assert "베타 사용자 120명" in page.inner_text("#brandDocs .doc-preview")
    page.click('#brandDocs [data-key="del-u2"]')
    page.click('#brandDocs [data-key="del-yes-u2"]')
    page.wait_for_selector('#brandDocs .doc-title:has-text("IR 요약")', state="detached")
    shot(e2e, page, "01_documents")
    docs = srv.api("/api/documents")["documents"]
    assert [(d["id"], d["kind"], d["title"]) for d in docs] == [("u1", "markdown", "회사 소개서")]
    # API 게시 연결 (publish.js): the only place publishing is set up, optional while nothing is (test_19 drives it)
    page.wait_for_selector('#brandConn:not([hidden]) .pub-platform[data-platform="linkedin"]')
    assert page.inner_text("#brandConn .docs-head") == "API 게시 연결\n선택 기능"
    no_js_errors(page)
    e2e["done"].add("profile")


def test_02_studio_run(e2e, page):
    need(e2e, "profile")
    srv = e2e["server"]
    page.goto(srv.base + "#/studio")
    page.wait_for_selector("#btnNewRun:not([hidden])")
    page.click("#btnNewRun")
    page.wait_for_selector("#briefDialog[open]")
    page.wait_for_function("document.getElementById('f-topic').value.length > 0")  # sample brief prefilled
    for channel in ("bizplan", "naver_blog", "linkedin", "instagram"):
        page.check(f"#f-ch-{channel}")
    page.fill("#f-speed", "0")
    page.click("#briefSubmit")
    page.wait_for_selector("#briefDialog", state="hidden")
    page.wait_for_selector('#runBar[data-kind="done"]')
    page.wait_for_selector('#runBar:has-text("결과 4개")')
    assert page.inner_text("#sourceTag") == "실시간 실행"
    assert page.locator("#summaryResults .result-tile").count() == 4
    shot(e2e, page, "02_studio_done")
    runs = srv.api("/api/runs?kind=pipeline")["runs"]
    assert runs[0]["status"] == "completed" and len(runs[0]["items"]) == 4
    e2e["run_id"] = runs[0]["run_id"]
    no_js_errors(page)
    e2e["done"].add("run")


def test_03_library_blind_check_and_review_panel(e2e, page):
    need(e2e, "run")
    srv = e2e["server"]
    nav(page, "library")
    page.wait_for_selector(".item-card")
    assert page.locator(".item-card").count() == 4
    assert page.locator(".item-card .score-badge b").count() == 4  # every card has a score
    shot(e2e, page, "03_library")
    page.click('.item-card[href*="_bizplan"]')
    page.wait_for_selector(".detail-head")
    page.wait_for_selector(".content-card .md")
    text = page.inner_text("#view-library")
    for name in TEAM:
        assert name not in text, f"사업계획서 화면에 팀원 실명 {name}이 보여요"
    review = page.inner_text(".review-body")
    for label in ("형식 검사", "블라인드(실명 미노출)", "금지 표현 없음"):
        assert label in review
    assert page.locator(".versions .version").count() >= 2  # R0 + revision rounds
    detail = srv.api(f"/api/items/{srv.item('bizplan')['id']}")
    assert all(name not in v["draft"]["content"] for v in detail["versions"] for name in TEAM)
    shot(e2e, page, "03_bizplan_detail")
    no_js_errors(page)
    e2e["done"].add("library")


def test_04_edit_linkedin_counters_match_server(e2e, page):
    need(e2e, "library")
    srv = e2e["server"]
    item = srv.item("linkedin")
    page.goto(srv.base + "#/library/" + item["id"])
    page.wait_for_selector('[data-key="edit"]')
    page.click('[data-key="edit"]')
    page.wait_for_selector("#edContent")
    content = page.input_value("#edContent")
    lines = content.split("\n")
    page.fill("#edContent", "새 훅: 조사와 검수가 병목이었어요.\n" + "\n".join(lines[1:]))
    page.wait_for_selector('#edMeters li:has-text("필수 문구 포함")')  # brand checks from the saved profile
    meters = {}
    for li in page.locator("#edMeters li").all():
        label = li.locator(".m-label").inner_text()
        meters[label] = li.locator(".m-val").inner_text()
    page.click(".editor button[type=submit]")
    page.wait_for_selector(".save-result")
    saved = page.inner_text(".save-result")
    assert "새 버전(v" in saved and "서버 형식 검사" in saved
    shot(e2e, page, "04_edit_saved")
    detail = srv.api(f"/api/items/{item['id']}")
    latest = detail["versions"][-1]
    assert latest["source"] == "human" and latest["draft"]["content"].startswith("새 훅:")
    assert detail["item"]["status"] == "draft" and detail["item"]["version"] == latest["version"]
    # the browser's live counters use the same units as channels.py
    from insia_agents.channels import check_format
    from insia_agents.models import Draft, Profile
    server_checks = check_format(Draft.model_validate(latest["draft"]), None, Profile.model_validate(srv.api("/api/profile")["profile"]))
    for check in server_checks:
        assert meters.get(check.label) == check.value, (check.label, meters.get(check.label), check.value)
    no_js_errors(page)
    e2e["linkedin"] = item["id"]
    e2e["done"].add("edit")


def test_05_review_and_revise_jobs(e2e, page):
    need(e2e, "edit")
    srv = e2e["server"]
    item_id = e2e["linkedin"]
    before = srv.api(f"/api/items/{item_id}")["versions"][-1]
    assert before["review"] is None
    page.click('[data-key="act-review"]')
    assert wait_job(page) == "done"
    page.wait_for_function("document.querySelector('.fold-meta') && !/검수 전/.test(document.querySelector('.fold-meta').textContent)")
    reviewed = srv.api(f"/api/items/{item_id}")["versions"][-1]
    assert reviewed["id"] == before["id"] and reviewed["review"] is not None

    page.click('[data-key="act-revise"]')
    page.fill("#reviseText", "첫 두 줄을 더 짧게 해 주세요")
    page.click(".action-panel .btn--primary")
    page.wait_for_function("location.hash === '#/studio'")  # watch the capybaras
    assert wait_job(page) == "done"
    shot(e2e, page, "05_revise_done")
    detail = srv.api(f"/api/items/{item_id}")
    latest = detail["versions"][-1]
    assert latest["source"] == "agent" and latest["instructions"] == "첫 두 줄을 더 짧게 해 주세요"
    assert latest["version"] == reviewed["version"] + 1
    page.click("#jobBanner a")
    page.wait_for_selector('.v-ins:has-text("첫 두 줄을 더 짧게")')
    no_js_errors(page)
    e2e["done"].add("jobs")


def test_06_approval_gate_schedule_publish(e2e, page):
    need(e2e, "jobs")
    srv = e2e["server"]
    item_id = e2e["linkedin"]
    latest = srv.api(f"/api/items/{item_id}")["versions"][-1]
    assert latest["review"] is not None and latest["review"]["passed"] is False  # the recorded run's LinkedIn stays below 80
    page.goto(srv.base + "#/library/" + item_id)
    page.wait_for_selector('[data-key="act-approve"]')
    page.click('[data-key="act-approve"]')
    panel = page.wait_for_selector('.action-panel[data-panel="approve"]')
    assert "바로 승인할 수 없어요" in panel.inner_text() and "검수를 통과하지 못했어요" in panel.inner_text()
    shot(e2e, page, "06_approval_blocked")
    page.click('.action-panel button:has-text("그래도 승인")')
    page.wait_for_selector('.detail-head .status-pill[data-status="approved"]')
    forced = srv.api(f"/api/items/{item_id}")["item"]
    assert forced["status"] == "approved" and forced["note"].startswith("그래도 승인")
    assert "그래도 승인" in page.inner_text(".actions-card .item-note")
    # the forced approval stays visible: a '강제 승인' label with the approved version and its score
    assert forced["approval_forced"] is True and forced["approved_version"] == latest["version"]
    page.wait_for_selector('.detail-head [data-key="forced-approval"]')
    assert "강제 승인" in page.inner_text(".detail-head .detail-state")
    record = page.inner_text(".actions-card .forced-note")
    assert "강제 승인 기록" in record and f"v{forced['approved_version']}" in record
    assert f"{forced['approved_score']}점" in record
    nav(page, "library")
    card = page.locator(f'.item-card[href$="{item_id}"]')
    card.wait_for()
    assert "강제 승인" in card.locator(".forced-pill").inner_text()
    shot(e2e, page, "06_forced_label")

    bizplan = srv.item("bizplan")
    assert bizplan["passed"] is True
    page.goto(srv.base + "#/library/" + bizplan["id"])
    page.wait_for_selector('[data-key="act-approve"]')
    page.click('[data-key="act-approve"]')
    page.wait_for_selector('.detail-head .status-pill[data-status="approved"]')
    assert page.locator(".detail-head .forced-pill").count() == 0  # a passed review: a normal approval
    day = (date.fromisoformat(today_kst()) + timedelta(days=3)).isoformat()
    page.click('[data-key="act-schedule"]')
    assert page.inner_text(".action-panel .panel-hint") == "INSIA는 예정일에 자동으로 게시하지 않아요. 날짜는 알림용이에요."  # DESIGN 10-2
    page.fill("#schedDate", day)
    page.click(".action-panel .btn--primary")
    page.wait_for_selector('.detail-head .status-pill[data-status="scheduled"]')
    page.click('[data-key="act-publish"]')
    page.fill("#pubUrl", "https://blog.naver.com/insia/1")
    page.click(".action-panel .btn--primary")
    page.wait_for_selector('.detail-head .status-pill[data-status="published"]')
    shot(e2e, page, "06_published")
    done = srv.api(f"/api/items/{bizplan['id']}")["item"]
    assert done["status"] == "published" and done["scheduled_at"] == day
    assert done["published_url"] == "https://blog.naver.com/insia/1" and done["published_at"]
    no_js_errors(page)
    e2e["done"].add("approval")


@pytest.mark.parametrize("channel,fmt", [("bizplan", "docx"), ("naver_blog", "html"), ("linkedin", "txt"), ("instagram", "zip")])
def test_07_exports_download(e2e, page, channel, fmt, tmp_path):
    need(e2e, "run")
    srv = e2e["server"]
    item = srv.item(channel, e2e["run_id"])
    page.goto(srv.base + "#/library/" + item["id"])
    link = page.locator(f'.export-card [data-format="{fmt}"]')
    if fmt == "docx" and not capabilities().get("docx"):
        assert page.locator('.export-card [data-disabled="true"]').count() >= 1
        pytest.skip("python-docx가 없어서 Word 내보내기는 비활성으로 보여요")
    link.wait_for()
    assert link.get_attribute("download") is not None
    assert link.get_attribute("href").startswith(f"/api/items/{item['id']}/export?format={fmt}")
    with page.expect_download() as info:
        link.click()
    download = info.value
    name = download.suggested_filename
    assert re.fullmatch(rf"\d{{4}}-\d{{2}}-\d{{2}}_{channel}_[^/\\:*?\"<>|]+\.{fmt}", name), name
    assert re.search(r"[가-힣]", name), f"한글 파일 이름이 살아 있어야 해요: {name}"
    path = tmp_path / name
    download.save_as(str(path))
    data = path.read_bytes()
    if fmt == "docx":
        docx = pytest.importorskip("docx")
        document = docx.Document(str(path))
        body = "\n".join(p.text for p in document.paragraphs)
        assert document.paragraphs and all(n not in body for n in TEAM)
    elif fmt == "zip":
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        slides = [n for n in names if re.fullmatch(r"slide-\d{2}\.png", n)]
        assert "caption.txt" in names and (slides or "slides.html" in names), names
    elif fmt == "html":
        assert data.lstrip().startswith(b"<") and "<h" in data.decode("utf-8")
    else:
        assert "#" in data.decode("utf-8")
    shot(e2e, page, f"07_export_{channel}")
    no_js_errors(page)


def test_08_calendar_plan_generate_move_skip(e2e, page):
    need(e2e, "profile")
    srv = e2e["server"]
    page.goto(srv.base + "#/calendar")
    page.click('[data-key="plan-toggle"]')
    assert page.input_value("#planStart") == today_kst()  # defaults to today (KST)
    today = date.fromisoformat(today_kst())
    monday = today + timedelta(days=(7 - today.weekday()) % 7)  # plan next Monday's week (today if Monday)
    page.fill("#planTheme", "AI로 콘텐츠 운영 시간 줄이기")
    page.fill("#planStart", monday.isoformat())
    for channel in ("naver_blog", "linkedin", "instagram"):
        page.fill(f"#planCount-{channel}", "1")
    assert page.is_hidden("#planWeekendHint")
    page.check("#planWeekend-instagram")  # 주말에도 올리기: Instagram only
    assert page.is_visible("#planWeekendHint") and "매일" in page.inner_text("#planWeekendHint")
    with page.expect_request(lambda r: r.url.endswith("/api/calendar/plan") and r.method == "POST") as sent:
        page.click("#planForm button[type=submit]")
    assert json.loads(sent.value.post_data)["weekend_channels"] == ["instagram"]
    page.wait_for_selector("#view-calendar .notice:has-text('계획을 세웠어요')")
    sunday = monday + timedelta(days=6)
    slots = srv.api(f"/api/calendar?from={monday}&to={sunday}")["slots"]
    assert sorted(s["channel"] for s in slots) == ["instagram", "linkedin", "naver_blog"]
    page.wait_for_selector(f'[data-key="slot-{slots[0]["id"]}"]')
    for slot in slots:
        day = date.fromisoformat(slot["date"])
        weekend_ok = slot["channel"] == "instagram"  # the only channel allowed on Saturday/Sunday
        assert monday <= day <= sunday and (day.weekday() < 5 or weekend_ok), f"평일이 아닌 날에 배치됐어요: {slot}"
        cell = page.locator(".cal-day").nth((day - monday).days)  # week view: 월 … 일
        assert cell.locator(f'[data-key="slot-{slot["id"]}"]').count() == 1, slot
    shot(e2e, page, "08_planned")

    first, second, third = slots[0], slots[1], slots[2]
    page.click(f'[data-key="slot-{first["id"]}"]')
    page.click("#slotDialog .slot-actions .btn--primary")  # 초안 만들기
    page.wait_for_function("location.hash === '#/studio'")
    assert wait_job(page) == "done"
    shot(e2e, page, "08_slot_generated")
    fresh = {s["id"]: s for s in srv.api("/api/calendar")["slots"]}
    assert fresh[first["id"]]["status"] == "drafted" and fresh[first["id"]]["item_id"]
    assert srv.api(f"/api/items/{fresh[first['id']]['item_id']}")["item"]["channel"] == first["channel"]

    nav(page, "calendar")
    page.wait_for_selector(f'[data-key="slot-{first["id"]}"][data-status="drafted"]')
    target = next(d for d in (monday + timedelta(days=i) for i in range(5)) if d.isoformat() != second["date"]).isoformat()
    page.click(f'[data-key="slot-{second["id"]}"]')
    page.click("#slotDialog button:has-text('날짜 바꾸기')")
    page.fill("#slotDate", target)
    page.click("#slotDialog button:has-text('날짜 저장')")
    page.wait_for_selector("#slotDialog", state="hidden")
    page.click(f'[data-key="slot-{third["id"]}"]')
    page.click("#slotDialog button:has-text('건너뛰기')")
    page.wait_for_selector(f'[data-key="slot-{third["id"]}"][data-status="skipped"]')
    fresh = {s["id"]: s for s in srv.api("/api/calendar")["slots"]}
    assert fresh[second["id"]]["date"] == target
    assert fresh[third["id"]]["status"] == "skipped"
    shot(e2e, page, "08_moved_skipped")
    no_js_errors(page)
    e2e["done"].add("calendar")


def test_09_usage_reads_sensibly_with_zero_cost(e2e, page):
    need(e2e, "run")
    srv = e2e["server"]
    page.goto(srv.base + "#/usage")
    page.wait_for_selector(".kpi-row")
    text = page.inner_text("#view-usage")
    assert "모의 실행은 비용이 들지 않아요" in text
    assert "$0.00" in text and "실행별 비용" in text
    shot(e2e, page, "09_usage")
    no_js_errors(page)


def test_10_restart_replay_and_resume_interrupted(e2e, page):
    need(e2e, "run")
    srv = e2e["server"]
    # a run that is still going when the server dies: start it slowly, stop the server, mark it running again
    started = srv.api("/api/runs", {"topic": "동네 꽃집 정기 배송", "channels": ["linkedin", "instagram"],
                                    "keywords": ["꽃 구독"], "options": {"speed": 1}})
    deadline = time.monotonic() + 10
    while srv.api(f"/api/runs/{started['run_id']}")["events"] < 5 and time.monotonic() < deadline:
        time.sleep(0.1)
    srv.stop()
    with Workspace(e2e["settings"].home) as ws:
        ws.update_run(started["run_id"], status="running")  # what a crash leaves behind
    srv.start()
    run = srv.api(f"/api/runs/{started['run_id']}")
    assert run["status"] == "interrupted" and run["resumable"] is True
    assert len(srv.api("/api/items")["items"]) >= 4  # everything from before the restart is still there

    page.goto(srv.base + "#/studio")
    page.wait_for_selector('#runBar:not([hidden])')
    assert page.inner_text("#sourceTag") == "지난 실행 다시 보기"
    assert "중단됨" in page.inner_text("#runBar")
    shot(e2e, page, "10_interrupted")
    page.click('[data-key="run-resume"]')
    page.wait_for_selector('#runBar[data-kind="done"]')
    assert srv.api(f"/api/runs/{started['run_id']}")["status"] == "completed"

    # replay a finished run from 실행 기록
    page.click("#btnRuns")
    row = page.locator("#runsDialog .run-row").filter(has_text="사업계획서")
    row.first.locator("button:has-text('다시 보기')").click()
    page.wait_for_selector("#runsDialog", state="hidden")
    page.wait_for_function("document.querySelectorAll('#summaryResults .result-tile').length === 4")
    assert page.inner_text("#sourceTag") == "지난 실행 다시 보기"
    shot(e2e, page, "10_replay")
    no_js_errors(page)


def test_11_token_login_and_logout(e2e):
    settings = replace(e2e["settings"], home=e2e["root"] / "workspace-token")
    locked = Server(settings, token=TOKEN)
    try:
        page = new_page(e2e)
        page.goto(locked.base + "#/library")
        page.wait_for_selector("#loginToken")
        assert page.inner_text("#modeBadge") == "LOCKED"
        page.fill("#loginToken", "wrong-token-000000")
        page.click(".login-form button[type=submit]")
        page.wait_for_selector(".login-form .form-error:has-text('토큰이 맞지 않아요')")
        shot(e2e, page, "11_wrong_token")
        page.fill("#loginToken", TOKEN)
        page.click(".login-form button[type=submit]")
        page.wait_for_selector("#view-library:not([hidden]) .view-head")
        assert page.inner_text("#modeBadge") == "LIVE"
        for view in ("calendar", "brand", "usage", "library"):
            nav(page, view)
            page.wait_for_selector(f"#view-{view} .view-title")
            assert not page.locator(f"#view-{view} .notice[data-kind='error']").count()
        page.click("#btnLogout")
        page.wait_for_selector("#loginToken")
        assert page.inner_text("#modeBadge") == "LOCKED"
        shot(e2e, page, "11_logged_out")
        no_js_errors(page)
    finally:
        locked.stop()


def test_12_phone_width_has_no_sideways_scroll(e2e):
    need(e2e, "run")
    srv = e2e["server"]
    page = new_page(e2e, viewport={"width": 390, "height": 844}, device_scale_factor=2, is_mobile=True, has_touch=True)
    for route in ("#/library/" + srv.item("linkedin")["id"], "#/calendar", "#/usage", "#/studio"):
        page.goto(srv.base + route)
        page.wait_for_timeout(600)
        width = page.evaluate("[document.documentElement.scrollWidth, window.innerWidth]")
        assert width == [390, 390], (route, width)
        shot(e2e, page, "12_phone_" + route.split("/")[1])
    no_js_errors(page)


# ---------------------------------------------------------------------------
# Agent results vs. a person's edit, edits refused while an agent writes, the blind preview
# (the server runs in this process: the mock backend is patched to act at a precise moment)
# ---------------------------------------------------------------------------


def test_13_revise_kept_in_history_when_a_person_edits_meanwhile(e2e, page, monkeypatch):
    need(e2e, "run")
    srv = e2e["server"]
    item = srv.item("naver_blog", e2e["run_id"])
    base = srv.api(f"/api/items/{item['id']}")["versions"][-1]
    original, fired = MockBackend.revise, []

    def revise_while_a_person_edits(self, brief, plan, research, draft, review, *args, **kwargs):
        out = original(self, brief, plan, research, draft, review, *args, **kwargs)
        if draft.channel == "naver_blog" and not fired:  # `insia` in a terminal saves an edit meanwhile
            fired.append(True)
            with Workspace(e2e["settings"].home) as cli:
                latest = cli.get_item(item["id"]).versions[-1].draft
                actions.edit_item(cli, item["id"], "터미널에서 사람이 고친 제목", latest.content, latest.hashtags,
                                  settings=e2e["settings"])
        return out

    monkeypatch.setattr(MockBackend, "revise", revise_while_a_person_edits)
    page.goto(srv.base + "#/library/" + item["id"])
    page.wait_for_selector('[data-key="act-revise"]')
    assert start_revise(page, "소제목을 더 구체적으로") == "done"
    kept = page.locator('#jobBanner [data-key="job-kept"]')
    assert kept.count() == 1 and "사람이 고친 버전" in kept.inner_text() and "기록" in kept.inner_text()
    shot(e2e, page, "13_kept_banner")
    detail = srv.api(f"/api/items/{item['id']}")
    revision, current = detail["versions"][-2], detail["versions"][-1]
    assert revision["source"] == "agent" and revision["version"] == base["version"] + 2
    assert current["source"] == "human" and current["draft"]["title"] == "터미널에서 사람이 고친 제목"

    page.click("#jobBanner a")  # 보관함에서 보기
    notice = page.wait_for_selector(".kept-notice")
    assert "에이전트 결과를 기록에만 남겼어요" in notice.inner_text() and "사람이 고친 버전" in notice.inner_text()
    page.click('[data-key="kept-view"]')
    page.wait_for_selector(f".notice:has-text('이전 버전(v{revision['version']})을 보고 있어요')")
    shot(e2e, page, "13_kept_notice")
    no_js_errors(page)


def test_14_revise_kept_current_by_a_cli_round_is_not_reported_as_kept(e2e, page, monkeypatch):
    """A CLI run's next round lands while the job re-reviews its revision: the run keeps the revision current (a
    copy on top that gets the job's review), so the dashboard must not say the result was only kept in history."""
    need(e2e, "run")
    srv = e2e["server"]
    item = srv.item("instagram", e2e["run_id"])
    original, fired = MockBackend.review, []

    def review_while_a_cli_round_lands(self, brief, research, draft, format_checks):
        out = original(self, brief, research, draft, format_checks)
        if draft.channel == "instagram" and any("[사람 지시]" in c for c in draft.change_log) and not fired:
            fired.append(True)
            with Workspace(e2e["settings"].home) as cli:  # `insia run-due` in a terminal: invisible to the server
                cli.add_run_version(item["id"], Draft(channel="instagram", round=draft.round + 1, title="CLI 라운드",
                                                      content="CLI 실행이 만든 다음 라운드"), run_id=e2e["run_id"])
        return out

    monkeypatch.setattr(MockBackend, "review", review_while_a_cli_round_lands)
    page.goto(srv.base + "#/library/" + item["id"])
    page.wait_for_selector('[data-key="act-revise"]')
    assert start_revise(page, "캡션 첫 줄을 더 짧게") == "done"
    assert fired and page.locator('#jobBanner [data-key="job-kept"]').count() == 0
    assert "기록에만" not in page.inner_text("#jobBanner")
    detail = srv.api(f"/api/items/{item['id']}")
    revision, cli_round, current = detail["versions"][-3:]
    assert cli_round["draft"]["title"] == "CLI 라운드" and current["draft"]["content"] == revision["draft"]["content"]
    assert current["review"] is not None and current["review"] == revision["review"]  # the job's review followed the copy
    assert detail["item"]["score"] == revision["review"]["score"]
    page.click("#jobBanner a")
    page.wait_for_selector(".detail-head")
    page.wait_for_function("document.querySelector('.fold-meta') && !/검수 전/.test(document.querySelector('.fold-meta').textContent)")
    assert page.locator(".kept-notice").count() == 0
    no_js_errors(page)


def test_15_edit_refused_while_a_job_runs_keeps_the_text_and_restores_it_later(e2e, page, monkeypatch):
    need(e2e, "run")
    srv = e2e["server"]
    item = fresh_item(srv, "linkedin", "동네 세탁소 수거 예약")
    entered, release = threading.Event(), threading.Event()
    original = MockBackend.revise

    def slow_revise(self, brief, plan, research, draft, review, *args, **kwargs):
        if draft.channel == "linkedin" and not entered.is_set():
            entered.set()
            release.wait(20)  # the revise job stays active until the test lets it go
        return original(self, brief, plan, research, draft, review, *args, **kwargs)

    monkeypatch.setattr(MockBackend, "revise", slow_revise)
    try:
        page.goto(srv.base + "#/library/" + item["id"])
        page.wait_for_selector('[data-key="edit"]')
        page.click('[data-key="edit"]')
        page.fill("#edTitle", "사람이 작업 중에 고친 제목")
        job = srv.api(f"/api/items/{item['id']}/revise", {"instructions": "짧게", "options": {"speed": 0}})
        assert entered.wait(10)
        page.click(".editor button[type=submit]")
        err = page.wait_for_selector(".editor .form-error:not([hidden])")
        assert err.get_attribute("data-status") == "409"
        assert "에이전트가 이 콘텐츠를" in err.inner_text() and "고친 내용은 편집창과 이 브라우저에 그대로 남아 있어요" in err.inner_text()
        assert page.input_value("#edTitle") == "사람이 작업 중에 고친 제목"  # nothing typed is lost
        shot(e2e, page, "15_edit_409")
    finally:
        release.set()
    assert wait_run(srv, job["run_id"])["status"] == "completed"
    newer = srv.api(f"/api/items/{item['id']}")["versions"][-1]
    assert newer["source"] == "agent" and newer["draft"]["title"] != "사람이 작업 중에 고친 제목"

    page.reload()  # the job added a newer version: the unsaved edit (kept for the older version) comes back
    page.wait_for_selector('[data-key="edit"]')
    page.click('[data-key="edit"]')
    page.wait_for_selector("#edTitle")
    assert page.input_value("#edTitle") == "사람이 작업 중에 고친 제목"
    restored = page.inner_text(".editor .notice")
    assert "저장하지 않은 편집을 되살렸어요" in restored and f"새 버전(v{newer['version']})" in restored
    page.click(".editor button[type=submit]")
    page.wait_for_selector(".save-result")
    saved = srv.api(f"/api/items/{item['id']}")["versions"][-1]
    assert (saved["source"], saved["draft"]["title"]) == ("human", "사람이 작업 중에 고친 제목")
    leftovers = page.evaluate("id => Object.keys(localStorage).filter(k => k.indexOf('insia.unsaved.' + id + '.') === 0 && localStorage.getItem(k))",
                              item["id"])
    assert leftovers == []  # the restored draft is cleared once saved
    no_js_errors(page)


def test_16_blind_preview_of_school_and_employer_names_is_never_shown_as_passed(e2e, page):
    need(e2e, "profile")
    srv = e2e["server"]
    before = srv.api("/api/profile")["profile"]
    team = [dict(m) for m in before["team"]]
    team[0]["background"] = "카카오 출신 PM 7년, 고려대학교 경영학 졸업"
    srv.api("/api/profile", {**before, "team": team}, method="PUT")
    try:
        item = fresh_item(srv, "bizplan", "동네 세탁소 수거 예약 서비스")
        page.goto(srv.base + "#/library/" + item["id"])
        page.reload()  # the dashboard caches the profile per page load; the team background was added via the API
        page.wait_for_selector('[data-key="edit"]')
        page.click('[data-key="edit"]')
        page.wait_for_selector('#edMeters li:has-text("블라인드")')
        page.fill("#edContent", page.input_value("#edContent") + "\n\n- 대표: 카카오 출신 PM 7년, 고려대학교 경영학 졸업 (자사 자료)")
        meter = page.locator('#edMeters li:has-text("블라인드")')
        page.wait_for_function("(() => { const li = [...document.querySelectorAll('#edMeters li')].find(x => x.textContent.includes('블라인드')); return li && li.dataset.ok === 'partial'; })()")
        assert meter.locator(".m-mark").inner_text() == "…" and "저장하면 서버가 확인" in meter.inner_text()
        page.click(".editor button[type=submit]")
        page.wait_for_selector(".save-result")
        server_row = page.locator('.save-result .checks-list li[data-check="blind_names"]')
        assert server_row.get_attribute("data-state") == "no" and "고려대학교" in server_row.inner_text()
        # the same session: the review card of the saved (unreviewed) version shows the server's verdict
        card_row = page.locator('.review-body .checks-list li[data-check="blind_names"]')
        assert "저장할 때 서버 계산" in page.inner_text(".review-body")
        assert card_row.get_attribute("data-state") == "no"
        shot(e2e, page, "16_blind_saved")

        page.reload()  # after a reload only the browser preview is left: partial, never "통과"
        page.wait_for_selector('.review-body .checks-list li[data-check="blind_names"]')
        row = page.locator('.review-body .checks-list li[data-check="blind_names"]')
        mark = row.locator("span").first
        assert row.get_attribute("data-state") == "part"
        assert (mark.inner_text(), mark.get_attribute("aria-label")) == ("…", "일부만 확인")
        assert "재검수하면 서버가 확인" in row.inner_text() and "✓" not in row.inner_text()
        shot(e2e, page, "16_blind_preview")
        no_js_errors(page)
    finally:
        srv.api("/api/profile", before, method="PUT")


# ---------------------------------------------------------------------------
# API publishing (web/js/publish.js). The dashboard package's tests have a 1-second budget together (DESIGN.md
# 12-3 #10, 14.5), so they stay on the 보관함 screen of this page (no reload, no second server or browser) and
# click through the DOM (the same handlers, without Playwright's ~80 ms of actionability waits per click).
#   17: a workspace that never set publishing up renders the 보관함 detail exactly as without publish.js.
#   18: the same server gets the fake-transport service (in-memory LinkedIn, nothing leaves the machine):
#       connected → approved item → confirm dialog → one send → "게시 완료 · API".
#   19: publish.js on its own, with the dashboard's request layer answered in place: the connection card (as an
#       unconfigured workspace sees it, then the address-paste connection), the Instagram button and 'AI 정보'
#       choice, the failed result's "다시 확인하고 게시".
# ---------------------------------------------------------------------------

_PUBLISH_DOM = """() => ({
  card: document.querySelector('.actions-card').outerHTML,
  head: document.querySelector('.detail-head').outerHTML,
  buttons: [...document.querySelectorAll('.actions-card .action-row button')].map(b => b.textContent),
  extra: document.querySelectorAll('.btn--api, .pub-section, #pubDialog, [data-key^="api-"], [data-via="api"]').length
})"""
_ATTEMPT_GET = "**/api/publish/attempts/*"
_LI_POST = ("혼자 창업하면 마케팅은 늘 '이번 주만 넘기고'가 돼요.\n\n"
            "그래서 조사와 검수부터 에이전트에게 맡기고, 저는 마지막 확인만 해요.\n\n"
            "여러분은 어떤 일을 먼저 덜어 내고 싶나요?")


def goto_hash(page, route: str) -> None:
    """Change the route inside the loaded dashboard (no reload) and return once the router has handled it: a new
    item's detail then shows its loading state, so later waits never match the previous screen."""
    changed = page.evaluate("""h => new Promise(done => {
      if (location.hash === h) { done(false); return; }  // no hashchange, nothing would be drawn again
      window.addEventListener('hashchange', () => done(true), { once: true });
      location.hash = h;
    })""", route)
    assert changed, f"이미 {route} 화면이에요"


def press(page, selector: str) -> None:
    """``element.click()`` once the element is shown and enabled (the handlers a mouse click runs; a disabled
    control ignores both). Waits at most 2 s, frame by frame, like Playwright's own checks but without their
    ~80 ms per click."""
    state = page.evaluate("""([s, ms]) => new Promise(done => {
      const start = performance.now();
      const attempt = () => {
        const e = document.querySelector(s);
        const why = !e ? 'missing' : !e.getClientRects().length ? 'hidden' : e.disabled ? 'disabled' : '';
        if (!why) { e.click(); done('ok'); }
        else if (performance.now() - start > ms) done(why);
        else requestAnimationFrame(attempt);
      };
      attempt();
    })""", [selector, 2000])
    assert state == "ok", f"{selector}: {state}"


def wait_for(page, selector: str) -> None:
    """Returns on the DOM change that adds ``selector`` (``wait_for_selector`` polls once per animation frame)."""
    found = page.evaluate("""([s, ms]) => new Promise(done => {
      if (document.querySelector(s)) { done(true); return; }
      const seen = new MutationObserver(() => {
        if (document.querySelector(s)) { seen.disconnect(); clearTimeout(timer); done(true); }
      });
      const timer = setTimeout(() => { seen.disconnect(); done(false); }, ms);
      seen.observe(document.documentElement, { childList: true, subtree: true, attributes: true });
    })""", [selector, TIMEOUT_MS])
    assert found, f"{selector}: 나타나지 않았어요"


def wait_route(page, held: list, timeout_ms: int = 10_000):
    """The first request a ``page.route`` handler kept (Playwright hands routes over only inside its own calls)."""
    waited = 0
    while not held:
        assert waited < timeout_ms, "요청이 오지 않았어요"
        page.wait_for_timeout(5)
        waited += 5
    return held[0]


def test_17_unconfigured_workspace_shows_no_publishing_ui_in_the_library(e2e, page):
    need(e2e, "approval")
    srv = e2e["server"]
    item_id = e2e["linkedin"]  # approved (forced) in test_06, never published
    detail = srv.api(f"/api/items/{item_id}")
    assert detail["item"]["status"] == "approved" and detail.get("publish") is None  # configured=false → publish: null
    assert srv.api("/api/publish")["configured"] is False
    goto_hash(page, "#/library/" + item_id)
    wait_for(page, '[data-key="act-publish"]')
    # the same detail drawn again with publish.js unplugged is the dashboard as it was before API publishing
    with_hooks, without_hooks = page.evaluate("""([id, grab]) => {
      const I = window.INSIA, hooks = I.publish, dom = new Function('return (' + grab + ')()');
      const withHooks = dom();
      I.publish = undefined;
      try { I.ws.go('library', id, { focus: false }); return [withHooks, dom()]; } finally { I.publish = hooks; }  // same route: drawn again
    }""", [item_id, _PUBLISH_DOM])
    assert with_hooks == without_hooks
    assert with_hooks["buttons"] == ["게시 완료 표시", "게시 예정일 정하기", "재검수", "수정 요청", "보관"]  # 게시 완료 표시 stays first
    assert with_hooks["extra"] == 0
    shot(e2e, page, "17_unconfigured_detail")
    no_js_errors(page)
    e2e["done"].add("publish_unconfigured")


def test_18_fake_publish_connect_confirm_and_publish_once(e2e, page):
    need(e2e, "publish_unconfigured")
    srv = e2e["server"]
    server = srv.srv
    workspace = server.manager.workspace
    env = {"INSIA_PUBLISH_FAKE": "1", "INSIA_PUBLISH_FAKE_ALLOW": str(workspace.home),  # allowed wherever the temp dir is
           "INSIA_CREDENTIALS_DIR": str(e2e["root"] / "publish-credentials")}
    settings = PublishSettings.from_env(env, workspace.home, server_port=server.server_address[1])
    assert settings.enabled and settings.fake, settings.disabled_reason  # a refused fake mode fails; it never skips
    service = PublishService(settings, workspace)  # what `INSIA_PUBLISH_FAKE=1 insia serve` builds, on this workspace
    unconfigured, server.publish = server.publish, service
    posts: list[str] = []

    def record(request) -> None:
        if request.method == "POST" and re.search(r"/publish(/preview)?$", request.url):
            posts.append(request.url.rsplit("/", 1)[-1])  # "publish" | "preview"

    polls: list = []
    page.on("request", record)
    try:
        li_id = e2e["linkedin"]
        li_draft = srv.api(f"/api/items/{li_id}")["versions"][-1]["draft"]
        srv.api(f"/api/items/{li_id}/draft", {"title": li_draft["title"], "content": _LI_POST, "hashtags": li_draft["hashtags"]},
                method="PUT")  # the recorded draft keeps [대표 경험: …] placeholders, which block publishing
        srv.api(f"/api/items/{li_id}/status", {"status": "approved", "force": True, "note": "그래도 승인 · E2E"})

        # connected the way the card does it (test_19 drives the card itself): app info, then the pasted address
        srv.api("/api/publish/linkedin/app", {"client_id": "e2e-client-id", "client_secret": "e2e-client-secret"}, method="PUT")
        start = srv.api("/api/publish/linkedin/connect", {})
        assert start["mode"] == "paste"  # the redirect URI is on localhost, this request on 127.0.0.1
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(start["authorize_url"]).query)["state"][0]
        connected = srv.api("/api/publish/linkedin/complete", {"url": f"{start['redirect_uri']}?code=e2e-code&state={state}"})
        assert connected["state"] == "connected"

        # LinkedIn: confirm dialog → one send → "게시 완료 · API"
        page.evaluate("id => INSIA.ws.go('library', id, { focus: false })", li_id)  # test_17 left it on screen: draw it again
        wait_for(page, '[data-key="act-api-publish"]:not([disabled])')
        buttons = page.eval_on_selector_all(".actions-card .action-row button", "bs => bs.map(b => b.textContent)")
        assert buttons[0] == "게시 완료 표시" and buttons[-1] == "LinkedIn에 API로 게시"  # added at the end, nothing moves
        press(page, '[data-key="act-api-publish"]')
        wait_for(page, "#pubDialog[open] .pub-text")
        dialog = page.evaluate("""() => ({ focus: document.activeElement.id, send: document.getElementById('pubSend').disabled,
                                           notices: document.querySelector('#pubDialog .pub-notices').textContent })""")
        assert dialog["focus"] == "pubTitle" and dialog["send"] is True  # nothing can be sent before the checkbox
        assert "예약·반복 게시를 하지 않아요" in dialog["notices"] and "게시 완료 표시" in dialog["notices"]
        shot(e2e, page, "18_linkedin_dialog")
        page.route(_ATTEMPT_GET, lambda route: polls.append(route) if not polls else route.continue_())
        press(page, "#pubConfirm")
        # two clicks in a row: the first turns the button off at once, so only one request goes out
        page.eval_on_selector("#pubSend", "b => { b.click(); b.click(); }")
        wait_for(page, '[data-key="api-progress"]')  # the first poll is held: still sending
        sending = page.evaluate("""() => ({ button: document.querySelector('[data-key="act-api-publish"]').textContent,
                                            locked: ['edit', 'act-publish', 'act-review', 'act-archive'].map(k => document.querySelector('[data-key="' + k + '"]').disabled) })""")
        assert sending == {"button": "게시 중…", "locked": [True, True, True, True]}  # nothing changes the item meanwhile
        wait_route(page, polls).continue_()
        wait_for(page, '.detail-head .status-pill[data-via="api"]')
        published = page.evaluate("""() => ({ pill: document.querySelector('.detail-head .status-pill').textContent,
                                              logos: document.querySelectorAll('.actions-card img, .actions-card svg[data-brand]').length })""")
        assert "게시 완료 · API" in published["pill"] and published["logos"] == 0  # plain names, no platform logos
        assert posts == ["preview", "publish"], posts
        done = srv.api(f"/api/items/{li_id}")
        assert done["item"]["status"] == "published" and done["item"]["published_via"] == "fake"
        shot(e2e, page, "18_linkedin_published")
        no_js_errors(page)
    finally:
        page.remove_listener("request", record)
        for route in polls[:1]:  # a failed wait must not leave the page's poll hanging
            try:
                route.continue_()
            except Exception:  # noqa: BLE001 - already continued
                pass
        page.unroute(_ATTEMPT_GET)
        server.publish = unconfigured
        try:  # later tests see an unconfigured workspace again
            service.disconnect("linkedin", forget_app=True)
        except Exception:  # noqa: BLE001 - an unsettled attempt keeps its connection; nothing else to undo
            pass
        service.shutdown(timeout=2.0)


# publish.js driven with its request layer (INSIA.ws.get/post) answered in place, on this loaded page. Returns what
# it saw; the assertions are in Python.
_PUBLISH_STATES = r"""async () => {
  const I = window.INSIA, ws = I.ws, pub = I.publish, real = { get: ws.get, post: ws.post };
  const calls = [], out = {}, reply = {};
  let status = null;  // null: GET /api/publish goes to the real server
  const settle = () => new Promise(r => setTimeout(r, 0));
  const until = async (fn, what) => {
    const t0 = performance.now();
    while (!fn()) { if (performance.now() - t0 > 5000) throw new Error('기다렸지만 없어요: ' + what); await new Promise(r => setTimeout(r, 2)); }
  };
  const q = s => document.querySelector(s);
  ws.get = function (path) {
    if (path === '/api/publish' && status) return Promise.resolve(status);
    return real.get.apply(ws, arguments);
  };
  ws.post = function (path, body) {
    const m = /\/(publish\/preview|publish|linkedin\/connect|linkedin\/complete)$/.exec(path);
    if (!m || !reply[m[1]]) return real.post.apply(ws, arguments);
    calls.push([m[1], body]);
    return reply[m[1]](body);
  };
  const li = { state: 'connected', app: { client_id_set: true, client_secret_set: true, source: 'workspace', redirect_uri: 'http://localhost:8765/oauth/linkedin/callback' },
               account: { name: 'E2E', kind: 'LinkedIn 개인 프로필', id_hint: '…ab12' }, token: { days_left: 59 } };
  const statusWith = (linkedin, instagram) => ({ enabled: true, configured: true, fake: false, media: {}, platforms: { linkedin: linkedin, instagram: instagram } });
  const ctxFor = (id, platform, block) => {
    const ctx = {
      item: { id: id, status: 'approved', channel: platform }, busy: false, editing: false, job: false, panel: '', refreshed: 0,
      detail: { publish: Object.assign({ platform: platform, state: 'connected', available: true, reason: '', blocked_by: '',
                                         blockers: [], active_attempt: null, last_attempt: null }, block) },
      getPanel: () => ctx.panel, setPanel: k => { ctx.panel = k; }, rerender: () => {},
      refresh: () => { ctx.refreshed++; return Promise.resolve(); }, edit: () => {}
    };
    return ctx;
  };
  const setStatus = async ig => {  // the next button() call loads it (once), as on the 보관함 screen
    status = statusWith(li, ig);
    pub.invalidate();
    pub.button(ctxFor('c-probe', 'instagram', {}));
    await settle(); await settle();
  };
  const shown = b => b ? { text: b.textContent, disabled: b.disabled } : null;
  const preview = (platform, extra) => Object.assign({
    preview_id: 'pv-' + platform, preview_hash: 'sha256:' + '0'.repeat(64), expires_at: new Date(Date.now() + 1800e3).toISOString(),
    platform: platform, item: { id: 'c-' + platform, version: 2, title: '상태 확인' }, account: { name: 'E2E' },
    content: { text: '미리보기 글이에요.', chars: 9, limit: platform === 'instagram' ? 2200 : 3000, hashtags: ['#1인창업'] },
    slides: [], errors: [], warnings: [], notices: [], quota: null, first_comment_link: '', request_preview: [], can_publish: true
  }, extra || {});
  const box = document.createElement('section');
  box.className = 'brand-conn card';
  try {
    // the connection card as a workspace that never set publishing up sees it (the server's own status)
    document.body.appendChild(box);
    pub.invalidate();
    pub.connectionsCard(box, { reload: true });
    await until(() => box.querySelector('.pub-platform[data-platform="linkedin"]') && box.querySelector('.docs-head .fold-meta'), 'card');
    out.card = { meta: box.querySelector('.docs-head .fold-meta').textContent, fake: !!box.querySelector('.pub-fake'),
                 logos: box.querySelectorAll('img, svg[data-brand]').length, consent: box.textContent.includes('권한 있는 서비스'),
                 editRedirect: !!box.querySelector('[data-key="conn-edit-redirect"]'), suggest: !!box.querySelector('[data-key="conn-suggest-redirect"]'),
                 connect: !!box.querySelector('[data-key="conn-li-connect"]') };
    // the address-paste connection once the app info is saved (LinkedIn's side made up here)
    status = statusWith(Object.assign({}, li, { state: 'not_connected', account: null, token: null }), { state: 'disabled' });
    reply['linkedin/connect'] = () => Promise.resolve({ mode: 'paste', authorize_url: 'https://www.linkedin.com/oauth/v2/authorization?response_type=code&state=st-e2e',
      redirect_uri: 'http://localhost:8765/oauth/linkedin/callback', open_url: 'http://localhost:8765/#/brand/connections', expires_in: 600 });
    reply['linkedin/complete'] = () => { status = statusWith(li, { state: 'disabled' }); return Promise.resolve(li); };
    pub.connectionsCard(box, { reload: true });
    await until(() => box.querySelector('[data-key="conn-li-connect"]'), 'connect button');
    box.querySelector('[data-key="conn-li-connect"]').click();
    await until(() => box.querySelector('#connLiPaste'), 'paste box');
    out.paste = { open: box.querySelector('[data-key="conn-open-origin"]').getAttribute('href'), focus: document.activeElement.dataset.key };
    const pasted = 'http://localhost:8765/oauth/linkedin/callback?code=c-e2e&state=st-e2e';
    const area = box.querySelector('#connLiPaste');
    area.value = pasted;
    area.dispatchEvent(new Event('input'));
    box.querySelector('[data-key="conn-paste-done"]').click();
    await until(() => box.querySelector('.pub-platform[data-platform="linkedin"] .pub-badge[data-kind="ok"]'), 'connected badge');
    out.paste.sent = calls.map(c => [c[0], c[1]]);
    out.paste.said = box.textContent.includes('LinkedIn 계정을 연결했어요');
    out.paste.disconnect = !!box.querySelector('[data-key="conn-disc-linkedin"]');
    box.remove();
    calls.length = 0;

    // the Instagram button: only once a token was pasted (an account is known), even while LinkedIn is set up
    await setStatus({ state: 'not_connected', account: null, token: null, requirements: {} });
    out.igNotConnected = shown(pub.button(ctxFor('c-ig', 'instagram', { state: 'not_connected', available: false })));
    out.igNotConfigured = shown(pub.button(ctxFor('c-ig', 'instagram', { state: 'not_configured', available: false })));
    out.igUnavailableNoAccount = shown(pub.button(ctxFor('c-ig', 'instagram', { state: 'unavailable', available: false, blockers: ['render_unavailable'] })));
    await setStatus({ state: 'unavailable', account: { id_hint: '…0001', username: '@insia' }, token: null, requirements: { render: false } });
    out.igUnavailableAccount = shown(pub.button(ctxFor('c-ig', 'instagram', { state: 'unavailable', available: false, blockers: ['render_unavailable'] })));
    await setStatus({ state: 'connected', account: { id_hint: '…0001', username: '@insia' }, token: { days_left: 50 }, requirements: {} });
    out.igConnected = shown(pub.button(ctxFor('c-ig', 'instagram', {})));

    // the 'AI 정보' pair: nothing chosen, no preview, nothing to send; a choice renders the cards and locks the pair
    let render = null;
    reply['publish/preview'] = () => new Promise(resolve => { render = resolve; });
    pub.button(ctxFor('c-ig', 'instagram', {})).click();
    const radios = () => [...document.querySelectorAll('input[name=pubAi]')].map(r => ({ checked: r.checked, disabled: r.disabled }));
    out.igOpen = { open: q('#pubDialog').open, radios: radios(), wait: !!q('#pubDialog .pub-wait'), confirm: q('#pubConfirm').disabled,
                   send: q('#pubSend').disabled, calls: calls.length };
    q('#pubAi-no').click();
    q('#pubAi-yes').click();  // switching mid-render: the radio is off, so this does nothing
    out.igRendering = { loading: !!q('#pubDialog .pub-loading'), radios: radios(), calls: calls.map(c => c[1].options) };
    render(preview('instagram', { slides: [1, 2, 3].map(n => ({ n: n, url: '', alt: n === 1 ? '카드 한 장에 담긴 내용을 소리 내어 읽어 주듯 적은 대체텍스트예요. '.repeat(12) : '슬라이드 ' + n })) }));
    await settle();
    const clamp = () => getComputedStyle(q('#pubDialog .pub-alt')).webkitLineClamp;
    out.igPreview = { radios: radios(), slides: document.querySelectorAll('#pubDialog .pub-slide').length, clamp: clamp(),
                      meta: q('#pubDialog .pub-content-head .fold-meta').textContent, confirm: q('#pubConfirm').disabled, send: q('#pubSend').disabled };
    q('#pubDialog .pub-slide-btn').click();
    out.igPreview.zoomedClamp = clamp();
    q('#pubDialog').close();  // its close event comes later, after the next dialog is already open: it must not end that one

    // a failed send: the result card's "다시 확인하고 게시" opens a fresh preview and sends nothing by itself
    calls.length = 0;
    reply['publish/preview'] = () => Promise.resolve(preview('linkedin'));
    reply['publish'] = () => Promise.resolve({ attempt: { id: 'pa-c-failed', platform: 'linkedin', status: 'failed',
      error: 'LinkedIn이 게시를 거절했어요(403). 개발자 앱에 Share on LinkedIn 제품이 있는지 확인해 주세요.' } });
    const liCtx = ctxFor('c-li', 'linkedin', {});
    pub.button(liCtx).click();
    await settle();
    q('#pubConfirm').click();
    q('#pubSend').click();
    await settle(); await settle();
    const section = pub.section(liCtx);
    const retry = section && section.querySelector('[data-key="api-retry"]');
    out.failed = { dialogOpen: q('#pubDialog').open, refreshed: liCtx.refreshed, text: section ? section.textContent : '',
                   retry: shown(retry), calls: calls.map(c => c[0]) };
    calls.length = 0;
    if (retry) retry.click();
    await settle();
    out.retry = { open: q('#pubDialog').open, calls: calls.map(c => c[0]), text: !!q('#pubDialog .pub-text'),
                  checked: q('#pubConfirm').checked, send: q('#pubSend').disabled };
  } finally {
    ws.get = real.get;
    ws.post = real.post;
    box.remove();
    I.views.publish.reset();  // forget every made-up id and status; closes the dialog
  }
  return out;
}"""


def test_19_publish_card_instagram_choice_and_retry_after_a_failure(e2e, page):
    need(e2e, "publish_unconfigured")  # this page on the 보관함 screen; the server is back to "never set up"
    seen = page.evaluate(_PUBLISH_STATES)
    # 브랜드·자료 → API 게시 연결 in a workspace that never set it up: optional, no logos, what is stored and how to
    # revoke it; an http redirect URL is only suggested for localhost, so nothing to "switch to" on 127.0.0.1
    assert seen["card"] == {"meta": "선택 기능", "fake": False, "logos": 0, "consent": True, "editRedirect": True, "suggest": False,
                            "connect": False}
    # the address-paste connection: "open at localhost" offered, the pasted address sent as is, then connected
    paste = seen["paste"]
    assert paste["open"] == "http://localhost:8765/#/brand/connections" and paste["focus"] == "conn-li-open"
    assert paste["sent"] == [["linkedin/connect", {}], ["linkedin/complete", {"url": "http://localhost:8765/oauth/linkedin/callback?code=c-e2e&state=st-e2e"}]]
    assert paste["said"] is True and paste["disconnect"] is True
    # Instagram gets a button only for a connected account: never for "not connected" / "no app" (7-2)
    assert seen["igNotConnected"] is None and seen["igNotConfigured"] is None and seen["igUnavailableNoAccount"] is None
    assert seen["igUnavailableAccount"] == {"text": "인스타그램에 API로 게시", "disabled": True}
    assert seen["igConnected"] == {"text": "인스타그램에 API로 게시", "disabled": False}
    # the 'AI 정보' label has no default: no preview and nothing to send until a person picks (14.2)
    unchosen = {"checked": False, "disabled": False}
    assert seen["igOpen"] == {"open": True, "radios": [unchosen, unchosen], "wait": True, "confirm": True, "send": True, "calls": 0}
    # picking renders the cards; the pair stays locked until they are back (a second render would be refused, 409)
    assert seen["igRendering"] == {"loading": True, "calls": [{"is_ai_generated": False}],
                                   "radios": [{"checked": False, "disabled": True}, {"checked": True, "disabled": True}]}
    preview = seen["igPreview"]
    assert preview["radios"] == [unchosen, {"checked": True, "disabled": False}]
    assert preview["slides"] == 3 and preview["clamp"] == "5" and preview["zoomedClamp"] == "none"  # long alt text folds, opens on zoom
    assert "/ 2,200자" in preview["meta"] and "/ 5개" in preview["meta"]
    assert preview["confirm"] is False and preview["send"] is True  # the checkbox comes first
    # a failed send says nothing went out and offers "다시 확인하고 게시", which starts from a fresh preview
    failed = seen["failed"]
    assert failed["calls"] == ["publish/preview", "publish"] and failed["dialogOpen"] is False and failed["refreshed"] >= 1
    assert "게시하지 못했어요" in failed["text"] and "아무것도 올라가지 않았어요" in failed["text"]
    assert failed["retry"] == {"text": "다시 확인하고 게시", "disabled": False}
    assert seen["retry"] == {"open": True, "calls": ["publish/preview"], "text": True, "checked": False, "send": True}
    assert page.evaluate("!document.getElementById('pubDialog') || !document.getElementById('pubDialog').open")
    no_js_errors(page)


# publish.js's record-only views (final review F1-1/3/4/6), drawn from made-up item blocks on this loaded page with its
# request layer answered in place. Returns what it saw; the assertions are in Python.
_PUBLISH_RECORDS = r"""async () => {
  const I = window.INSIA, ws = I.ws, pub = I.publish, real = { get: ws.get, put: ws.put };
  const out = {}, puts = [];
  const settle = () => new Promise(r => setTimeout(r, 0));
  const history = [
    { id: 'pa-h1', platform: 'instagram', version: 1, status: 'published', requested_by: 'dashboard@127.0.0.1', resolved_by: 'instagram_check', created_at: '2026-09-28T03:00:00Z' },
    { id: 'pa-h2', platform: 'instagram', version: 1, status: 'published', requested_by: 'cli:me@host', resolved_by: 'system:late_answer', created_at: '2026-09-28T02:00:00Z' }
  ];
  ws.get = function (path) {
    if (path === '/api/items/c-hist/publish') return Promise.resolve({ item_id: 'c-hist', attempts: history });
    if (path === '/api/publish') return Promise.resolve({ enabled: false, configured: false, fake: false, media: {}, platforms: {} });
    if (path.indexOf('/api/publish/attempts/') === 0) return new Promise(() => {});  // a poll that never answers
    return real.get.apply(ws, arguments);
  };
  ws.put = function (path, body) { puts.push([path, body]); return Promise.resolve({ attempt: {} }); };
  const ctxFor = (id, platform, block, item) => {
    const ctx = {
      item: Object.assign({ id: id, status: 'approved', channel: platform, published_via: '', published_url: '' }, item || {}),
      busy: false, editing: false, job: false, panel: '', refreshed: 0,
      detail: { publish: Object.assign({ platform: platform, state: 'connected', available: false, reason: '', blocked_by: '',
                                         blockers: [], active_attempt: null, last_attempt: null }, block) },
      getPanel: () => ctx.panel, setPanel: k => { ctx.panel = k; }, rerender: () => {},
      refresh: () => { ctx.refreshed++; return Promise.resolve(); }, edit: () => {}
    };
    return ctx;
  };
  const text = n => (n ? n.textContent : '');
  try {
    // switched off (INSIA_PUBLISH=0) with an unknown Instagram attempt: the lock and the 결과 불명 card, no re-check
    const unknown = { id: 'pa-u', platform: 'instagram', status: 'unknown', error: '인스타그램 응답이 끊겼어요.' };
    const offCtx = ctxFor('c-off', 'instagram', { state: 'disabled', active_attempt: unknown, last_attempt: unknown });
    const off = pub.section(offCtx);
    out.off = { lock: pub.lock(offCtx), button: pub.button(offCtx), card: !!(off && off.querySelector('[data-key="api-unknown"]')),
                check: !!(off && off.querySelector('[data-key="api-check"]')),
                resolve: !!(off && off.querySelector('[data-key="api-resolve-published"]')) };
    out.onCheck = !!pub.section(ctxFor('c-on', 'instagram', { active_attempt: unknown })).querySelector('[data-key="api-check"]');
    // step words: the server's step_label, else the fallback table (never 'media' / 'self_check')
    const sending = (step, extra) => Object.assign({ id: 'pa-s-' + step, platform: 'instagram', status: 'sending', step: step, progress: { done: 0, total: 10 } }, extra || {});
    out.steps = ['media', 'self_check', 'mystery_step'].map(s => text(pub.section(ctxFor('c-s-' + s, 'instagram', { active_attempt: sending(s) })).querySelector('[data-key="api-progress"]')));
    out.stepLabel = text(pub.section(ctxFor('c-s-label', 'instagram', { active_attempt: sending('children 2/7', { step_label: '이미지 등록 2/7' }) })).querySelector('[data-key="api-progress"]'));
    // a failure the platform message already explains: "nothing was posted" once
    const failed = { id: 'pa-f', platform: 'instagram', status: 'failed', created_at: '2026-09-28T03:00:00Z',
                     error: '인스타그램에서 오류가 났어요(코드 100). 아무것도 게시되지 않았어요.' };
    out.failedNote = text(pub.section(ctxFor('c-f', 'instagram', { last_attempt: failed })));
    // moved back after an API post of this version: already published, not "the status update failed"
    out.already = text(pub.section(ctxFor('c-a', 'linkedin', { blocked_by: 'already_published',
      reason: '이 버전은 이미 API로 게시했어요. 게시 완료로 다시 표시하려면 ‘게시 완료 표시’를 눌러 주세요. 다시 올리려면 내용을 고쳐 새 버전으로 승인해 주세요.' })));
    // 게시 기록: who settled an attempt, in words
    const hctx = ctxFor('c-hist', 'instagram', { last_attempt: history[0] }, { status: 'published', published_via: 'instagram_api', published_url: 'https://www.instagram.com/p/X/' });
    hctx.panel = 'api_attempts';
    pub.panel('api_attempts', hctx);
    await settle(); await settle();
    out.history = text(pub.panel('api_attempts', hctx));
    // an Instagram re-check that found several recent posts: pick one → PUT its address
    const found = { id: 'pa-c', platform: 'instagram', status: 'published', permalink: '', permalink_missing: true, candidates: [
      { id: '18000000000000002', permalink: 'https://www.instagram.com/p/MANUAL/', timestamp: '2026-09-28T03:04:00Z' },
      { id: '18000000000000001', permalink: 'https://www.instagram.com/p/APIPOST/', timestamp: '2026-09-28T03:00:20Z' },
      { id: '18000000000000009', permalink: '', timestamp: '2026-09-28T03:02:00Z' }] };
    const cctx = ctxFor('c-cand', 'instagram', { last_attempt: found }, { status: 'published', published_via: 'instagram_api' });
    const cs = pub.section(cctx);
    const picks = cs ? [...cs.querySelectorAll('[data-key="api-candidate-pick"]')] : [];
    out.cand = { count: picks.length, text: text(cs && cs.querySelector('[data-key="api-candidates"]')) };
    if (picks[1]) picks[1].click();
    await settle(); await settle();
    out.cand.puts = puts.slice();
    out.cand.refreshed = cctx.refreshed;
  } finally {
    ws.get = real.get;
    ws.put = real.put;
    I.views.publish.reset();
  }
  return out;
}"""


def test_20_publish_records_lock_steps_history_and_candidates(e2e, page):
    need(e2e, "publish_unconfigured")  # this page on the 보관함 screen
    seen = page.evaluate(_PUBLISH_RECORDS)
    off = seen["off"]  # INSIA_PUBLISH=0 with an unknown attempt: locked and answerable, nothing that calls the platform
    assert "게시 결과를 먼저 정리해 주세요" in off["lock"] and off["button"] is None
    assert off["card"] is True and off["resolve"] is True and off["check"] is False
    assert seen["onCheck"] is True  # with publishing on, Instagram's re-check is offered
    media, self_check, mystery = seen["steps"]
    assert "(이미지 올릴 준비)" in media and "(공개 주소 확인)" in self_check
    assert not any(raw in " ".join(seen["steps"]) for raw in ("media", "self_check", "mystery_step"))
    assert "인스타그램에 올리는 중이에요." in mystery  # a step without words is left out
    assert "(이미지 등록 2/7)" in seen["stepLabel"]
    assert "아무것도 게시되지 않았어요." in seen["failedNote"] and "아무것도 올라가지 않았어요" not in seen["failedNote"]
    assert "이미 API로 게시했어요" in seen["already"] and "바꾸지 못했어요" not in seen["already"]
    assert "인스타그램 재확인" in seen["history"] and "늦게 도착한 응답(자동)" in seen["history"]
    assert "instagram_check" not in seen["history"] and "system:late_answer" not in seen["history"]
    cand = seen["cand"]
    assert cand["count"] == 2 and "MANUAL" in cand["text"] and "APIPOST" in cand["text"]
    assert cand["puts"] == [["/api/publish/attempts/pa-c/permalink", {"permalink": "https://www.instagram.com/p/APIPOST/"}]]
    assert cand["refreshed"] >= 1
    no_js_errors(page)

"""End-to-end: the real dashboard (web/) against the real API server in mock mode, in a headless browser.

Covers the weekly journey a founder runs: 브랜드·자료 (profile + documents) → 스튜디오 run →
보관함 (blind check, review panel, edit, 재검수, 수정 요청, approval gate, schedule, publish) →
exports → 캘린더 (plan, generate, move, skip) → 사용량 → restart (replay + resume an interrupted
run) → token login/logout → phone width.

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
import urllib.request
import zipfile
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

try:  # a missing or broken Playwright install (e.g. no greenlet wheel) skips the module
    from playwright import sync_api
except ImportError as exc:  # includes ModuleNotFoundError
    pytest.skip(f"Playwright가 없어서 대시보드 E2E를 건너뛰어요 ({exc})", allow_module_level=True)

from insia_agents.config import Settings, today_kst  # noqa: E402
from insia_agents.db import Workspace  # noqa: E402
from insia_agents.exporters import capabilities  # noqa: E402
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

    bizplan = srv.item("bizplan")
    assert bizplan["passed"] is True
    page.goto(srv.base + "#/library/" + bizplan["id"])
    page.wait_for_selector('[data-key="act-approve"]')
    page.click('[data-key="act-approve"]')
    page.wait_for_selector('.detail-head .status-pill[data-status="approved"]')
    day = (date.fromisoformat(today_kst()) + timedelta(days=3)).isoformat()
    page.click('[data-key="act-schedule"]')
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
    page.click("#planForm button[type=submit]")
    page.wait_for_selector("#view-calendar .notice:has-text('계획을 세웠어요')")
    sunday = monday + timedelta(days=6)
    slots = srv.api(f"/api/calendar?from={monday}&to={sunday}")["slots"]
    assert sorted(s["channel"] for s in slots) == ["instagram", "linkedin", "naver_blog"]
    page.wait_for_selector(f'[data-key="slot-{slots[0]["id"]}"]')
    for slot in slots:
        day = date.fromisoformat(slot["date"])
        assert monday <= day <= sunday and day.weekday() < 5, f"평일이 아닌 날에 배치됐어요: {slot}"
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

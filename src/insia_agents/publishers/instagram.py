"""Instagram: an approved ``instagram`` item → a carousel on the connected professional account (DESIGN.md 4-2, 14.2,
14.3; IG §3–§7). Instagram API with Instagram Login, host ``graph.instagram.com``, a token pasted from the Meta app
dashboard (no OAuth in v1) that INSIA refreshes.

* Preview: ``clean_draft`` → ``parse_carousel`` + ``instagram_caption`` (the same code as the zip export), limits
  checked here (2–10 slides, caption ≤ 2,200, ≤ 5 hashtags, ≤ 20 @mentions, no placeholders, alt text ≤ 1,000),
  then the slides are rendered **once** as JPEG 1080×1350 (quality 90) and each image is checked (baseline, not
  MPO, ≤ 8,000,000 bytes, ratio 0.8–1.91 and equal to slide 1). The bytes are staged by the service and their
  sha256 goes into the confirmed preview.
* Send: quota check → images copied to the public folder → self-check of every public URL (200, image/jpeg, same
  sha256; a redirect fails) → one child container per slide (``is_carousel_item=true``, ``alt_text``; never
  ``caption`` or ``is_ai_generated``) → polling → parent (``media_type=CAROUSEL``, ``children``, ``caption`` and
  ``is_ai_generated=true`` **only** when the person chose it) → polling → ``claim_write`` → ``media_publish``
  → permalink. ``24/2207008`` after ``media_publish`` is retried at most twice (30 s, 60 s, each after a new
  ``claim_write``) when the container is still ``FINISHED``; a lost answer is checked once (``PUBLISHED`` → success)
  and otherwise left ``unknown`` — never published again automatically (live test IG-13 pending).
* ``reconcile`` re-checks an ``unknown`` attempt read-only (never ``media_publish``).

Bodies are form-encoded (``BODY_ENCODING``; live test IG-9 may switch it to JSON). Every response goes through
``first_record`` (wrapped ``{"data": [...]}`` or flat).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from ..channels import check_format
from ..exporters.common import ExportError, clean_draft, find_placeholders, strip_inline
from ..exporters.instagram import RenderUnavailable, parse_carousel, slides_html
from ..exporters.text import instagram_caption
from .base import (
    AttemptTakenOverError,
    InstagramPreviewOptions,
    PlatformError,
    PreviewDraft,
    PreviewOptions,
    ReconnectRequiredError,
    RenderError,
    SendOutcome,
    ValidationIssue,
)
from .http import IG_CONTAINER_TIMEOUT, Response, TransportError, first_record
from .media import jpeg_info, new_media_token, public_url, sha256_hex
from .redact import get_logger, register_secret

if TYPE_CHECKING:
    from ..models import ContentItemDetail, Profile, PublishAttempt
    from .base import AttemptGuard
    from .service import PublishService

log = get_logger(__name__)

GRAPH_HOST = "https://graph.instagram.com"
BODY_ENCODING = "form"                   # "form" | "json" — one constant to flip after live test IG-9
IG_MAX_CAPTION = 2200
IG_MAX_HASHTAGS = 5                      # platform cap since 2025-12 (IG §0-2); more is blocked in the preview
IG_MAX_MENTIONS = 20
IG_CAROUSEL_RANGE = (2, 10)
IG_MAX_BYTES = 8_000_000
IG_ALT_MAX = 1000
IG_RATIO_RANGE = (0.8, 1.91)
INSIA_MIN_SLIDES = 7                     # INSIA's own channel target (7–10) — a warning only
JPEG_QUALITY = 90
POLL_DELAYS = (5, 15, 30, 60)            # after the immediate first check; then every 60 s …
POLL_EVERY = 60
POLL_TOTAL_SECONDS = 300                 # … for at most 5 minutes in total
CREATE_RETRY_DELAYS = (1, 3)             # transient container-creation errors (-1/2207001, -1/2207032)
PUBLISH_RETRY_DELAYS = (30, 60)          # 24/2207008 after media_publish while the container is still FINISHED
TOKEN_LIFETIME = timedelta(days=60)
REFRESH_MIN_AGE = timedelta(hours=24)
REFRESH_WINDOW = timedelta(days=15)
EXPIRING_DAYS = 7
PROFESSIONAL_TYPES = {"business": "인스타그램 비즈니스 계정", "media_creator": "인스타그램 크리에이터 계정",
                      "creator": "인스타그램 크리에이터 계정"}
INSTAGRAM_POST_HOSTS = ("www.instagram.com", "instagram.com")
FAKE_MEDIA_BASE = "https://example.invalid"
_HASHTAG = re.compile(r"(?<!\S)#[^\s#]+")
_MENTION = re.compile(r"(?<![\w.@])@[A-Za-z0-9._]{1,30}")
_SUBCODE = re.compile(r"22070\d\d")
_SENTENCE_END = re.compile(r"[.!?。！？](?=\s|$)|(?:다|요|죠)\.(?=\s|$)|\n")
_AUTH_CODES = {102, 190, 463, 467}

MSG_NOTHING = "아무것도 게시되지 않았어요."
MSG_NO_NETWORK = "인스타그램에 연결하지 못했어요. 인터넷 연결을 확인해 주세요. " + MSG_NOTHING
MSG_UNKNOWN = ("인스타그램의 응답을 받지 못했어요. 게시됐을 수도 있어요. ‘인스타그램에서 다시 확인’을 눌러 확인해 주세요.")
MSG_RECONNECT = "인스타그램 연결이 끝났어요. 새 토큰을 붙여 넣어 주세요. " + MSG_NOTHING
MSG_TIMEOUT = "인스타그램이 5분 안에 이미지를 처리하지 못했어요. " + MSG_NOTHING
MSG_EXPIRED = "인스타그램 컨테이너가 만료됐어요. 새로 다시 시도해 주세요. " + MSG_NOTHING
MSG_RETRY_NEW = "인스타그램이 게시를 마치지 못했어요. 새로 다시 시도해 주세요. " + MSG_NOTHING
MSG_ACCOUNT_CHANGED = "그사이 연결된 계정이 바뀌었어요. 다시 확인해 주세요. " + MSG_NOTHING
MSG_NOT_PUBLISHED = "컨테이너는 남아 있지만 게시되지 않았어요. 다시 시도하면 새로 만들어요."
MSG_PERMALINK_MISSING = "게시는 됐지만 링크를 받지 못했어요. 인스타그램에서 확인한 뒤 주소를 넣어 주세요."
MSG_HOSTING = ("인스타그램이 이미지를 가져갈 공개 주소 {url}에 바깥에서 접속되지 않아요. 터널·리버스 프록시가 켜져 있는지, "
               "미디어 포트를 가리키는지, 비밀번호·봇 차단이 /pub/m/을 막지 않는지 확인해 주세요. " + MSG_NOTHING)

# (code, subcode) → Korean reason (IG §7). Unknown pairs get a generic sentence with the codes.
_ERRORS: dict[tuple[int, int], str] = {
    (-2, 2207003): "인스타그램이 이미지를 가져가다 시간이 초과됐어요. 공개 주소(터널·프록시)가 켜져 있는지 확인해 주세요.",
    (-1, 2207001): "인스타그램 서버 오류예요. 잠시 후 다시 시도해 주세요.",
    (-1, 2207032): "인스타그램 서버 오류예요. 잠시 후 다시 시도해 주세요.",
    (-2, 2207020): "인스타그램 컨테이너가 만료됐어요. 새로 다시 시도해 주세요.",
    (24, 2207008): "인스타그램 컨테이너를 찾지 못했어요. 새로 다시 시도해 주세요.",
    (4, 2207051): "인스타그램이 스팸으로 의심해 막았어요. 앱에서 직접 올려 주세요.",
    (9, 2207042): "오늘 인스타그램 API 게시 한도를 다 썼어요. 내일 다시 시도하거나 앱에서 직접 올려 주세요.",
    (24, 2207006): "인스타그램이 이미지를 찾지 못했어요. 토큰을 확인한 뒤 다시 시도해 주세요.",
    (25, 2207050): "계정이 제한된 상태예요. 인스타그램 앱에서 확인해 주세요.",
    (100, 2207028): "캐러셀 장 수가 인스타그램 기준(2~10장)에 맞지 않아요(INSIA 오류).",
    (100, 2207040): "@멘션이 인스타그램 한도(20개)를 넘었어요(INSIA 오류).",
    (36004, 2207010): "캡션이 인스타그램 한도를 넘었어요(INSIA 오류).",
    (9004, 2207052): ("인스타그램이 이미지를 가져가지 못했어요. 공개 주소가 인터넷에서 열리는지, 비밀번호·봇 차단이 "
                      "/pub/m/을 막지 않는지 확인해 주세요."),
    (36000, 2207004): "이미지가 인스타그램 한도(8MB)를 넘었어요(INSIA 오류).",
    (36001, 2207005): "이미지 형식을 인스타그램이 받지 않았어요(INSIA 오류).",
    (36003, 2207009): "이미지 비율을 인스타그램이 받지 않았어요(INSIA 오류).",
}
_SUBCODE_ONLY = {sub: text for (_code, sub), text in _ERRORS.items()}
_TRANSIENT_CREATE = {2207001: 2, 2207032: 2, 2207003: 1}   # subcode → retries allowed when creating a container
_NOT_READY = {2207008, 2207027}


class _Stop(Exception):
    """Internal: end the send with this outcome."""

    def __init__(self, outcome: SendOutcome) -> None:
        super().__init__(outcome.error)
        self.outcome = outcome


def _now_utc(service: PublishService) -> datetime:
    return service.clock.now().astimezone(timezone.utc)


def _fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def plain(text: str) -> str:
    return strip_inline(text or "").replace("**", "").strip()


def trim_alt(text: str, limit: int = IG_ALT_MAX) -> tuple[str, bool]:
    """Alt text cut at the last sentence end within ``limit`` characters (hard cut when there is none)."""
    if len(text) <= limit:
        return text, False
    head = text[:limit]
    ends = [m.end() for m in _SENTENCE_END.finditer(head)]
    cut = ends[-1] if ends and ends[-1] >= limit // 3 else limit
    return head[:cut].rstrip(), True


def count_hashtags(caption: str) -> int:
    return len(_HASHTAG.findall(caption or ""))


def count_mentions(caption: str) -> int:
    return len(_MENTION.findall(caption or ""))


def status_subcode(status: str) -> int | None:
    """The first ``22070dd`` in a container's ``status`` string (``"Error: 2207004"``, ``"ERROR 2207052: …"``)."""
    match = _SUBCODE.search(status or "")
    return int(match.group(0)) if match else None


def account_kind(account_type: str) -> str:
    return PROFESSIONAL_TYPES.get((account_type or "").strip().lower(), "인스타그램 프로페셔널 계정")


def graph_error(response: Response) -> tuple[int, int, str, str]:
    """``(code, subcode, type, fbtrace_id)`` of a Graph API error envelope (0 when absent). Never the message."""
    body = response.json()
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict):
        return 0, 0, "", ""

    def num(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    return (num(error.get("code")), num(error.get("error_subcode")), str(error.get("type") or ""),
            str(error.get("fbtrace_id") or "")[:60])


def is_auth_error(response: Response) -> bool:
    code, _sub, kind, _trace = graph_error(response)
    return response.status == 401 or code in _AUTH_CODES or (kind == "OAuthException" and code in (0, 190))


def error_message(code: int, subcode: int) -> str:
    text = _ERRORS.get((code, subcode)) or _SUBCODE_ONLY.get(subcode)
    if text:
        return text
    shown = f"{code}/{subcode}" if subcode else str(code or "알 수 없음")
    return f"인스타그램에서 오류가 났어요(코드 {shown})."


def _regain_minutes(response: Response) -> int | None:
    raw = response.header("x-business-use-case-usage")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    for entries in (data.values() if isinstance(data, dict) else []):
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("estimated_time_to_regain_access") is not None:
                try:
                    return int(entry["estimated_time_to_regain_access"])
                except (TypeError, ValueError):
                    return None
    return None


def refresh_due(values: dict[str, str], now: datetime) -> bool:
    """IG §3.2 + DESIGN.md 2-4: an estimated expiry → as soon as the token is 24 h old; otherwise when the last
    issue/refresh is 24 h old and fewer than 15 days remain."""
    if not values.get("access_token"):
        return False
    issued = _parse(values.get("issued_at", ""))
    refreshed = _parse(values.get("refreshed_at", ""))
    last = max([m for m in (issued, refreshed) if m is not None], default=None)
    if last is None or now - last < REFRESH_MIN_AGE:
        return False
    if values.get("expires_estimated") == "1" and refreshed is None:
        return True
    expires = _parse(values.get("expires_at", ""))
    return expires is None or expires - now < REFRESH_WINDOW


class InstagramClient:
    """The Graph API calls INSIA makes (all with ``Authorization: Bearer``; the refresh call is the documented
    exception that takes ``access_token`` as a query parameter — its URL is never logged)."""

    def __init__(self, service: PublishService) -> None:
        self.service = service

    @property
    def base(self) -> str:
        return f"{GRAPH_HOST}/{self.service.settings.ig_api_version}"

    def _call(self, method: str, path: str, token: str, *, params: dict[str, str] | None = None,
              fields: dict[str, str] | None = None, timeout: float | None = None) -> Response:
        headers = {"Authorization": f"Bearer {token}"}
        kwargs: dict[str, Any] = {"headers": headers, "params": params}
        if fields is not None:
            if BODY_ENCODING == "json":
                kwargs["json_body"] = fields
            else:
                kwargs["form"] = fields
        if timeout is not None:
            kwargs["timeout"] = timeout
        return self.service.transport.request(method, f"{self.base}/{path.lstrip('/')}", **kwargs)

    def me(self, token: str) -> Response:
        return self._call("GET", "me", token, params={"fields": "user_id,username,account_type"})

    def quota(self, token: str, ig_id: str) -> Response:
        return self._call("GET", f"{ig_id}/content_publishing_limit", token, params={"fields": "quota_usage,config"})

    def create_child(self, token: str, ig_id: str, image_url: str, alt: str) -> Response:
        fields = {"image_url": image_url, "is_carousel_item": "true"}
        if alt:
            fields["alt_text"] = alt
        return self._call("POST", f"{ig_id}/media", token, fields=fields, timeout=IG_CONTAINER_TIMEOUT)

    def create_carousel(self, token: str, ig_id: str, children: list[str], caption: str, ai_generated: bool) -> Response:
        fields = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": caption}
        if ai_generated:  # the parent only, and only when a person chose it (IG §1 #16, DESIGN.md 14.2)
            fields["is_ai_generated"] = "true"
        return self._call("POST", f"{ig_id}/media", token, fields=fields, timeout=IG_CONTAINER_TIMEOUT)

    def status(self, token: str, container_id: str) -> Response:
        return self._call("GET", container_id, token, params={"fields": "status_code,status"})

    def publish(self, token: str, ig_id: str, creation_id: str) -> Response:
        return self._call("POST", f"{ig_id}/media_publish", token, fields={"creation_id": creation_id})

    def media(self, token: str, media_id: str) -> Response:
        return self._call("GET", media_id, token, params={"fields": "id,permalink,timestamp,media_type"})

    def recent_media(self, token: str, ig_id: str) -> Response:
        return self._call("GET", f"{ig_id}/media", token, params={"fields": "id,permalink,timestamp", "limit": "5"})

    def refresh(self, token: str) -> Response:
        return self.service.transport.request("GET", f"{GRAPH_HOST}/refresh_access_token",
                                              params={"grant_type": "ig_refresh_token", "access_token": token})


def _current_draft(detail: ContentItemDetail):
    for candidate in reversed(detail.versions):
        if candidate.version == detail.item.version:
            return candidate.draft
    return detail.versions[-1].draft if detail.versions else None


class InstagramPublisher:
    """``Publisher`` for Instagram carousels."""

    platform = "instagram"

    def __init__(self, service: PublishService) -> None:
        self.service = service
        self.client = InstagramClient(service)

    @property
    def settings(self):
        return self.service.settings

    def readiness(self):
        return self.service.readiness("instagram")

    def media_base(self) -> str:
        return self.settings.media_base_url or (FAKE_MEDIA_BASE if self.settings.fake else "")

    # -- connection ------------------------------------------------------------------
    def lookup(self, token: str) -> dict[str, str]:
        """``/me`` → ``{"user_id", "username", "account_type"}``. ``ReconnectRequiredError`` on an auth error,
        ``PlatformError`` otherwise."""
        try:
            response = self.client.me(token)
        except TransportError:
            raise PlatformError(MSG_NO_NETWORK.replace(" " + MSG_NOTHING, ""), platform="instagram") from None
        if is_auth_error(response):
            raise ReconnectRequiredError(platform="instagram")
        body = first_record(response.json())
        if response.status != 200 or not isinstance(body, dict) or not (body.get("user_id") or body.get("id")):
            code, sub, _kind, trace = graph_error(response)
            raise PlatformError(f"인스타그램 계정 정보를 읽지 못했어요 (HTTP {response.status}).", platform="instagram",
                                platform_status=response.status, platform_code=code, platform_subcode=sub, trace_id=trace)
        return {"user_id": str(body.get("user_id") or body.get("id")), "username": str(body.get("username") or ""),
                "account_type": str(body.get("account_type") or "")}

    def try_refresh(self, token: str) -> tuple[str, int] | None:
        """One refresh call: ``(new token, expires_in)`` or ``None`` (not allowed yet, or failed). ``ReconnectRequiredError``
        is raised only for an auth error when ``strict`` handling is wanted by the caller (see ``refresh``)."""
        response = self.client.refresh(token)
        body = first_record(response.json())
        if response.status == 200 and isinstance(body, dict) and body.get("access_token"):
            new = str(body["access_token"])
            register_secret(new)
            try:
                expires_in = int(body.get("expires_in") or 0)
            except (TypeError, ValueError):
                expires_in = 0
            return new, expires_in
        if is_auth_error(response):
            raise ReconnectRequiredError(platform="instagram")
        return None

    # -- preview -----------------------------------------------------------------------
    def quota(self, token: str, ig_id: str) -> dict[str, int] | None:
        try:
            response = self.client.quota(token, ig_id)
        except TransportError:
            return None
        if is_auth_error(response):
            self.service.mark_reconnect("instagram", "인스타그램이 토큰을 받지 않았어요.")
            raise ReconnectRequiredError(platform="instagram")
        body = first_record(response.json())
        if response.status != 200 or not isinstance(body, dict):
            return None
        config = body.get("config") if isinstance(body.get("config"), dict) else {}
        try:
            return {"used": int(body.get("quota_usage") or 0), "total": int(config.get("quota_total") or 0)}
        except (TypeError, ValueError):
            return None

    def build_preview(self, detail: ContentItemDetail, profile: Profile | None, options: PreviewOptions) -> PreviewDraft:
        assert isinstance(options, InstagramPreviewOptions)
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        raw = _current_draft(detail)
        draft = clean_draft(raw) if raw is not None else None
        slides = parse_carousel(draft.content or "") if draft is not None else []
        caption = ""
        if draft is not None:
            try:
                caption = instagram_caption(draft)
            except ExportError:
                caption = ""
        if not caption.strip():
            errors.append(ValidationIssue("error", "caption_empty", "인스타그램 캡션(## 캡션)이 비어 있어요. 채운 뒤 다시 승인해 주세요."))
        low, high = IG_CAROUSEL_RANGE
        if not low <= len(slides) <= high:
            errors.append(ValidationIssue("error", "slides",
                                          f"캐러셀은 {low}~{high}장이어야 해요 (지금 {len(slides)}장)."))
        elif len(slides) < INSIA_MIN_SLIDES:
            warnings.append(ValidationIssue("warning", "slides_few",
                                            f"INSIA 기준(7~10장)보다 적어요 (지금 {len(slides)}장)."))
        chars = len(caption)
        if chars > IG_MAX_CAPTION:
            errors.append(ValidationIssue("error", "caption_too_long",
                                          f"캡션이 {chars:,}자라 인스타그램 한도({IG_MAX_CAPTION:,}자)를 넘어요."))
        elif len(caption.encode("utf-16-le")) // 2 > IG_MAX_CAPTION:
            warnings.append(ValidationIssue("warning", "count_method", "인스타그램이 글자를 세는 방식에 따라 캡션이 잘릴 수 있어요."))
        tags = _HASHTAG.findall(caption)
        if len(tags) > IG_MAX_HASHTAGS:
            errors.append(ValidationIssue("error", "hashtags",
                                          f"해시태그가 {len(tags)}개예요. 인스타그램은 {IG_MAX_HASHTAGS}개까지만 받아요."))
        mentions = count_mentions(caption)
        if mentions > IG_MAX_MENTIONS:
            errors.append(ValidationIssue("error", "mentions", f"@멘션이 {mentions}개예요. {IG_MAX_MENTIONS}개까지만 돼요."))
        keep = profile.required_phrases if profile is not None else []
        texts = [caption] + [f"{s.title}\n{s.text}\n{s.alt}\n{s.source}" for s in slides]
        found: list[str] = []
        for text in texts:
            for ph in find_placeholders(text, keep=keep):
                if ph not in found:
                    found.append(ph)
            if "○○" in text and "○○" not in found:
                found.append("○○")
        if found:
            shown = ", ".join(found[:3]) + (f" 외 {len(found) - 3}개" if len(found) > 3 else "")
            errors.append(ValidationIssue("error", "placeholder",
                                          f"자리표시가 남아 있어요: {shown}. 편집해서 채운 뒤 다시 승인해 주세요."))
        alts: list[str] = []
        missing_alt, trimmed_alt = [], []
        for n, slide in enumerate(slides, start=1):
            alt, cut = trim_alt(plain(slide.alt))
            if not alt:
                missing_alt.append(n)
            if cut:
                trimmed_alt.append(n)
            alts.append(alt)
        if missing_alt:
            warnings.append(ValidationIssue("warning", "alt_missing",
                                            f"대체텍스트가 없는 슬라이드: {', '.join(map(str, missing_alt))} (대체텍스트 없이 보내요)."))
        if trimmed_alt:
            warnings.append(ValidationIssue("warning", "alt_trimmed",
                                            f"대체텍스트가 {IG_ALT_MAX:,}자를 넘어 줄인 슬라이드: {', '.join(map(str, trimmed_alt))}."))
        if draft is not None:
            for check in check_format(draft, detail.brief, profile):
                if not check.passed:
                    warnings.append(ValidationIssue("warning", f"format_{check.id}",
                                                    f"{check.label}: {check.value} (기준 {check.expected})"))
        item = detail.item
        if item.approval_forced:
            score = f"{item.approved_score}점" if item.approved_score is not None else "검수 없음"
            warnings.append(ValidationIssue("warning", "forced_approval",
                                            f"검수를 통과하지 않은 버전({score})을 그래도 승인했어요."))

        token, ig_id = self.service.instagram_token()
        me = self.lookup(token)
        if me["user_id"] != ig_id:
            raise ReconnectRequiredError("연결된 계정이 바뀌었어요. 다시 연결해 주세요.", platform="instagram")
        quota = self.quota(token, ig_id)
        if quota is not None and quota["total"] and quota["used"] >= quota["total"]:
            errors.append(ValidationIssue("error", "quota",
                                          f"오늘 인스타그램 API 게시 한도({quota['total']}개)를 다 썼어요. 내일 다시 시도하거나 앱에서 "
                                          "직접 올려 주세요."))

        images: list[bytes] = []
        slide_rows: list[dict[str, Any]] = []
        if not errors and draft is not None:
            page = slides_html(slides, profile, title=draft.title, render_mode=True)
            try:
                rendered = self.service.render_slides(page, len(slides))
            except RenderUnavailable as exc:
                raise RenderError(f"카드 이미지를 그리지 못했어요: {exc}", platform="instagram") from None
            images = list(rendered)
            overflow = tuple(getattr(rendered, "overflow", ()) or ())
            if overflow:
                warnings.append(ValidationIssue("warning", "overflow",
                                                f"글이 길어 잘린 슬라이드: {', '.join(map(str, overflow))}"))
            first_ratio = None
            for n, data in enumerate(images, start=1):
                try:
                    info = jpeg_info(data)
                except ValueError as exc:
                    errors.append(ValidationIssue("error", "image", f"슬라이드 {n}: {exc}"))
                    continue
                problems = []
                if not info.baseline or info.has_mpf:
                    problems.append("baseline JPEG가 아니에요")
                if len(data) > IG_MAX_BYTES:
                    problems.append(f"파일이 {len(data):,}바이트라 8MB를 넘어요")
                ratio = info.ratio
                if not IG_RATIO_RANGE[0] - 1e-6 <= ratio <= IG_RATIO_RANGE[1] + 1e-6:
                    problems.append(f"비율 {info.width}×{info.height}이 인스타그램 기준(4:5~1.91:1) 밖이에요")
                if first_ratio is None:
                    first_ratio = ratio
                elif abs(ratio - first_ratio) > 1e-6:
                    problems.append("1번 슬라이드와 비율이 달라요")
                if problems:
                    errors.append(ValidationIssue("error", "image", f"슬라이드 {n}: {', '.join(problems)}"))
                slide_rows.append({"n": n, "alt": alts[n - 1] if n <= len(alts) else "", "bytes": len(data),
                                   "width": info.width, "height": info.height, "sha256": sha256_hex(data)})
        if not slide_rows:
            slide_rows = [{"n": n, "alt": alts[n - 1], "bytes": 0, "width": 0, "height": 0, "sha256": ""}
                          for n in range(1, len(slides) + 1)]

        base = self.media_base()
        count = len(slides)
        shown_base = base or "https://…"
        notices = [
            {"code": "no_delete", "message": "인스타그램 API로 올린 게시물은 INSIA에서 지울 수 없어요. 잘못 올렸다면 인스타그램 앱에서 "
                                             "직접 삭제해야 해요."},
            {"code": "public_media", "message": f"카드 이미지 {count}장을 잠깐 공개 주소({shown_base}/pub/m/…)에 올려 인스타그램이 "
                                                "가져가게 해요. 게시가 끝나면 바로 지워요(늦어도 24시간 안에)."},
        ]
        if quota is not None and quota["total"]:
            notices.append({"code": "quota", "message": f"오늘 남은 게시 한도: {max(0, quota['total'] - quota['used'])}/"
                                                        f"{quota['total']}개"})
        notices += [
            {"code": "no_tags", "message": "사람 태그·공동 작업자·유료 파트너십 표시는 API로 넣을 수 없어요. 필요하면 앱에서 올려 주세요."},
            {"code": "single_post", "message": "지금 이 게시물 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요."},
            {"code": "manual_done", "message": "이미 인스타그램 앱에서 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요."},
        ]
        graph = f"{GRAPH_HOST}/{self.settings.ig_api_version}"
        request_preview: list[dict[str, Any]] = []
        for row in slide_rows:
            fields = {"image_url": public_url(shown_base, "<token>", row["n"]), "is_carousel_item": "true"}
            if row["alt"]:
                fields["alt_text"] = row["alt"]
            request_preview.append({"method": "POST", "url": f"{graph}/<IG_ID>/media", "fields": fields})
        parent = {"media_type": "CAROUSEL", "children": "<CHILD_IDS>", "caption": caption}
        if options.is_ai_generated:
            parent["is_ai_generated"] = "true"
        request_preview.append({"method": "POST", "url": f"{graph}/<IG_ID>/media", "fields": parent})
        request_preview.append({"method": "POST", "url": f"{graph}/<IG_ID>/media_publish",
                                "fields": {"creation_id": "<CAROUSEL_ID>"}})
        username = f"@{me['username']}" if me["username"] else ""
        account = {"name": username or "인스타그램 계정", "kind": account_kind(me["account_type"]), "id_hint": "…" + ig_id[-3:],
                   "username": username}
        return PreviewDraft(
            platform="instagram", account_id=ig_id, account=account, api_version=self.settings.ig_api_version,
            options=options.to_json(),
            payload={"caption": caption, "slides": [{"n": r["n"], "sha256": r["sha256"], "bytes": r["bytes"], "alt": r["alt"]}
                                                    for r in slide_rows]},
            content={"text": caption, "chars": chars, "limit": IG_MAX_CAPTION, "hashtags": tags,
                     "options": options.to_json()},
            slides=slide_rows, slide_bytes=images, errors=errors, warnings=warnings, notices=notices,
            quota=quota, request_preview=request_preview,
        )

    # -- send ------------------------------------------------------------------------
    def _fail(self, message: str, code: str = "", **state: Any) -> _Stop:
        return _Stop(SendOutcome("failed", error_code=code, error=message, state=state))

    def _definite_error(self, response: Response, stage: str) -> _Stop:
        if is_auth_error(response):
            self.service.mark_reconnect("instagram", "인스타그램이 토큰을 받지 않았어요.")
            return self._fail(MSG_RECONNECT, "reconnect", reconnect=True)
        code, sub, _kind, trace = graph_error(response)
        state = {"fbtrace_id": trace} if trace else {}
        if code == 80002:
            minutes = _regain_minutes(response)
            wait = f"{minutes}분 뒤" if minutes else "잠시 뒤"
            return self._fail(f"인스타그램 API 호출 한도에 걸렸어요. {wait} 다시 시도해 주세요. {MSG_NOTHING}", "80002", **state)
        error_code = f"{code}/{sub}" if sub else (str(code) if code else str(response.status))
        return self._fail(f"{error_message(code, sub)} {MSG_NOTHING}", error_code, stage=stage, **state)

    def _create(self, make: Any, stage: str) -> str:
        """Create a container with the transient-error retries of IG §7; returns its id."""
        retries_used: dict[int, int] = {}
        while True:
            try:
                response = make()
            except TransportError:
                raise self._fail(MSG_NO_NETWORK, "network") from None
            body = first_record(response.json())
            if response.status == 200 and isinstance(body, dict) and body.get("id"):
                return str(body["id"])
            _code, sub, _kind, _trace = graph_error(response)
            allowed = _TRANSIENT_CREATE.get(sub, 0)
            if not is_auth_error(response) and retries_used.get(sub, 0) < allowed:
                delay = CREATE_RETRY_DELAYS[min(retries_used.get(sub, 0), len(CREATE_RETRY_DELAYS) - 1)]
                retries_used[sub] = retries_used.get(sub, 0) + 1
                self.service.clock.sleep(delay)
                continue
            raise self._definite_error(response, stage)

    def _poll(self, token: str, ids: list[str], guard: AttemptGuard, step: str) -> None:
        """Wait until every container is ``FINISHED`` (immediately, then 5 s, 15 s, 30 s, 60 s, every 60 s; 5 min)."""
        pending = list(ids)
        waited = 0
        delays = iter(POLL_DELAYS)
        while True:
            for container in list(pending):
                try:
                    response = self.client.status(token, container)
                except TransportError:
                    continue  # a read: try again next round
                if is_auth_error(response):
                    raise self._definite_error(response, step)
                body = first_record(response.json())
                code = str(body.get("status_code") or "").upper() if isinstance(body, dict) else ""
                if code in ("FINISHED", "PUBLISHED"):
                    pending.remove(container)
                elif code == "ERROR":
                    sub = status_subcode(str(body.get("status") or ""))
                    if sub is not None:
                        raise self._fail(f"{error_message(0, sub)} {MSG_NOTHING}", str(sub), container=container)
                    raise self._fail(f"인스타그램이 이미지를 처리하지 못했어요. {MSG_NOTHING}", "ERROR", container=container)
                elif code == "EXPIRED":
                    raise self._fail(MSG_EXPIRED, "EXPIRED", container=container)
            if not pending:
                return
            delay = next(delays, POLL_EVERY)
            if waited + delay > POLL_TOTAL_SECONDS:
                raise self._fail(MSG_TIMEOUT, "timeout")
            guard.step(step)
            self.service.clock.sleep(delay)
            waited += delay

    def _container_status(self, token: str, container: str) -> str:
        try:
            response = self.client.status(token, container)
        except TransportError:
            return ""
        body = first_record(response.json())
        return str(body.get("status_code") or "").upper() if response.status == 200 and isinstance(body, dict) else ""

    def _find_media(self, token: str, ig_id: str, since: str) -> tuple[str, str, list[dict[str, str]]]:
        """The media a ``PUBLISHED`` container became: ``(id, permalink, candidates)``; id/permalink only when exactly
        one recent post is newer than the attempt (else a person picks from ``candidates``)."""
        try:
            response = self.client.recent_media(token, ig_id)
        except TransportError:
            return "", "", []
        body = response.json()
        rows = body.get("data") if isinstance(body, dict) else None
        start = _parse(since) or datetime.min.replace(tzinfo=timezone.utc)
        start -= timedelta(seconds=60)  # clock skew between this machine and Instagram
        candidates = []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            stamp = _parse(str(row.get("timestamp") or ""))
            if stamp is not None and stamp >= start:
                candidates.append({"id": str(row["id"]), "permalink": str(row.get("permalink") or ""),
                                   "timestamp": str(row.get("timestamp") or "")})
        if len(candidates) == 1:
            return candidates[0]["id"], self.service.checked_permalink("instagram", candidates[0]["permalink"]), candidates
        return "", "", candidates

    def _permalink(self, token: str, media_id: str) -> str:
        try:
            response = self.client.media(token, media_id)
        except TransportError:
            return ""
        body = first_record(response.json())
        if response.status != 200 or not isinstance(body, dict):
            return ""
        return self.service.checked_permalink("instagram", str(body.get("permalink") or ""))

    def _published(self, token: str, media_id: str, permalink: str = "", **state: Any) -> SendOutcome:
        if media_id and not permalink:
            permalink = self._permalink(token, media_id)
        extra = dict(state)
        if not permalink:
            extra["permalink_missing"] = True
            extra["note"] = MSG_PERMALINK_MISSING
        return SendOutcome("published", external_id=media_id, permalink=permalink, state=extra)

    def _self_check(self, urls: list[tuple[str, str]]) -> None:
        for url, digest in urls:
            try:
                response = self.service.transport.request("GET", url, timeout=30.0)
            except TransportError:
                raise self._fail(MSG_HOSTING.format(url=url.rsplit("/pub/m/", 1)[0]), "hosting") from None
            content_type = response.header("content-type").split(";", 1)[0].strip().lower()
            if response.status != 200 or content_type != "image/jpeg" or sha256_hex(response.body) != digest:
                raise self._fail(MSG_HOSTING.format(url=url.rsplit("/pub/m/", 1)[0]), "hosting",
                                 self_check_status=response.status)

    def send(self, attempt: PublishAttempt, payload: dict[str, Any], guard: AttemptGuard) -> SendOutcome:
        try:
            return self._send(attempt, payload, guard)
        except _Stop as stop:
            return stop.outcome

    def _send(self, attempt: PublishAttempt, payload: dict[str, Any], guard: AttemptGuard) -> SendOutcome:
        guard.step("check")
        try:
            token, ig_id = self.service.instagram_token()
        except ReconnectRequiredError as exc:
            return SendOutcome("failed", error_code="reconnect", error=str(exc))
        if ig_id != attempt.account_id:
            return SendOutcome("failed", error_code="account_changed", error=MSG_ACCOUNT_CHANGED)
        block = payload.get("instagram") or {}
        options = payload.get("options") or {}
        caption = str(block.get("caption") or "")
        slides = list(block.get("slides") or [])
        ai_generated = options.get("is_ai_generated") is True
        try:
            quota = self.quota(token, ig_id)
        except ReconnectRequiredError:
            return SendOutcome("failed", error_code="reconnect", error=MSG_RECONNECT, state={"reconnect": True})
        if quota is None:
            return SendOutcome("failed", error_code="network", error=MSG_NO_NETWORK)
        if quota["total"] and quota["used"] >= quota["total"]:
            return SendOutcome("failed", error_code="quota",
                               error=f"오늘 인스타그램 API 게시 한도({quota['total']}개)를 다 썼어요. 내일 다시 시도하거나 앱에서 "
                                     f"직접 올려 주세요. {MSG_NOTHING}")

        # public images: folder name recorded first (cleanup/recovery can always find it), .expires written last
        token_dir = new_media_token()
        guard.step("media", {"slides": len(slides)})
        guard.set_media_token(token_dir)  # type: ignore[attr-defined] - the service's guard
        try:
            self.service.media.publish(attempt.preview_id, token_dir, [str(s.get("sha256") or "") for s in slides])
        except ValueError as exc:
            return SendOutcome("failed", error_code="changed", error=f"{exc} {MSG_NOTHING}")
        base = self.media_base()
        urls = [(public_url(base, token_dir, int(s["n"])), str(s["sha256"])) for s in slides]
        if not self.settings.skip_self_check and not self.settings.fake:
            guard.step("self_check")
            self._self_check(urls)

        children: list[str] = []
        total = len(slides)
        for index, (slide, (url, _digest)) in enumerate(zip(slides, urls), start=1):
            alt = str(slide.get("alt") or "")
            child = self._create(lambda url=url, alt=alt: self.client.create_child(token, ig_id, url, alt), "children")
            children.append(child)
            guard.step(f"children {index}/{total}", {"children": list(children)})
        self._poll(token, children, guard, "polling")
        carousel = self._create(lambda: self.client.create_carousel(token, ig_id, children, caption, ai_generated),
                                "carousel")
        guard.step("carousel", {"carousel_id": carousel})
        self._poll(token, [carousel], guard, "carousel")

        try:
            _, current = self.service.instagram_token()
        except ReconnectRequiredError:
            return SendOutcome("failed", error_code="reconnect", error=MSG_RECONNECT, state={"reconnect": True})
        if current != attempt.account_id:
            return SendOutcome("failed", error_code="account_changed", error=MSG_ACCOUNT_CHANGED)
        return self._publish_step(attempt, token, ig_id, carousel, guard)

    def _publish_step(self, attempt: PublishAttempt, token: str, ig_id: str, carousel: str,
                      guard: AttemptGuard) -> SendOutcome:
        retry_delays = list(PUBLISH_RETRY_DELAYS)
        while True:
            guard.claim_write()
            try:
                response = self.client.publish(token, ig_id, carousel)
            except TransportError as exc:
                if exc.sent == "no":
                    return SendOutcome("failed", error_code="network", error=MSG_NO_NETWORK)
                return self._after_lost_answer(attempt, token, ig_id, carousel)
            body = first_record(response.json())
            if response.status == 200 and isinstance(body, dict) and body.get("id"):
                return self._finish_published(token, str(body["id"]), guard)
            if response.status >= 500:
                return self._after_lost_answer(attempt, token, ig_id, carousel)
            code, sub, _kind, _trace = graph_error(response)
            if sub in _NOT_READY and not is_auth_error(response):
                status = self._container_status(token, carousel)
                if status == "PUBLISHED":
                    media_id, permalink, candidates = self._find_media(token, ig_id, attempt.created_at)
                    return self._finish_published(token, media_id, guard, permalink, candidates=candidates)
                if status == "FINISHED" and retry_delays:
                    self.service.clock.sleep(retry_delays.pop(0))
                    continue
                return SendOutcome("failed", error_code=f"{code}/{sub}", error=MSG_RETRY_NEW)
            stop = self._definite_error(response, "publish")
            return stop.outcome

    def _finish_published(self, token: str, media_id: str, guard: AttemptGuard, permalink: str = "",
                          **state: Any) -> SendOutcome:
        try:
            guard.step("permalink")
        except AttemptTakenOverError:  # the post exists: the service still records the success
            pass
        return self._published(token, media_id, permalink, **state)

    def _after_lost_answer(self, attempt: PublishAttempt, token: str, ig_id: str, carousel: str) -> SendOutcome:
        """``media_publish`` sent but no answer: check the container once; never publish again automatically."""
        if self._container_status(token, carousel) == "PUBLISHED":
            media_id, permalink, candidates = self._find_media(token, ig_id, attempt.created_at)
            return self._published(token, media_id, permalink, candidates=candidates)
        return SendOutcome("unknown", error_code="timeout", error=MSG_UNKNOWN)

    def reconcile(self, attempt: PublishAttempt, guard: AttemptGuard | None = None) -> SendOutcome | None:
        """Read-only re-check of an ``unknown`` attempt (DESIGN.md 4-2-7). ``None`` = still unknown."""
        carousel = str(attempt.state.get("carousel_id") or "")
        if not carousel:
            return SendOutcome("failed", error_code="no_container", error=MSG_NOT_PUBLISHED)
        try:
            token, ig_id = self.service.instagram_token()
        except ReconnectRequiredError:
            return None
        status = self._container_status(token, carousel)
        if status == "PUBLISHED":
            media_id, permalink, candidates = self._find_media(token, ig_id, attempt.created_at)
            return self._published(token, media_id, permalink, candidates=candidates)
        if status in ("FINISHED", "IN_PROGRESS"):
            return SendOutcome("failed", error_code=status, error=MSG_NOT_PUBLISHED)
        if status in ("EXPIRED", "ERROR"):
            return SendOutcome("failed", error_code=status, error=MSG_NOT_PUBLISHED)
        return None


def expiry_after(now: datetime, expires_in: int) -> datetime:
    return now + (timedelta(seconds=expires_in) if expires_in > 0 else TOKEN_LIFETIME)


__all__ = [
    "BODY_ENCODING", "FAKE_MEDIA_BASE", "IG_ALT_MAX", "IG_CAROUSEL_RANGE", "IG_MAX_BYTES", "IG_MAX_CAPTION",
    "IG_MAX_HASHTAGS", "IG_MAX_MENTIONS", "IG_RATIO_RANGE", "INSTAGRAM_POST_HOSTS", "POLL_DELAYS", "POLL_TOTAL_SECONDS",
    "InstagramClient", "InstagramPublisher", "account_kind", "count_hashtags", "count_mentions", "error_message",
    "graph_error", "is_auth_error", "refresh_due", "status_subcode", "trim_alt",
]

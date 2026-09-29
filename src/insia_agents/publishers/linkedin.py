"""LinkedIn: an approved ``linkedin`` item → a text post on the member's own profile (DESIGN.md 4-1, 14.3; LI §5, §8, §10).

* Transform: the manual ``.txt`` export text (``paste_text``) with bold markers removed; the trailing hashtag line
  is split off and appended again as hashtags. ``commentary`` is the body in the little text format (every
  reserved character escaped, LI §8) plus the tags: ``plain`` (default, DESIGN.md 14.3) ``#태그`` with the tag's
  own reserved characters escaped but the leading ``#`` kept as hashtag syntax, or ``template``
  ``{hashtag|\\#|태그}``. ``INSIA_LINKEDIN_HASHTAGS`` switches (``HASHTAG_MODE_DEFAULT`` is the one place to flip after
  live test T4).
* Send: ``POST https://api.linkedin.com/rest/posts`` with ``Linkedin-Version`` (``DEFAULT_VERSION``) and
  ``X-Restli-Protocol-Version: 2.0.0``; 201 (with or without ``x-restli-id``) is a confirmed success; a 409 is
  retried once (``claim_write`` again); 422 counts as success only for a *duplicate* naming a share/ugcPost URN;
  5xx / a timeout after the write step are ``unknown``; everything else is ``failed`` with a Korean reason. A 403
  gets the "Share on LinkedIn product / w_member_social scope" hint (live test T1 not run yet, 14.3); there is no
  ``ugcPosts`` fallback in v1.

Changing the API version (developers): read LinkedIn's change log → update ``DEFAULT_VERSION`` (settings) and
``KNOWN_SUNSETS`` → run the fake-transport tests → repeat live test T1 → release note. Platform versions and limits
change only here and in ``instagram.py``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import TYPE_CHECKING, Any

from ..channels import check_format
from ..exporters.common import ExportError, find_placeholders, is_hashtag_line, strip_inline
from ..exporters.text import paste_text
from .base import (
    VISIBILITY_LABELS,
    LinkedInPreviewOptions,
    PlatformError,
    PreviewDraft,
    PreviewOptions,
    ReconnectRequiredError,
    SendOutcome,
    ValidationIssue,
)
from .http import FAKE_PERMALINK_BASE, TransportError
from .oauth import fetch_userinfo
from .redact import get_logger
from .settings import DEFAULT_LINKEDIN_VERSION

if TYPE_CHECKING:
    from ..models import ContentItemDetail, Profile, PublishAttempt
    from .base import AttemptGuard
    from .service import PublishService

log = get_logger(__name__)

DEFAULT_VERSION = DEFAULT_LINKEDIN_VERSION
# Known sunset dates (LI §12, versioning page 2026-09-16). Versions not listed: the 15th, 12 months later.
KNOWN_SUNSETS: dict[str, str] = {"202609": "2027-09-15", "202511": "2026-11-16", "202510": "2026-10-15",
                                 "202509": "2026-09-15"}
SUNSET_WARNING_DAYS = 60
LINKEDIN_MAX_CHARS = 3000                 # LinkedIn Help a528176 (the channel guide's 1,300–2,000 is INSIA's own target)
POSTS_URL = "https://api.linkedin.com/rest/posts"
RESTLI_PROTOCOL = "2.0.0"
HASHTAG_MODE_DEFAULT = "plain"            # DESIGN.md 14.3 — mirrors settings.DEFAULT_LINKEDIN_HASHTAGS
ACCOUNT_KIND = "LinkedIn 개인 프로필"
LINKEDIN_POST_HOSTS = ("www.linkedin.com", "linkedin.com")
LITTLE_RESERVED = frozenset("|{}@[]()<>#\\*_~")
_POST_URN = re.compile(r"urn:li:(?:share|ugcPost):\d+")
_FIRST_COMMENT = re.compile(r"첫\s*댓글\s*링크\s*[:：]\s*(https?://\S+)")

MSG_NO_NETWORK = "LinkedIn에 연결하지 못했어요. 인터넷 연결을 확인해 주세요. 글은 올라가지 않았어요."
MSG_UNKNOWN = ("LinkedIn의 응답을 받지 못했어요. 글이 올라갔을 수도 있어요. "
               "LinkedIn 내 활동에서 확인한 뒤 알려 주세요.")
MSG_RECONNECT = "LinkedIn 연결이 끝났거나 해제됐어요. 다시 연결해 주세요. 글은 올라가지 않았어요."
MSG_FORBIDDEN = ("게시 권한이 없어요. 개발자 앱에 ‘Share on LinkedIn’ 제품이 추가됐는지, 토큰 스코프에 w_member_social이 "
                 "있는지 확인한 뒤 다시 연결해 주세요. 글은 올라가지 않았어요.")
MSG_CONFLICT = "LinkedIn에서 충돌이 났어요. 잠시 후 다시 시도해 주세요. 글은 올라가지 않았어요."
MSG_RATE_LIMITED = "LinkedIn 하루 한도에 걸렸어요. 한국 시간 오전 9시(UTC 자정) 이후 다시 시도해 주세요. 글은 올라가지 않았어요."
MSG_TOO_LONG = "글이 LinkedIn 한도를 넘었어요. 줄여서 다시 승인해 주세요. 글은 올라가지 않았어요."
MSG_ACCOUNT_CHANGED = "그사이 연결된 계정이 바뀌었어요. 다시 확인해 주세요. 글은 올라가지 않았어요."


def little_escape(text: str) -> str:
    """Escape every little-format reserved character (including the backslash) so it stays plain text (LI §8)."""
    return "".join("\\" + ch if ch in LITTLE_RESERVED else ch for ch in text)


def tag_word(tag: str) -> str:
    return re.sub(r"\s+", "", str(tag or "")).lstrip("#＃")


def hashtags_text(tags: list[str], mode: str) -> str:
    """The hashtag part of ``commentary``: ``plain`` → ``#태그`` (the ``#`` is hashtag syntax, not escaped; reserved
    characters inside the word are), ``template`` → ``{hashtag|\\#|태그}``."""
    if mode == "template":
        return " ".join("{hashtag|\\#|" + little_escape(t) + "}" for t in tags)
    return " ".join("#" + little_escape(t) for t in tags)


def build_commentary(body: str, tags: list[str], mode: str = HASHTAG_MODE_DEFAULT) -> str:
    parts = [little_escape(body)]
    if tags:
        parts.append(hashtags_text(tags, mode))
    return "\n\n".join(p for p in parts if p)


def split_text(draft_text: str) -> tuple[str, list[str], bool]:
    """``(body, tags, had_bold)`` from the paste text: bold markers removed, trailing hashtag line split off."""
    had_bold = "**" in draft_text
    lines = [strip_inline(line) for line in draft_text.replace("\r\n", "\n").rstrip().split("\n")]
    tags: list[str] = []
    if lines and is_hashtag_line(lines[-1]):
        tags = [w for w in (tag_word(t) for t in lines[-1].split()) if w]
        lines = lines[:-1]
    body = "\n".join(lines).rstrip()
    seen: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.append(tag)
    return body, seen, had_bold


def visible_text(body: str, tags: list[str]) -> str:
    return body + ("\n\n" + " ".join("#" + t for t in tags) if tags else "")


def version_sunset(version: str) -> str:
    """Sunset date (ISO) of a ``YYYYMM`` version: the known table, else the 15th twelve months later."""
    if version in KNOWN_SUNSETS:
        return KNOWN_SUNSETS[version]
    try:
        year, month = int(version[:4]), int(version[4:6])
        return date(year + 1, month, 15).isoformat()
    except (ValueError, IndexError):
        return ""


def sunset_warning(version: str, today: date) -> str:
    """Korean warning from 60 days before the version's sunset, else ""."""
    sunset = version_sunset(version)
    if not sunset:
        return ""
    days = (date.fromisoformat(sunset) - today).days
    if days > SUNSET_WARNING_DAYS:
        return ""
    if days < 0:
        return f"LinkedIn API 버전 {version}은 {sunset}에 끝났어요. INSIA를 업데이트해 주세요."
    return f"LinkedIn API 버전 {version}이 {sunset}에 끝나요. INSIA를 업데이트해 주세요."


def permalink_for(urn: str, *, fake: bool = False) -> str:
    if not urn:
        return ""
    if fake:
        return f"{FAKE_PERMALINK_BASE}/linkedin/feed/update/{urn}/"
    return f"https://www.linkedin.com/feed/update/{urn}/"


def post_body(sub: str, commentary: str, visibility: str) -> dict[str, Any]:
    return {
        "author": f"urn:li:person:{sub}",
        "commentary": commentary,
        "visibility": visibility,
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }


def _current_draft(detail: ContentItemDetail):
    version = detail.item.version
    for candidate in reversed(detail.versions):
        if candidate.version == version:
            return candidate.draft
    return detail.versions[-1].draft if detail.versions else None


def _error_fields(body: Any) -> tuple[str, str]:
    if isinstance(body, dict):
        return str(body.get("code") or body.get("serviceErrorCode") or ""), str(body.get("message") or "")
    return "", ""


class LinkedInPublisher:
    """``Publisher`` for LinkedIn (member profile text posts)."""

    platform = "linkedin"

    def __init__(self, service: PublishService) -> None:
        self.service = service

    @property
    def settings(self):
        return self.service.settings

    def readiness(self):
        return self.service.readiness("linkedin")

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}", "Linkedin-Version": self.settings.linkedin_version,
                "X-Restli-Protocol-Version": RESTLI_PROTOCOL}

    # -- preview -----------------------------------------------------------------
    def account(self) -> tuple[str, str]:
        """``(sub, name)`` of the connected member, read fresh from ``/v2/userinfo`` (names are never stored).
        Raises ``ReconnectRequiredError`` (401 / a different member) or ``PlatformError``."""
        token, sub = self.service.linkedin_token()
        try:
            status, body = fetch_userinfo(self.service.transport, token)
        except TransportError:
            raise PlatformError(MSG_NO_NETWORK.replace(" 글은 올라가지 않았어요.", ""), platform="linkedin") from None
        if status == 401:
            self.service.mark_reconnect("linkedin", "LinkedIn이 토큰을 받지 않았어요 (401).")
            raise ReconnectRequiredError(platform="linkedin")
        if status != 200 or not body.get("sub"):
            raise PlatformError(f"LinkedIn 계정 정보를 읽지 못했어요 (HTTP {status}).", platform="linkedin",
                                platform_status=status)
        if str(body["sub"]) != sub:
            raise ReconnectRequiredError("연결된 계정이 바뀌었어요. 다시 연결해 주세요.", platform="linkedin")
        name = str(body.get("name") or "").strip()
        self.service.remember_linkedin_name(name)
        return sub, name

    def build_preview(self, detail: ContentItemDetail, profile: Profile | None, options: PreviewOptions) -> PreviewDraft:
        assert isinstance(options, LinkedInPreviewOptions)
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        notices: list[dict[str, str]] = []
        draft = _current_draft(detail)
        body, tags, had_bold = "", [], False
        if draft is None:
            errors.append(ValidationIssue("error", "empty", "게시할 본문이 없어요."))
        else:
            try:
                body, tags, had_bold = split_text(paste_text(draft))
            except ExportError:
                body = ""
            if not body.strip():
                errors.append(ValidationIssue("error", "empty", "LinkedIn 본문이 비어 있어요. 초안을 채운 뒤 다시 승인해 주세요."))
        visible = visible_text(body, tags)
        mode = self.settings.linkedin_hashtags
        commentary = build_commentary(body, tags, mode)
        chars = len(visible)
        if chars > LINKEDIN_MAX_CHARS:
            errors.append(ValidationIssue("error", "too_long",
                                          f"글이 {chars:,}자라 LinkedIn 한도({LINKEDIN_MAX_CHARS:,}자)를 넘어요. 줄여서 다시 승인해 주세요."))
        elif len(visible.encode("utf-16-le")) // 2 > LINKEDIN_MAX_CHARS or len(commentary) > LINKEDIN_MAX_CHARS:
            warnings.append(ValidationIssue("warning", "count_method",
                                            "LinkedIn이 글자를 세는 방식에 따라 끝부분이 잘릴 수 있어요."))
        keep = profile.required_phrases if profile is not None else []
        placeholders = find_placeholders(visible, keep=keep)
        if "○○" in visible and "○○" not in placeholders:
            placeholders.append("○○")
        if placeholders:
            shown = ", ".join(placeholders[:3]) + (f" 외 {len(placeholders) - 3}개" if len(placeholders) > 3 else "")
            errors.append(ValidationIssue("error", "placeholder",
                                          f"자리표시가 남아 있어요: {shown}. 편집해서 채운 뒤 다시 승인해 주세요."))
        if had_bold:
            warnings.append(ValidationIssue("warning", "bold", "굵게 표시(**)를 빼고 보내요."))
        if draft is not None:
            for check in check_format(draft, detail.brief, profile):
                if check.passed:
                    continue
                if check.id == "no_link":
                    message = "본문에 링크가 있어요. LinkedIn 운영 기준은 링크를 첫 댓글에 두는 거예요."
                else:
                    message = f"{check.label}: {check.value} (기준 {check.expected})"
                warnings.append(ValidationIssue("warning", f"format_{check.id}", message))
        item = detail.item
        if item.approval_forced:
            score = f"{item.approved_score}점" if item.approved_score is not None else "검수 없음"
            warnings.append(ValidationIssue("warning", "forced_approval",
                                            f"검수를 통과하지 않은 버전({score})을 그래도 승인했어요."))
        first_link = ""
        if draft is not None:
            for entry in draft.change_log:
                match = _FIRST_COMMENT.search(entry or "")
                if match:
                    first_link = match.group(1).rstrip(".,)")
                    break
        notices.append({"code": "single_post", "message": "지금 이 글 한 건만 올려요. INSIA는 예약·반복 게시를 하지 않아요. "
                                                          "LinkedIn API 이용약관이 자동 게시를 금지하기 때문이에요."})
        notices.append({"code": "edit_on_platform", "message": "올린 뒤 고치거나 지우려면 LinkedIn에서 직접 해야 해요."})
        notices.append({"code": "manual_done", "message": "이미 LinkedIn에 직접 올렸다면 여기서 게시하지 말고 ‘게시 완료 표시’를 눌러 주세요."})
        if first_link:
            notices.append({"code": "first_comment",
                            "message": f"링크는 본문에 넣지 않았어요. 게시한 뒤 첫 댓글로 직접 달아 주세요: {first_link}"})
        sub, name = self.account()
        account = {"name": name, "kind": ACCOUNT_KIND, "id_hint": "…" + sub[-3:]}
        request = post_body(sub, commentary, options.visibility)
        request["author"] = "urn:li:person:" + account["id_hint"]
        return PreviewDraft(
            platform="linkedin", account_id=sub, account=account, api_version=self.settings.linkedin_version,
            options=options.to_json(), payload={"commentary": commentary, "text": visible},
            content={"text": visible, "chars": chars, "limit": LINKEDIN_MAX_CHARS, "hashtags": ["#" + t for t in tags],
                     "options": options.to_json(), "visibility_label": VISIBILITY_LABELS[options.visibility],
                     "hashtag_mode": mode},
            errors=errors, warnings=warnings, notices=notices, first_comment_link=first_link,
            request_preview=[{"method": "POST", "url": POSTS_URL,
                              "headers": {"Authorization": "Bearer ***", "Linkedin-Version": self.settings.linkedin_version,
                                          "X-Restli-Protocol-Version": RESTLI_PROTOCOL,
                                          "Content-Type": "application/json"},
                              "body": request}],
        )

    # -- send ----------------------------------------------------------------------
    def send(self, attempt: PublishAttempt, payload: dict[str, Any], guard: AttemptGuard) -> SendOutcome:
        guard.step("check")
        try:
            token, sub = self.service.linkedin_token()
        except ReconnectRequiredError as exc:
            return SendOutcome("failed", error_code="reconnect", error=str(exc))
        if sub != attempt.account_id:
            return SendOutcome("failed", error_code="account_changed", error=MSG_ACCOUNT_CHANGED)
        block = payload.get("linkedin") or {}
        options = payload.get("options") or {}
        body = post_body(sub, str(block.get("commentary") or ""), str(options.get("visibility") or "PUBLIC"))
        for attempt_no in (1, 2):
            guard.claim_write()
            try:
                response = self.service.transport.request("POST", POSTS_URL, headers=self._headers(token), json_body=body)
            except TransportError as exc:
                if exc.sent == "no":
                    return SendOutcome("failed", error_code="network", error=MSG_NO_NETWORK)
                return SendOutcome("unknown", error_code="timeout", error=MSG_UNKNOWN)
            trace = {"x_li_uuid": response.header("x-li-uuid")[:80]} if response.header("x-li-uuid") else {}
            if response.status == 201:
                urn = response.header("x-restli-id").strip()
                if urn and not _POST_URN.fullmatch(urn) and not urn.startswith("urn:li:"):
                    urn = ""
                return SendOutcome("published", external_id=urn, permalink=permalink_for(urn, fake=self.settings.fake),
                                   state={**trace, **({} if urn else {"permalink_missing": True})})
            code, message = _error_fields(response.json())
            if response.status == 409 and attempt_no == 1:
                log.info("LinkedIn 409 CONFLICT, 한 번 다시 보내요 (%s)", attempt.id)
                continue
            return self._outcome(response.status, code, message, trace)
        return SendOutcome("failed", error_code="409", error=MSG_CONFLICT)  # pragma: no cover - loop always returns

    def _outcome(self, status: int, code: str, message: str, trace: dict[str, Any]) -> SendOutcome:
        error_code = f"{status}/{code}" if code else str(status)
        if status == 422:
            urn = _POST_URN.search(message or "")
            if urn and "duplicate" in (message or "").lower():
                found = urn.group(0)
                return SendOutcome("published", external_id=found, permalink=permalink_for(found, fake=self.settings.fake),
                                   state={**trace, "duplicate": True})
            return SendOutcome("failed", error_code=error_code, state=trace,
                               error=f"LinkedIn이 글을 받지 않았어요({code or 'UNPROCESSABLE_ENTITY'}). 글은 올라가지 않았어요.")
        if status >= 500:
            return SendOutcome("unknown", error_code=error_code, error=MSG_UNKNOWN, state=trace)
        if status == 401:
            self.service.mark_reconnect("linkedin", "LinkedIn이 토큰을 받지 않았어요 (401).")
            return SendOutcome("failed", error_code=error_code, error=MSG_RECONNECT, state={**trace, "reconnect": True})
        if status == 403:
            return SendOutcome("failed", error_code=error_code, error=MSG_FORBIDDEN, state=trace)
        if status == 409:
            return SendOutcome("failed", error_code=error_code, error=MSG_CONFLICT, state=trace)
        if status == 426:
            version = self.settings.linkedin_version
            return SendOutcome("failed", error_code=error_code, state=trace,
                               error=f"LinkedIn API 버전 {version}이 더 이상 지원되지 않아요. INSIA를 업데이트해 주세요. "
                                     "글은 올라가지 않았어요.")
        if status == 429:
            return SendOutcome("failed", error_code=error_code, error=MSG_RATE_LIMITED, state=trace)
        if status == 400:
            if code == "FIELD_LENGTH_TOO_LONG":
                return SendOutcome("failed", error_code=error_code, error=MSG_TOO_LONG, state=trace)
            if code == "VERSION_MISSING":
                return SendOutcome("failed", error_code=error_code, state=trace,
                                   error="INSIA가 LinkedIn 요청에 API 버전을 빠뜨렸어요(INSIA 오류). 글은 올라가지 않았어요.")
            if code in ("INVALID_URN_ID", "INVALID_URN_TYPE"):
                return SendOutcome("failed", error_code=error_code, state=trace,
                                   error="LinkedIn이 계정 정보를 받지 않았어요. 다시 연결해 주세요. 글은 올라가지 않았어요.")
            return SendOutcome("failed", error_code=error_code, state=trace,
                               error=f"LinkedIn이 요청 형식을 받지 않았어요({code or 'HTTP 400'}). INSIA 오류일 수 있어요. "
                                     "글은 올라가지 않았어요.")
        return SendOutcome("failed", error_code=error_code, state=trace,
                           error=f"LinkedIn에서 오류가 났어요(HTTP {status}). 글은 올라가지 않았어요.")

    def reconcile(self, attempt: PublishAttempt, guard: AttemptGuard) -> SendOutcome | None:
        """LinkedIn posts cannot be read back (``r_member_social`` is closed, LI §5): a person resolves."""
        return None


__all__ = [
    "ACCOUNT_KIND", "DEFAULT_VERSION", "HASHTAG_MODE_DEFAULT", "KNOWN_SUNSETS", "LINKEDIN_MAX_CHARS",
    "LINKEDIN_POST_HOSTS", "LITTLE_RESERVED", "POSTS_URL", "LinkedInPublisher", "build_commentary", "hashtags_text",
    "little_escape", "permalink_for", "post_body", "split_text", "sunset_warning", "version_sunset", "visible_text",
]

"""Runtime settings (environment + CLI) and mode resolution."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Mapping

from .channels import DEFAULT_PASS_SCORE

Mode = Literal["auto", "live", "mock"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT: dict[str, Effort] = {"orchestrator": "high", "researcher": "medium", "reviewer": "high"}
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
KST = timezone(timedelta(hours=9))

# Package root: src/insia_agents -> repo root is two levels above ``src``.
PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT_GUESS = PACKAGE_DIR.parent.parent


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _find_dir(env_value: str | None, relative: str) -> Path | None:
    """Locate a repo directory (``web``, ``examples/sample-run``) robustly.

    Order: explicit env override → current working directory → the repo that
    contains this package (editable install / running from source).
    """
    if env_value:
        return Path(env_value).expanduser().resolve()
    for base in (Path.cwd(), REPO_ROOT_GUESS):
        candidate = (base / relative).resolve()
        if candidate.is_dir():
            return candidate
    return None


def today_kst() -> str:
    return datetime.now(KST).date().isoformat()


@dataclass(frozen=True)
class Settings:
    mode: Mode = "auto"
    model: str = DEFAULT_MODEL
    effort: Mapping[str, Effort] = field(default_factory=lambda: dict(DEFAULT_EFFORT))
    fallbacks: bool = True  # server-side refusal fallback (beta server-side-fallback-2026-07-01)
    max_rounds: int = 2
    pass_score: int = DEFAULT_PASS_SCORE
    speed: float = 1.0  # mock only: real seconds slept per simulated second
    out_dir: Path | None = Path("outputs")
    web_dir: Path | None = None
    sample_dir: Path | None = None
    today: str = field(default_factory=today_kst)
    web_search_max_uses: int = 12
    web_fetch_max_uses: int = 10
    max_continuations: int = 5  # pause_turn resumes per call
    max_workers: int = 4  # live mode: channels in parallel

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, **overrides: object) -> "Settings":
        env = os.environ if env is None else env
        effort = dict(DEFAULT_EFFORT)
        for role in effort:
            value = env.get(f"INSIA_EFFORT_{role.upper()}", "").strip().lower()
            if value:
                if value not in EFFORT_LEVELS:
                    raise ValueError(f"INSIA_EFFORT_{role.upper()}={value!r}: {', '.join(EFFORT_LEVELS)} 중 하나여야 해요")
                effort[role] = value  # type: ignore[assignment]
        base = cls(
            mode=(env.get("INSIA_MODE") or "auto").strip().lower() or "auto",  # type: ignore[arg-type]
            model=(env.get("INSIA_MODEL") or DEFAULT_MODEL).strip(),
            effort=effort,
            fallbacks=_truthy(env.get("INSIA_FALLBACKS"), True),
            web_dir=_find_dir(env.get("INSIA_WEB_DIR"), "web"),
            sample_dir=_find_dir(env.get("INSIA_SAMPLE_DIR"), "examples/sample-run"),
            today=(env.get("INSIA_TODAY") or today_kst()).strip(),
        )
        if env.get("INSIA_OUT_DIR"):
            base = replace(base, out_dir=Path(env["INSIA_OUT_DIR"]))
        clean = {k: v for k, v in overrides.items() if v is not None}
        settings = replace(base, **clean) if clean else base
        settings.validate()
        return settings

    def with_options(self, **overrides: object) -> "Settings":
        clean = {k: v for k, v in overrides.items() if v is not None}
        settings = replace(self, **clean) if clean else self
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.mode not in ("auto", "live", "mock"):
            raise ValueError(f"mode={self.mode!r}: auto, live, mock 중 하나여야 해요")
        if not 0 <= self.max_rounds <= 5:
            raise ValueError("max_rounds는 0~5 사이여야 해요")
        if not 0 <= self.pass_score <= 100:
            raise ValueError("pass_score는 0~100 사이여야 해요")
        if not 0 <= self.speed <= 100:
            raise ValueError("speed는 0~100 사이여야 해요 (0 = 기다리지 않음)")


# Fallback copy of examples/sample-run/brief.json (used when the repo's
# examples/ folder is not available, e.g. a non-editable install).
SAMPLE_BRIEF: dict[str, object] = {
    "topic": "1인 창업자·소상공인을 위한 AI 콘텐츠 에이전트 서비스 'INSIA 스마트에이전트'",
    "goal": "예비창업패키지 등 정부 창업지원사업 신청용 사업계획서 초안 작성과 서비스 런칭 홍보 콘텐츠(네이버 블로그·링크드인·인스타그램) 제작",
    "audience": "콘텐츠 마케팅을 혼자 해야 하는 1인 창업자, 소상공인, 초기 스타트업 대표",
    "channels": ["bizplan", "naver_blog", "linkedin", "instagram"],
    "tone": "신뢰감 있고 친근한 전문가 톤. 과장·확정적 표현 금지, 수치는 반드시 출처와 기준 시점 표기",
    "keywords": ["AI 마케팅 자동화", "1인 창업", "사업계획서", "SNS 운영", "AI 에이전트"],
    "notes": ("리서치·총괄·검수 3개 AI 에이전트가 협업해 사업계획서와 채널별 콘텐츠를 만들고 사람이 최종 승인하는 구조. "
              "팀 정보는 [대표자 성명] 같은 자리표시로 남길 것. 가격은 월 구독형(예: 베이직/프로) 가정으로 제안하되 가정임을 명시."),
    "language": "ko",
}


def load_sample_brief(settings: "Settings | None" = None):
    """The sample brief (``examples/sample-run/brief.json`` when present)."""
    from .models import Brief

    sample_dir = settings.sample_dir if settings is not None else _find_dir(os.environ.get("INSIA_SAMPLE_DIR"), "examples/sample-run")
    if sample_dir is not None and (Path(sample_dir) / "brief.json").is_file():
        try:
            return Brief.model_validate_json((Path(sample_dir) / "brief.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return Brief.model_validate(SAMPLE_BRIEF)


def has_credentials(env: Mapping[str, str] | None = None, home: Path | None = None) -> bool:
    """True when the zero-arg ``anthropic.Anthropic()`` client can find credentials."""
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY") or env.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    home = home if home is not None else Path.home()
    return (home / ".config" / "anthropic").is_dir()


def resolve_mode(mode: Mode, env: Mapping[str, str] | None = None, home: Path | None = None) -> tuple[Literal["live", "mock"], str]:
    """Return the concrete mode and a Korean one-line reason for the log."""
    if mode == "live":
        return "live", "live 모드로 실행해요 (Anthropic API 호출)"
    if mode == "mock":
        return "mock", "mock 모드로 실행해요 (API 호출 없이 오프라인 재생)"
    if has_credentials(env, home):
        return "live", "API 자격 증명을 찾아서 live 모드로 실행해요"
    return "mock", "API 키가 없어서 mock 모드로 실행해요 (ANTHROPIC_API_KEY를 설정하면 live 모드)"

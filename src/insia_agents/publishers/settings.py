"""Publishing settings from the environment and ``serve`` options (DESIGN.md 2-1, 3-3; 14.3 overrides).

``PublishSettings.from_env`` never raises for bad values: the server must still start, so a wrong value
becomes a Korean entry in ``warnings`` (and, for the media URL, ``media_valid=False`` + ``media_reason``) and
the default is used. Secrets read here (``INSIA_LINKEDIN_CLIENT_SECRET``, ``INSIA_IG_APP_SECRET``) are kept out
of ``repr`` and must never be logged or returned by an API.

Environment variables (all optional; unset = today's behaviour, nothing changes):

==================================  =====================================================================
``INSIA_PUBLISH``                   ``0``/``false``/``no``/``off`` hides API publishing entirely (default on)
``INSIA_PUBLISH_INSTAGRAM``         ``1`` turns Instagram on (beta, **default off** until live test IG-1)
``INSIA_PUBLISH_FAKE``              ``1`` = fake platform (tests/demo); only in a workspace under the system
                                    temp dir or the one named by ``INSIA_PUBLISH_FAKE_ALLOW``; otherwise the
                                    whole feature is disabled
``INSIA_PUBLISH_FAKE_ALLOW``        absolute workspace path where the fake mode may run
``INSIA_PUBLISH_SKIP_SELF_CHECK``   ``1`` skips the public media URL self-check (hairpin-NAT setups)
``INSIA_LINKEDIN_CLIENT_ID``        LinkedIn app Client ID (env wins; the dashboard shows "환경 변수에서 설정됨")
``INSIA_LINKEDIN_CLIENT_SECRET``    LinkedIn app Client Secret
``INSIA_LINKEDIN_REDIRECT_URI``     redirect URI registered in the LinkedIn app
``INSIA_LINKEDIN_VERSION``          ``Linkedin-Version`` header, ``YYYYMM`` (default ``202609``)
``INSIA_LINKEDIN_HASHTAGS``         ``plain`` (default, DESIGN.md 14.3) or ``template``
``INSIA_IG_API_VERSION``            Instagram Graph API version (default ``v25.0``)
``INSIA_IG_APP_ID`` / ``_SECRET``   Instagram app id/secret — stage-2 OAuth only (unused in v1)
``INSIA_IG_REDIRECT_URI``           stage-2 OAuth only (unused in v1)
``INSIA_MEDIA_BASE_URL``            public ``https://`` origin Instagram fetches images from (``serve --media-base-url``)
``INSIA_MEDIA_PORT``                media-only listener port (``serve --media-port``)
``INSIA_CREDENTIALS_DIR``           where ``secrets.sqlite`` lives (default ``<workspace>/credentials``)
==================================  =====================================================================

``INSIA_LINKEDIN_ENDPOINT`` (2-1) is intentionally not read: the ``ugcPosts`` fallback is not in v1 (14.3).
There is no environment variable for the Instagram access token (a refreshed token has nowhere to go).
"""

from __future__ import annotations

import ipaddress
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from .base import LINKEDIN_CALLBACK_PATH

DEFAULT_LINKEDIN_VERSION = "202609"     # supported until 2027-09-15 (LI §12); linkedin.py keeps the sunset table
DEFAULT_IG_API_VERSION = "v25.0"        # available until 2028-07-29 (IG §0-5)
DEFAULT_LINKEDIN_HASHTAGS: Literal["plain", "template"] = "plain"   # DESIGN.md 14.3 (T4 may flip it)
LINKEDIN_HASHTAG_MODES: tuple[str, ...] = ("plain", "template")
# Instagram stays behind INSIA_PUBLISH_INSTAGRAM=1 until live test IG-1 passes; then flip this one line.
INSTAGRAM_DEFAULT_ENABLED = False
INSTAGRAM_BETA = True
DEFAULT_SERVER_PORT = 8765

PUBLISH_ENV_VARS: tuple[str, ...] = (
    "INSIA_PUBLISH", "INSIA_PUBLISH_INSTAGRAM", "INSIA_PUBLISH_FAKE", "INSIA_PUBLISH_FAKE_ALLOW",
    "INSIA_PUBLISH_SKIP_SELF_CHECK", "INSIA_LINKEDIN_CLIENT_ID", "INSIA_LINKEDIN_CLIENT_SECRET",
    "INSIA_LINKEDIN_REDIRECT_URI", "INSIA_LINKEDIN_VERSION", "INSIA_LINKEDIN_HASHTAGS", "INSIA_IG_API_VERSION",
    "INSIA_IG_APP_ID", "INSIA_IG_APP_SECRET", "INSIA_IG_REDIRECT_URI", "INSIA_MEDIA_BASE_URL", "INSIA_MEDIA_PORT",
    "INSIA_CREDENTIALS_DIR",
)

DISABLED_MESSAGE = "API 게시가 꺼져 있어요 (INSIA_PUBLISH=0)."
FAKE_BLOCKED_MESSAGE = "가짜 게시 모드는 임시 워크스페이스에서만 쓸 수 있어요 (INSIA_PUBLISH_FAKE)."
INSTAGRAM_BETA_OFF_MESSAGE = "인스타그램 API 게시는 아직 시험 중이라 꺼져 있어요. 켜는 방법은 운영 안내를 봐 주세요."
MEDIA_IN_PUBLIC_HOSTS_WARNING = "미디어 주소가 대시보드 허용 도메인에도 들어 있어요."

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_LINKEDIN_VERSION = re.compile(r"^\d{6}$")
_IG_VERSION = re.compile(r"^v\d{1,3}\.\d{1,2}$")
LOOPBACK_NAMES = frozenset({"localhost", "127.0.0.1", "::1"})

MediaMode = Literal["listener", "main", "none"]


def _flag(env: Mapping[str, str], name: str, default: bool, warnings: list[str]) -> bool:
    raw = (env.get(name) or "").strip().lower()
    if not raw:
        return default
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    warnings.append(f"{name}={raw!r}은(는) 알 수 없는 값이라 기본값을 써요 (1 또는 0으로 적어 주세요).")
    return default


def _is_loopback_host(host: str) -> bool:
    name = (host or "").strip("[]").lower().rstrip(".")
    if name in LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def is_temp_workspace(home: Path | str) -> bool:
    """True when the workspace lies under the system temp dir (``tempfile.gettempdir()``) — where the fake mode may run."""
    try:
        return Path(home).expanduser().resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())
    except (OSError, ValueError):
        return False


def check_redirect_uri(uri: str) -> str:
    """Why a LinkedIn redirect URI is not acceptable (Korean), or ``""`` when it is (DESIGN.md 3-2).

    Absolute URL, no ``#fragment``; ``http://`` only for ``localhost``/``127.0.0.1``/``[::1]``, otherwise
    ``https://``. (Whether LinkedIn's portal accepts a given http value is live test T3.)
    """
    value = (uri or "").strip()
    if not value:
        return "Redirect URI가 비어 있어요."
    if not value.isascii() or any(ch.isspace() for ch in value):
        return "Redirect URI에는 영문·숫자로 된 주소만 쓸 수 있어요 (띄어쓰기 없이)."
    try:
        parts = urlsplit(value)
        host = parts.hostname or ""
        parts.port  # noqa: B018 - raises ValueError for a bad port
    except ValueError:
        return "Redirect URI 형식이 올바르지 않아요."
    if parts.scheme not in ("http", "https") or not host:
        return "Redirect URI는 http:// 또는 https://로 시작하는 전체 주소여야 해요."
    if parts.fragment or "#" in value:
        return "Redirect URI에는 #을 쓸 수 없어요."
    if parts.scheme == "http" and host.lower() not in LOOPBACK_NAMES:
        return "http:// 주소는 localhost·127.0.0.1·[::1]에서만 쓸 수 있어요. 그 밖에는 https:// 주소를 써 주세요."
    return ""


def default_redirect_uri(public_hosts: Sequence[str] = (), *, trust_proxy: bool = False,
                         server_port: int = DEFAULT_SERVER_PORT) -> str:
    """Suggested LinkedIn redirect URI: ``https://<host>/oauth/linkedin/callback`` when exactly one public host is
    served behind a trusted proxy, else ``http://localhost:<port>/oauth/linkedin/callback``."""
    hosts = [h for h in public_hosts if h]
    if len(hosts) == 1 and trust_proxy:
        host = hosts[0]
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"https://{host}{LINKEDIN_CALLBACK_PATH}"
    return f"http://localhost:{int(server_port or DEFAULT_SERVER_PORT)}{LINKEDIN_CALLBACK_PATH}"


def check_media_base_url(url: str) -> tuple[str, str]:
    """Normalize the public media URL. Returns ``(normalized, reason)``: ``reason`` is ``""`` when usable.

    It must be an ASCII ``https://host[:port]`` origin (no path, query, fragment or user info — the listener
    serves ``/pub/m/…`` at the root) that the internet can reach (not localhost / a private IP literal).
    """
    value = (url or "").strip()
    if not value:
        return "", ""
    if not value.isascii() or any(ch.isspace() for ch in value):
        return "", "미디어 주소는 영문·숫자로 된 https:// 주소여야 해요 (예: https://media.example.com)."
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return "", "미디어 주소 형식이 올바르지 않아요 (예: https://media.example.com)."
    if parts.scheme.lower() != "https":
        return "", "미디어 주소가 https가 아니에요."
    if not host:
        return "", "미디어 주소 형식이 올바르지 않아요 (예: https://media.example.com)."
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
        return "", "미디어 주소에는 경로 없이 https://도메인만 적어 주세요 (예: https://media.example.com)."
    literal = host.strip("[]")
    try:
        ip = ipaddress.ip_address(literal)
    except ValueError:
        ip = None
    if _is_loopback_host(host) or (ip is not None and not ip.is_global):
        return "", "미디어 주소가 이 컴퓨터나 내부망을 가리켜요. 인스타그램이 인터넷에서 가져갈 수 있는 도메인이어야 해요."
    shown = f"[{literal}]" if ip is not None and ip.version == 6 else host
    return f"https://{shown}{f':{port}' if port else ''}", ""


def _parse_port(raw: Any, name: str, warnings: list[str]) -> tuple[int | None, str]:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None, ""
    try:
        port = int(str(raw).strip())
    except ValueError:
        port = -1
    if not 0 <= port <= 65535:
        reason = f"{name} 값이 올바르지 않아요 (1~65535 사이의 숫자)."
        warnings.append(reason)
        return None, reason
    return port, ""


@dataclass(frozen=True)
class PublishSettings:
    """Effective publishing configuration for one workspace + server/CLI process."""

    home: Path                              # the workspace (Settings.home)
    credentials_dir: Path                   # INSIA_CREDENTIALS_DIR or <home>/credentials (0700)
    publish_dir: Path                       # <home>/publish (staging/, public/)
    enabled: bool = True                    # False: INSIA_PUBLISH=0 or the fake mode was refused
    disabled_reason: str = ""               # Korean, when not enabled
    fake: bool = False                      # effective fake platform mode (published_via='fake')
    fake_requested: bool = False            # INSIA_PUBLISH_FAKE=1 (even when refused — doctor warns)
    instagram_enabled: bool = False         # enabled and (INSIA_PUBLISH_INSTAGRAM=1 or INSTAGRAM_DEFAULT_ENABLED)
    instagram_enabled_by: str = ""          # "env" | "default" | "" (off)
    linkedin_client_id: str = ""            # from env only ("" = use the workspace's stored value)
    linkedin_client_secret: str = field(default="", repr=False)
    linkedin_redirect_uri: str = ""         # from env only
    linkedin_redirect_uri_default: str = ""  # derived suggestion (default_redirect_uri)
    linkedin_version: str = DEFAULT_LINKEDIN_VERSION
    linkedin_hashtags: Literal["plain", "template"] = DEFAULT_LINKEDIN_HASHTAGS
    ig_api_version: str = DEFAULT_IG_API_VERSION
    ig_app_id: str = ""
    ig_app_secret: str = field(default="", repr=False)
    ig_redirect_uri: str = ""
    media_base_url: str = ""                # normalized https origin, "" when unset/invalid
    media_port: int | None = None
    media_mode: MediaMode = "none"          # listener (config A) | main (config B) | none
    media_valid: bool = False
    media_reason: str = ""                  # Korean, when not valid
    public_hosts: tuple[str, ...] = ()
    server_port: int = DEFAULT_SERVER_PORT
    trust_proxy: bool = False
    skip_self_check: bool = False
    warnings: tuple[str, ...] = ()          # Korean: ignored env values, media host also in public_hosts …

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, home: Path | str | None = None, *,
                 public_hosts: Sequence[str] = (), server_port: int = DEFAULT_SERVER_PORT,
                 media_port: int | None = None, media_base_url: str | None = None,
                 trust_proxy: bool = False) -> "PublishSettings":
        """Read the environment (default ``os.environ``). ``home`` defaults to ``INSIA_HOME`` or ``./workspace``.

        ``public_hosts`` / ``server_port`` / ``trust_proxy`` are the server's (already validated) values;
        ``media_port`` / ``media_base_url`` are ``serve --media-port`` / ``--media-base-url`` and win over
        ``INSIA_MEDIA_PORT`` / ``INSIA_MEDIA_BASE_URL``. Never raises for bad environment values.
        """
        env = os.environ if env is None else env
        warnings: list[str] = []
        get = lambda name: (env.get(name) or "").strip()  # noqa: E731

        if home is None:
            home = get("INSIA_HOME") or "workspace"
        home_path = Path(home).expanduser()
        credentials_dir = Path(get("INSIA_CREDENTIALS_DIR")).expanduser() if get("INSIA_CREDENTIALS_DIR") \
            else home_path / "credentials"
        hosts = tuple(dict.fromkeys(h.strip().lower().rstrip(".") for h in public_hosts if h and h.strip()))

        enabled = _flag(env, "INSIA_PUBLISH", True, warnings)
        disabled_reason = "" if enabled else DISABLED_MESSAGE
        fake_requested = _flag(env, "INSIA_PUBLISH_FAKE", False, warnings)
        fake = False
        if fake_requested and enabled:
            allow = get("INSIA_PUBLISH_FAKE_ALLOW")
            allowed = False
            if allow:
                try:
                    allowed = Path(allow).expanduser().resolve() == home_path.resolve()
                except OSError:
                    allowed = False
            if allowed or is_temp_workspace(home_path):
                fake = True
            else:
                enabled, disabled_reason = False, FAKE_BLOCKED_MESSAGE

        ig_flag = _flag(env, "INSIA_PUBLISH_INSTAGRAM", INSTAGRAM_DEFAULT_ENABLED, warnings)
        instagram_enabled = enabled and ig_flag
        instagram_enabled_by = ("env" if get("INSIA_PUBLISH_INSTAGRAM") else "default") if instagram_enabled else ""

        linkedin_version = get("INSIA_LINKEDIN_VERSION") or DEFAULT_LINKEDIN_VERSION
        if not _LINKEDIN_VERSION.match(linkedin_version):
            warnings.append(f"INSIA_LINKEDIN_VERSION={linkedin_version!r}은(는) YYYYMM 형식이 아니라 "
                            f"{DEFAULT_LINKEDIN_VERSION}을(를) 써요.")
            linkedin_version = DEFAULT_LINKEDIN_VERSION
        hashtags = get("INSIA_LINKEDIN_HASHTAGS").lower() or DEFAULT_LINKEDIN_HASHTAGS
        if hashtags not in LINKEDIN_HASHTAG_MODES:
            warnings.append(f"INSIA_LINKEDIN_HASHTAGS={hashtags!r}은(는) plain 또는 template이어야 해서 "
                            f"{DEFAULT_LINKEDIN_HASHTAGS}을(를) 써요.")
            hashtags = DEFAULT_LINKEDIN_HASHTAGS
        ig_version = get("INSIA_IG_API_VERSION") or DEFAULT_IG_API_VERSION
        if not _IG_VERSION.match(ig_version):
            warnings.append(f"INSIA_IG_API_VERSION={ig_version!r}은(는) v25.0 같은 형식이 아니라 "
                            f"{DEFAULT_IG_API_VERSION}을(를) 써요.")
            ig_version = DEFAULT_IG_API_VERSION

        redirect_env = get("INSIA_LINKEDIN_REDIRECT_URI")
        if redirect_env:
            why = check_redirect_uri(redirect_env)
            if why:
                warnings.append(f"INSIA_LINKEDIN_REDIRECT_URI: {why}")

        # Media: config A (listener) / config B (main port) / none (DESIGN.md 2-1, 3-3).
        if media_port is not None:
            port, port_reason = _parse_port(media_port, "--media-port", warnings)
        else:
            port, port_reason = _parse_port(get("INSIA_MEDIA_PORT") or None, "INSIA_MEDIA_PORT", warnings)
        raw_url = media_base_url if media_base_url is not None else get("INSIA_MEDIA_BASE_URL")
        media_url, url_reason = check_media_base_url(raw_url or "")
        media_host = urlsplit(media_url).hostname or "" if media_url else ""
        mode: MediaMode = "none"
        valid = False
        reason = ""
        if port is not None and port and server_port and port == int(server_port):
            reason = "미디어 포트는 대시보드 포트와 달라야 해요."
            warnings.append(reason)
            port = None
        if url_reason:
            reason = url_reason
            warnings.append(url_reason)
        elif port_reason and not reason:
            reason = port_reason
        elif not media_url:
            reason = reason or "미디어 공개 주소(INSIA_MEDIA_BASE_URL / --media-base-url)가 없어요."
        elif port is not None:
            mode, valid, reason = "listener", True, ""
            if media_host in hosts:
                warnings.append(MEDIA_IN_PUBLIC_HOSTS_WARNING)
        elif media_host in hosts:
            mode, valid, reason = "main", True, ""
        else:
            reason = reason or ("미디어 전용 포트(--media-port / INSIA_MEDIA_PORT)를 함께 정해 주세요. "
                                "대시보드는 그대로 두고 이미지 포트만 바깥에 연결하면 돼요.")

        return cls(
            home=home_path, credentials_dir=credentials_dir, publish_dir=home_path / "publish",
            enabled=enabled, disabled_reason=disabled_reason, fake=fake, fake_requested=fake_requested,
            instagram_enabled=instagram_enabled, instagram_enabled_by=instagram_enabled_by,
            linkedin_client_id=get("INSIA_LINKEDIN_CLIENT_ID"), linkedin_client_secret=get("INSIA_LINKEDIN_CLIENT_SECRET"),
            linkedin_redirect_uri=redirect_env,
            linkedin_redirect_uri_default=default_redirect_uri(hosts, trust_proxy=trust_proxy, server_port=server_port),
            linkedin_version=linkedin_version, linkedin_hashtags=hashtags,  # type: ignore[arg-type]
            ig_api_version=ig_version, ig_app_id=get("INSIA_IG_APP_ID"), ig_app_secret=get("INSIA_IG_APP_SECRET"),
            ig_redirect_uri=get("INSIA_IG_REDIRECT_URI"),
            media_base_url=media_url, media_port=port, media_mode=mode, media_valid=valid, media_reason=reason,
            public_hosts=hosts, server_port=int(server_port or 0), trust_proxy=bool(trust_proxy),
            skip_self_check=_flag(env, "INSIA_PUBLISH_SKIP_SELF_CHECK", False, warnings),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    # -- derived values ---------------------------------------------------------

    @property
    def linkedin_app_from_env(self) -> bool:
        """The LinkedIn app is defined by environment variables (read-only in the dashboard / CLI)."""
        return bool(self.linkedin_client_id)

    @property
    def env_configured(self) -> bool:
        """The environment alone makes publishing "configured" (``INSIA_LINKEDIN_CLIENT_ID`` or Instagram turned on).
        The service ORs this with stored app info / connections for ``GET /api/publish`` ``configured``."""
        return self.enabled and (bool(self.linkedin_client_id) or self.instagram_enabled)

    @property
    def media_host(self) -> str:
        return (urlsplit(self.media_base_url).hostname or "") if self.media_base_url else ""

    def platform_enabled(self, platform: str) -> bool:
        """Whether a platform is switched on at all (LinkedIn follows ``enabled``; Instagram also needs its beta flag)."""
        if platform == "linkedin":
            return self.enabled
        if platform == "instagram":
            return self.instagram_enabled
        return False

    def linkedin_redirect_uri_for(self, stored: str = "") -> str:
        """Effective redirect URI: environment → value stored in the workspace → derived default."""
        return self.linkedin_redirect_uri or (stored or "").strip() or self.linkedin_redirect_uri_default

    def media_json(self) -> dict[str, Any]:
        """The ``media`` block of ``GET /api/publish`` (DESIGN.md 6-2)."""
        return {"url": self.media_base_url, "mode": self.media_mode,
                "port": self.media_port if self.media_mode == "listener" else None,
                "valid": self.media_valid, "reason": self.media_reason}


__all__ = [
    "DEFAULT_IG_API_VERSION", "DEFAULT_LINKEDIN_HASHTAGS", "DEFAULT_LINKEDIN_VERSION", "DEFAULT_SERVER_PORT",
    "DISABLED_MESSAGE", "FAKE_BLOCKED_MESSAGE", "INSTAGRAM_BETA", "INSTAGRAM_BETA_OFF_MESSAGE",
    "INSTAGRAM_DEFAULT_ENABLED", "LINKEDIN_HASHTAG_MODES", "MEDIA_IN_PUBLIC_HOSTS_WARNING", "PUBLISH_ENV_VARS",
    "MediaMode", "PublishSettings", "check_media_base_url", "check_redirect_uri", "default_redirect_uri",
    "is_temp_workspace",
]

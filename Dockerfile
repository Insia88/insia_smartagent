# syntax=docker/dockerfile:1
# INSIA 스마트에이전트 — 대시보드 서버 이미지 (자세한 안내: docs/operations.md "설치 — Docker")
#
#   docker build -t insia-smartagent .                              # 기본: Word 내보내기 + PDF/DOCX 자료 읽기
#   docker build --build-arg WITH_RENDER=1 -t insia-smartagent .    # + 인스타그램 카드뉴스 PNG (Chromium, 이미지가 커져요)
#
# 데이터(워크스페이스)는 /data 볼륨에 쌓여요. 0.0.0.0으로 열리므로 INSIA_ACCESS_TOKEN이 꼭 필요해요.
# docker run으로 직접 띄울 때는 --init과 --stop-timeout 30을 붙여 주세요(docker-compose.yml은 이미 설정돼 있어요):
# 멈출 때 SIGTERM을 받으면 진행 중인 실행을 멈추고 저장한 뒤 끝나요(최대 20초).
#
# API 게시(선택, docs/operations.md "12. API 게시"): 토큰은 /data/credentials/(0700·0600, uid 10001)에만 저장되고
# insia backup은 이 폴더를 빼요. 인스타그램은 카드 이미지를 JPEG로 그려야 해서 WITH_RENDER=1이 필요하고, 이미지 전용
# 포트(INSIA_MEDIA_PORT, 예: 8766)는 serve와 같은 --host(컨테이너 안에서는 0.0.0.0)에 열리니 호스트에는
# 127.0.0.1:8766으로만 게시해 주세요. 게시는 사람이 대시보드에서 확인하고 누를 때만 해요(자동·예약 게시 없음).
FROM python:3.11-slim

ARG WITH_RENDER=0

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    INSIA_HOME=/data \
    INSIA_OUT_DIR=/data/outputs \
    INSIA_WEB_DIR=/app/web \
    INSIA_SAMPLE_DIR=/app/examples/sample-run

WORKDIR /app

# Package first so the dependency layer is reused when only web/ or examples/ change.
COPY pyproject.toml README.md ./
COPY src ./src
RUN extras="export,docs"; \
    if [ "$WITH_RENDER" = "1" ]; then extras="$extras,render"; fi; \
    pip install --no-cache-dir ".[$extras]"

# Optional: Chromium + Korean fonts for real 1080x1350 card-news PNGs (otherwise slides.html is exported).
RUN if [ "$WITH_RENDER" = "1" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends fonts-noto-cjk \
        && python -m playwright install --with-deps chromium \
        && rm -rf /var/lib/apt/lists/*; \
    fi

COPY web ./web
COPY examples ./examples

# Non-root user; /data is the workspace volume (insia.db, exports/, uploads/, logs/, outputs/,
# and with API publishing credentials/ (0700, never in backups) and publish/ (short-lived images)).
RUN groupadd --system --gid 10001 insia \
    && useradd --system --uid 10001 --gid insia --home-dir /home/insia --create-home --shell /usr/sbin/nologin insia \
    && mkdir -p /data \
    && chown insia:insia /data

USER insia
VOLUME ["/data"]
EXPOSE 8765
# 8766: optional media-only listener for Instagram API publishing (INSIA_MEDIA_PORT); publish it on 127.0.0.1 only.

# 200 (or 401 when the token did not match) from /api/health = alive. Uses INSIA_ACCESS_TOKEN when set.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["insia", "healthcheck", "--quiet"]

STOPSIGNAL SIGTERM
CMD ["insia", "serve", "--host", "0.0.0.0", "--port", "8765"]

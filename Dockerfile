# syntax=docker/dockerfile:1
# INSIA 스마트에이전트 — 대시보드 서버 이미지 (자세한 안내: docs/operations.md "설치 — Docker")
#
#   docker build -t insia-smartagent .                              # 기본: Word 내보내기 + PDF/DOCX 자료 읽기
#   docker build --build-arg WITH_RENDER=1 -t insia-smartagent .    # + 인스타그램 카드뉴스 PNG (Chromium, 이미지가 커져요)
#
# 데이터(워크스페이스)는 /data 볼륨에 쌓여요. 0.0.0.0으로 열리므로 INSIA_ACCESS_TOKEN이 꼭 필요해요.
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

# Non-root user; /data is the workspace volume (insia.db, exports/, uploads/, logs/, outputs/).
RUN groupadd --system --gid 10001 insia \
    && useradd --system --uid 10001 --gid insia --home-dir /home/insia --create-home --shell /usr/sbin/nologin insia \
    && mkdir -p /data \
    && chown insia:insia /data

USER insia
VOLUME ["/data"]
EXPOSE 8765

# 200 (or 401 when the token did not match) from /api/health = alive. Uses INSIA_ACCESS_TOKEN when set.
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD ["insia", "healthcheck", "--quiet"]

CMD ["insia", "serve", "--host", "0.0.0.0", "--port", "8765"]

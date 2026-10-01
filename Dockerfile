# syntax=docker/dockerfile:1
# Built by home-infra's scripts/build-ship.sh:
#   git archive HEAD | docker buildx build --platform linux/arm64 --target production ...
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

FROM base AS build
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON_PREFERENCE=only-system
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM base AS production
RUN useradd --uid 10001 --user-group --no-create-home --shell /usr/sbin/nologin joshua \
 && mkdir /data && chown 10001:10001 /data
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH \
    DB_PATH=/data/joshua.db
USER 10001:10001
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=10s --start-period=180s --retries=3 CMD ["joshua", "healthcheck"]
CMD ["joshua", "run"]

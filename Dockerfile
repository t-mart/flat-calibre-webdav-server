# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.14-trixie-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve from the lockfile alone, so this layer is cached until
# pyproject.toml or uv.lock actually change.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.14-slim-trixie AS runtime

# Deliberately left owned by root and world-readable: that is what lets the image
# run under `--user` as an arbitrary id with no /etc/passwd entry. Nothing in
# here ever needs to be writable, and the app cannot rewrite its own code.
COPY --from=builder /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CW_LIBRARY_ROOT=/library \
    CW_HOST=0.0.0.0 \
    CW_PORT=8080

# A default only. Whoever runs this has to be able to read the library on the
# host, so the id is the operator's call: override with `docker run --user`.
# Numeric, and no matching /etc/passwd entry is needed.
USER 1000:1000
EXPOSE 8080

ENTRYPOINT ["calibre-webdav"]

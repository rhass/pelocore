# Runtime base is swappable for hardened variants (see README):
#   docker build --build-arg BASE_IMAGE=dhi.io/python:3.13 .
ARG BASE_IMAGE=python:3.13-slim
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23

# ---- uv provider -----------------------------------------------------------
FROM ${UV_IMAGE} AS uv

# ---- builder ---------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

# git is needed only in the builder: pylotoncycle is a git-pinned dependency.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies first so source changes don't invalidate the layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# --no-editable installs the project itself into the venv for runtime use.
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------
FROM ${BASE_IMAGE} AS runtime

COPY --from=builder /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PELOCORE_STATE_PATH=/data/state.json

RUN useradd --create-home --uid 10001 pelocore \
    && mkdir -p /data \
    && chown pelocore:pelocore /data

USER pelocore
WORKDIR /app
EXPOSE 8080
VOLUME /data

# No curl in slim images; probe with the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["pelocore"]
CMD ["run"]

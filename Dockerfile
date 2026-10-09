# Multi-stage build on Chainguard's minimal Python.
#
# Builder: cgr.dev/chainguard/python:latest-dev (bash, apk, networked apk repo).
# Runtime: cgr.dev/chainguard/python:latest (nonroot uid 65532, no shell, no
# pip). The uv venv symlinks to the builder's interpreter, so the runtime must
# ship Python at the same path; the dev and runtime variants are built from the
# same package set and do, and the version assert below fails fast if latest
# ever floats to a new minor. The runtime has no shell, so it contains no RUN
# instructions: all setup happens in the builder and is copied in.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.23

# ---- uv provider -----------------------------------------------------------
FROM ${UV_IMAGE} AS uv

# ---- builder ---------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest-dev AS builder

ARG PYTHON_VERSION=3.14

# The dev variant defaults to uid 65532; flip to root for system-level setup.
USER root
RUN test "$(python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')" = "${PYTHON_VERSION}"

# The dev variant ships bash; pin it so RUNs do not depend on /bin/sh.
SHELL ["/bin/bash", "-c"]

COPY --from=uv /uv /usr/local/bin/uv

WORKDIR /app
# git is needed only in the builder: pylotoncycle is a git-pinned dependency.
# The builder is discarded at build end and never becomes part of the image.
# /app must be writable by the non-root venv owner, and /data is created here
# (the runtime has no shell, so /data cannot be set up there).
RUN apk add --no-cache git \
    && chown 65532:65532 /app \
    && mkdir -p /data && chown 65532:65532 /data
USER 65532

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON_DOWNLOADS=never

# Install dependencies first so source changes don't invalidate the layer.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# --no-editable installs the project itself into the venv for runtime use.
RUN uv sync --frozen --no-dev --no-editable

# ---- runtime ---------------------------------------------------------------
FROM cgr.dev/chainguard/python:latest AS runtime

COPY --from=builder /app/.venv /app/.venv
COPY --from=builder --chown=65532:65532 /data /data

ENV PATH="/app/.venv/bin:$PATH" \
    PELOCORE_STATE_PATH=/data/state.json

USER 65532
WORKDIR /app
EXPOSE 8080
VOLUME /data

# No shell in the runtime; probe with the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)"]

ENTRYPOINT ["pelocore"]
CMD ["run"]

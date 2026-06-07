# syntax=docker/dockerfile:1.7
#
# Backend image — Python 3.12 slim + uv-managed deps.
#
# Two stages:
#
#   builder  — installs deps into a venv. Uses ``uv sync`` against the
#              committed lockfile so the image is reproducible: every
#              ``docker build`` in this repo resolves to the same
#              wheels regardless of when Docker Hub was last updated.
#
#   runtime  — copies the venv + the ``copilot`` package only. No uv,
#              no build deps, no test fixtures. Lands at ~250 MB.
#
# The image runs as a non-root user; Fly.io's checker flags root-user
# containers and the cost of switching is essentially zero.

ARG PYTHON_VERSION=3.12-slim

FROM python:${PYTHON_VERSION} AS builder

# Bring uv in from its own image to avoid pinning a pip version here.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Install the locked deps first — copying just the lockfile bits keeps
# this layer cached across source-only changes.
COPY pyproject.toml uv.lock README.md ./
COPY apps/api ./apps/api
RUN uv sync --frozen --no-dev


FROM python:${PYTHON_VERSION} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

# Non-root user. Pinned UID so volume mounts and Fly's checker behave.
RUN groupadd --gid 10001 app && \
    useradd --uid 10001 --gid app --create-home --shell /sbin/nologin app

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --chown=app:app apps/api/copilot ./copilot
# Semantic layer needs ``data/semantic.yml`` at runtime; ship it next to
# the package so ``_default_yaml_path`` finds it via the ancestor walk
# (``/app/data/semantic.yml``). Override per-deploy with
# ``SEMANTIC_YAML_PATH``.
COPY --chown=app:app data/semantic.yml ./data/semantic.yml

USER app

EXPOSE 8000

# Healthcheck has the same intent as the /health endpoint we ship —
# Fly will use this when ``[checks]`` in fly.toml is configured but
# it's also handy for plain ``docker run``.
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health',timeout=2).status==200 else 1)"

CMD ["uvicorn", "copilot.main:app", "--host", "0.0.0.0", "--port", "8000"]

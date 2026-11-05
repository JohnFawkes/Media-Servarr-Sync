# syntax=docker/dockerfile:1
FROM python:3.15-slim AS base

# ── System deps ──────────────────────────────────────────────────────────────
# upgrade picks up security patches for packages already in the base image
# (e.g. liblzma5) that may lag behind the base image's own release date.
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
       curl \
    && rm -rf /var/lib/apt/lists/*

# ── Non-root user ─────────────────────────────────────────────────────────────
RUN groupadd --gid 1000 appuser \
 && useradd  --uid 1000 --gid 1000 --no-create-home appuser

WORKDIR /app

# ── Python deps (cached layer) ────────────────────────────────────────────────
COPY requirements.txt .
# pip is only needed at build time. It vendors its own urllib3, msgpack and
# setuptools (pkg_resources), which image scanners flag even though the app
# never imports them, and even the latest pip ships a vulnerable setuptools.
# So remove pip from the venv and from the base image's system Python once
# the deps are installed.
RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir -r requirements.txt \
    && /app/.venv/bin/pip uninstall -y pip \
    && python -m pip uninstall -y --root-user-action=ignore pip

ENV PATH="/app/.venv/bin:$PATH"

# ── App source ────────────────────────────────────────────────────────────────
COPY media-servarr-sync.py .
COPY templates/ templates/
COPY static/ static/

# Create data directory for persistent storage
RUN mkdir -p /data && chown appuser:appuser /data

# Drop privileges
USER appuser

# ── Runtime ───────────────────────────────────────────────────────────────────
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

ARG PORT=5000
ENV PORT=${PORT}
EXPOSE ${PORT}

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD /bin/sh -c 'curl -sf http://localhost:${PORT:-5000}/health || exit 1'

ENTRYPOINT ["python", "media-servarr-sync.py"]

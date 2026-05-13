# Workspace image used by the Docker sandbox backend.
# Each project session gets one container started from this image; the agent
# `exec`s into it via `sandbox_client.DockerBackend`.
#
# Build:   ./sandbox/build.sh
# Run:     docker run --rm acm-workspace:latest bash -c "python -V && node -v && git --version"

FROM python:3.13-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NODE_MAJOR=20

# System deps + Node 20 from NodeSource.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        git \
        build-essential \
        pkg-config \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Note: this is hardening hygiene, not a real isolation boundary —
# the Docker-socket backend mounts /var/run/docker.sock at the host level, so
# anyone who escapes this user still has Docker access on the host. For real
# multi-tenant isolation switch SANDBOX_BACKEND=e2b. See PROJECT.md.
RUN useradd --create-home --shell /bin/bash --uid 1000 agent \
    && mkdir -p /workspace \
    && chown agent:agent /workspace

USER agent
WORKDIR /workspace

# Default git identity — overridden per-session by sandbox_client when a
# session is repo-linked (uses the user's GitHub username/email).
RUN git config --global user.name  "Agent" \
    && git config --global user.email "agent@local" \
    && git config --global init.defaultBranch main \
    && git config --global advice.detachedHead false

# Keep the container alive so the backend can `docker exec` into it.
CMD ["sleep", "infinity"]

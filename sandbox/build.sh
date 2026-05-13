#!/usr/bin/env bash
# Build the workspace image used by the Docker sandbox backend.
# Usage: ./sandbox/build.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE_TAG="${IMAGE_TAG:-acm-workspace:latest}"

echo "Building ${IMAGE_TAG} from ${SCRIPT_DIR}/Dockerfile ..."
docker build -t "${IMAGE_TAG}" "${SCRIPT_DIR}"

echo
echo "Smoke test:"
docker run --rm "${IMAGE_TAG}" bash -c \
    "echo '  python:' \$(python --version 2>&1) && \
     echo '  node:  ' \$(node --version) && \
     echo '  npm:   ' \$(npm --version) && \
     echo '  git:   ' \$(git --version)"

echo
echo "Built ${IMAGE_TAG}."

#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-tdd-guard:preview}"
OUT="${OUT:-tdd-guard-preview.tar}"

echo "==> release check"
python3 scripts/release_check.py

echo "==> docker build: ${IMAGE}"
docker build --pull --no-cache -t "${IMAGE}" .

echo "==> image inspect"
docker image inspect "${IMAGE}" --format '{{.Id}}  {{.Size}} bytes'

echo "==> export image: ${OUT}"
docker save -o "${OUT}" "${IMAGE}"

echo "==> done"
echo "Image: ${IMAGE}"
echo "Archive: ${OUT}"

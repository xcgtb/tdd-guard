#!/usr/bin/env bash
set -euo pipefail

# 无论从哪里调用，都切到仓库根目录再构建
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-ttd-guard:preview}"
OUT="${OUT:-ttd-guard-preview.tar}"

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

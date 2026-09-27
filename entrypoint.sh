#!/bin/sh
# 默认 PUID/PGID=0，行为和之前完全一样（以 root 运行）。
# 想以非 root 运行时，在 docker-compose.yml 里加：
#   environment:
#     - PUID=1000
#     - PGID=1000
# （具体填 NAS 上挂载目录的属主 uid/gid，用 `id` 命令查）
set -e

PUID="${PUID:-0}"
PGID="${PGID:-0}"

if [ "$PUID" = "0" ] && [ "$PGID" = "0" ]; then
  exec "$@"
fi

if ! getent group "$PGID" >/dev/null 2>&1; then
  groupadd -g "$PGID" appgroup
fi
if ! id -u "$PUID" >/dev/null 2>&1; then
  useradd -u "$PUID" -g "$PGID" -M -s /usr/sbin/nologin appuser
fi

# 只调整 /data（AGENT_DATA，运行时数据），不动 L_ROOT/S_ROOT/CLOUD_L_ROOT——
# 那是用户自己的媒体库目录，容器不应该改它们的属主。
mkdir -p /data
chown -R "$PUID:$PGID" /data || true

exec gosu "$PUID:$PGID" "$@"

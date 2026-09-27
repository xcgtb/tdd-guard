# 🎬 TDD Guard

双库影视媒体治理系统 · Emby + 115 网盘 + CloudDrive2

## 快速开始

1. `cp .env.example .env`，填入 `WEB_PASSWORD`（用于登录 Web 界面的 Basic Auth 密码，必填，不填容器拒绝启动）。
2. `cp docker-compose.yml.example docker-compose.yml`，按注释修改 `volumes:` 里冒号左边的路径为你自己 NAS 上的真实目录。
   - `AGENT_DATA` 对应的目录用来存运行时数据（数据库、日志、执行记录），选一个空目录即可。
   - `L_ROOT`（本地/主库）、`S_ROOT`（分享库）是要治理的两个媒体库，指向你实际的媒体文件所在目录。
   - `CLOUD_L_ROOT` 是网盘挂载点（如 CloudDrive2），用于联动删除网盘源文件；如果你不需要这个功能可以先随便挂一个空目录。
   - 冒号右边的容器内路径可以保持默认，也可以自己改，但改了要连同 `environment:` 里对应的 `L_ROOT`/`S_ROOT`/`CLOUD_L_ROOT` 一起改，两边必须一致。
3. （可选）默认容器以 root 运行；如果想用非 root 用户，在 `docker-compose.yml` 里加 `environment: PUID=1000` / `PGID=1000`（填你 NAS 上媒体目录属主的 uid/gid）。
4. `docker compose up -d --build`
5. 浏览器打开 `http://NAS-IP:8321`，用 `.env` 里设置的密码登录。
6. 登录后在「设置」页里填 Emby 地址/API Key、TMDB API Key、Telegram Bot Token（如果需要通知）——这些是运行时配置，存在容器内的 `data/config.json` 里，**不需要**也不应该写进 `.env`。

## License
MIT

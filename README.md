# 🎬 TTD Guard

![LICENSE](https://img.shields.io/badge/LICENSE-MIT-blueviolet?style=for-the-badge)

![PYTHON](https://img.shields.io/badge/PYTHON-3.11-blue?style=for-the-badge)

![DOCKER](https://img.shields.io/badge/DOCKER-READY-blue?style=for-the-badge)

双库影视媒体治理系统：比较「本地库」与「分享库」中同一影视的画质与完整度，生成治理计划，并在确认后执行清理；同时提供 Emby 状态、TMDB 缺集检查和 Telegram 通知。

> **安全原则：默认先扫描、再生成计划、最后确认执行。**
>
> 建议第一次部署只做预览，不要直接执行删除。

## 功能

- **双库治理**：比较本地库与分享库中的电影/剧集，按画质和完整度生成治理计划。
- **多季保护**：按 Season 判断剧集完整度，避免把多季合集误判为空。
- **分享独有保护**：分享库独有的 Season 默认只报告，不因缺集而删除唯一资源。
- **白名单/豁免**：支持按关键词跳过治理。
- **TMDB / Emby**：检查缺集、缺季并展示媒体库状态。
- **Telegram Bot**：远程查看状态并执行受保护的治理操作。
- **二次确认**：治理计划与实际删除分离。
- **CloudDrive2 联动**：可选；开启后本地 STRM 删除可同步处理对应云端源文件。

## 运行要求

- Docker 20.10+
- Docker Compose v2+
- Linux/NAS 环境
- 两个可读写的 STRM 媒体目录
- 如需联动删除 CloudDrive2 源文件，再提供对应挂载目录

## 推荐部署：GHCR 预构建镜像

正式用户不需要下载源码，也不需要自己 `build`。只需要准备一个 `docker-compose.yml`，直接从 GHCR 拉取预构建镜像即可。

### 1. 准备目录

例如在 NAS 上：

```bash
mkdir -p /vol1/1000/docker/ttd-guard/data
cd /vol1/1000/docker/ttd-guard
```

### 2. 准备 `docker-compose.yml`

把仓库里的 `docker-compose.yml.example` 复制成 `docker-compose.yml`，然后只修改：

- `WEB_PASSWORD`：改成自己的强密码。
- `/你的本地影视库`：改成本地影视库实际路径。
- `/你的分享影视库`：改成分享影视库实际路径。
- 如果不使用 CloudDrive2 联动，删除 `/media/cloud` 那一行，并保持 `ENABLE_CD2_WATCHDOG: "0"`。
- 如果使用 CloudDrive2 联动，把 `/你的CloudDrive2影视库` 改成实际挂载路径，并将 `ENABLE_CD2_WATCHDOG` 改为 `"1"`。

例如：

```yaml
services:
  ttd-guard:
    image: ghcr.io/xcgtb/ttd-guard:latest
    container_name: ttd-guard
    restart: unless-stopped
    network_mode: host
    environment:
      TZ: Asia/Shanghai
      WEB_USER: admin
      WEB_PASSWORD: 请修改成自己的强密码
      AGENT_DATA: /data
      L_ROOT: /media/local
      S_ROOT: /media/share
      CLOUD_L_ROOT: /media/cloud
      ENABLE_CD2_WATCHDOG: "0"
    volumes:
      - ./data:/data
      - /你的本地影视库:/media/local:rw
      - /你的分享影视库:/media/share:rw
      # 使用 CloudDrive2 联动时保留
      - /你的CloudDrive2影视库:/media/cloud:rslave
```

> **安全提示：** `WEB_PASSWORD` 不要使用示例密码。真实部署时密码只保存在你自己的 NAS `docker-compose.yml` / `.env` 中，不要提交到 GitHub。

### 3. 启动

```bash
docker compose pull
docker compose up -d
docker compose ps
```

访问：

```text
http://NAS-IP:8321
```

## 更新

```bash
docker compose pull
docker compose up -d
```

## 项目目录

```text
ttd-guard/
├── app/                       # 后端核心代码
├── static/                    # Web 前端
├── scripts/                   # 健康检查、诊断、发布检查
├── tests/                     # CI / 回归测试
├── .github/workflows/         # GitHub Actions
├── Dockerfile
├── docker-compose.yml.example
├── .env.example
├── requirements.txt
├── CHANGELOG.md
└── README.md
```

## License

MIT

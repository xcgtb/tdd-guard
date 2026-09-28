# 🎬 TDD Guard

双库影视媒体治理系统：比较「本地库」与「分享库」中同一影视的画质与完整度，生成治理计划，并在确认后执行清理；同时提供 Emby 状态、TMDB 缺集检查和 Telegram 通知。

> **安全原则：默认先扫描、再生成计划、最后确认执行。**
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

复制配置：

```bash
git clone https://github.com/xcgtb/tdd-guard.git
cd tdd-guard
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml
```

编辑 `.env`，至少修改：

```dotenv
WEB_USER=admin
WEB_PASSWORD=请改成你自己的强密码
TZ=Asia/Shanghai
```

编辑 `docker-compose.yml`，把以下三个宿主机路径换成实际路径：

```yaml
- /你的数据目录:/data
- /你的本地STRM目录:/media/local
- /你的分享STRM目录:/media/share
```

如果使用 CloudDrive2 联动，再保留：

```yaml
- /你的CloudDrive2媒体挂载:/media/cloud:rslave
```

并在 `.env` 中：

```dotenv
ENABLE_CD2_WATCHDOG=1
```

启动：

```bash
docker compose pull
docker compose up -d
docker compose ps
```

访问：

```text
http://NAS-IP:8321
```

## 本地构建

如果不使用 GHCR：

```bash
docker compose build --no-cache
docker compose up -d
```

或直接：

```bash
docker build -t tdd-guard:preview .
```

## 配置说明

### Web 登录

| 变量 | 必填 | 说明 |
|---|---:|---|
| `WEB_USER` | 是 | Web 用户名 |
| `WEB_PASSWORD` | 是 | Web 密码；未设置且未显式允许无认证时拒绝启动 |
| `TZ` | 否 | 默认 `Asia/Shanghai` |

### 媒体目录

| 变量 | 默认容器路径 | 作用 |
|---|---|---|
| `AGENT_DATA` | `/data` | 配置、状态、执行记录 |
| `L_ROOT` | `/media/local` | 本地/主媒体库 |
| `S_ROOT` | `/media/share` | 分享媒体库 |
| `CLOUD_L_ROOT` | `/media/cloud` | CloudDrive2 等云盘挂载 |

### 外部服务

可以在 Web「设置」中配置，也可以用环境变量固定：

- `EMBY_HOST`
- `EMBY_KEY`
- `TMDB_KEY`
- `TG_BOT_TOKEN`
- `TG_CHAT_ID`
- `TG_ALLOWED_USERS`

如果这些环境变量显式设置为非空值，它们会覆盖 `data/config.json` 中对应值。

## 第一次使用建议

1. 启动后先打开 Web。
2. 确认 `/media/local`、`/media/share` 和 `/media/cloud` 映射正确。
3. 执行扫描/治理计划预览。
4. 检查待删除列表。
5. 确认无误后再执行真实治理。
6. 正式运行前建议备份 `/data`。

## 更新

使用 GHCR：

```bash
docker compose pull
docker compose up -d
```

指定版本：

```yaml
image: ghcr.io/xcgtb/tdd-guard:1.2.0
```

正式发布版本使用 Git tag，例如：

```bash
git tag v1.2.0
git push origin v1.2.0
```

GitHub Actions 会构建并推送 `linux/amd64` 与 `linux/arm64` 镜像。

## 开发与测试

源码仓库保留测试，用于 CI；**测试代码不会进入正式 Docker 镜像**。

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
pytest tests/ -v
```

发布前检查：

```bash
python scripts/release_check.py
```

## 项目目录

```text
tdd-guard/
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

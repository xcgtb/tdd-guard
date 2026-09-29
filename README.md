# 🎬 TTD Guard

![LICENSE](https://img.shields.io/badge/LICENSE-MIT-blueviolet?style=for-the-badge)
![PYTHON](https://img.shields.io/badge/PYTHON-3.11-blue?style=for-the-badge)
![DOCKER](https://img.shields.io/badge/DOCKER-READY-blue?style=for-the-badge)

双库影视媒体治理系统：对比「本地库」与「分享库」中同一影视的画质与完整度，自动生成治理计划，确认后执行清理，最大化给 115 网盘腾空间。同时提供 Emby 状态、TMDB 缺集检查、入库进度展示与 Telegram 通知。

> **安全原则：默认先扫描、再生成计划、最后确认执行。**
>
> 强烈建议第一次部署只做预览，不要直接执行删除。

## 核心功能

- **双库治理**：对比本地库与分享库的电影/剧集，按画质与完整度生成治理计划。
- **逐集择优**：剧集逐集对齐后按达标率择优，分享画质更优则删本地（联动删 115 源文件腾空间），本地更优则淘汰分享。
- **残次品清理**：前序缺失（缺 E01）、中间断层（疑似被和谐）的不完整季，本地+分享一并清理。
- **多季保护**（关闭 / 开启 / 全量豁免）：本地多季合集时，仅当分享对全部季逐集达标才整体删本地。
- **洗版残留清理**：识别更名洗版后、治理清理 STRM 时残留的字幕/元数据目录，支持一键全选删除。
- **入库进度**：影视探索 / 片库映射展示「已完整 / 连载中 X/Y / 未入库」及双库分库进度。
- **白名单/豁免**：按关键词跳过治理。
- **TMDB / Emby**：缺集、缺季检查与媒体库状态。
- **Telegram Bot**：远程查看状态并执行受保护的治理操作。
- **二次确认**：治理计划与实际删除分离。
- **CloudDrive2 联动**：可选；开启后本地 STRM 删除可同步处理对应云端源文件。

## 治理逻辑

### 画质比较（分层评分）

按优先级逐层比较，杜绝跨分辨率错判（如 1080p DV 误判高于 4K HDR）：

| 优先级 | 维度 | 说明 |
|---|---|---|
| 1 | 分辨率 | 2160p > 1080p > 720p > 其他 |
| 2 | HDR 维度 | DV > HDR10 > SDR |
| 3 | 编码档次 | REMUX > WEB-DL/其他 |
| 4 | 帧率 | 60fps+ > 其他 |
| 5 | 色深 | 10bit > 8bit |

### 决策模型（规则设置中切换）

| 策略 | 行为 |
|---|---|
| 画质优先（默认） | 两库比画质，高者胜出；平局按「平局保留本地」开关决定 |
| 保留本地 | 本地库永远优先，分享库只作补充 |
| 保留分享 | 分享库永远优先，节省 115 网盘空间 |

### 剧集治理（按季原子处理）

- **完整性优先**：一季合并后集号从 E01 起连续无断层，才按画质择优；否则判定为残次品，两边都删。
- **逐集达标率择优**：集数对齐后逐集比画质，达标率高者保留，另一方整体淘汰（单季只作用于单一库，不做跨库拆分）。
- **多季保护三档**：
  - 关闭：逐季独立择优。
  - 开启（默认）：分享对本地全部季逐集达标才整体删本地，否则逐季择优保护本地合集。
  - 全量豁免：本地多季合集一律保护。

## 运行要求

- Docker 20.10+
- Docker Compose v2+
- Linux / NAS 环境
- 两个可读写的 STRM 媒体目录
- 如需联动删除 CloudDrive2 源文件，另需提供对应挂载目录

## 推荐部署：GHCR 预构建镜像

正式用户无需下载源码、也无需自己 build，只需准备一个 `docker-compose.yml`，直接从 GHCR 拉取预构建镜像。

### 1. 准备目录

```bash
mkdir -p /vol1/1000/docker/ttd-guard/data
cd /vol1/1000/docker/ttd-guard
```

### 2. 准备 `docker-compose.yml`

把 `docker-compose.yml.example` 复制为 `docker-compose.yml`，只需修改：

- `WEB_PASSWORD`：改成自己的强密码。
- `/你的本地影视库`、`/你的分享影视库`：改为宿主机上的实际路径（只改冒号左边，冒号右边的容器内路径 `/media/local`、`/media/share`、`/media/cloud`、`/data` 已固定，不要修改）。
- 不使用 CloudDrive2 联动时，删除 `/media/cloud` 行，保持 `ENABLE_CD2_WATCHDOG: "0"`；使用时改挂载路径并置 `"1"`。

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
      ENABLE_CD2_WATCHDOG: "0"
    volumes:
      - ./data:/data
      - /你的本地影视库:/media/local:rw
      - /你的分享影视库:/media/share:rw
      # 使用 CloudDrive2 联动时保留
      - /你的CloudDrive2影视库:/media/cloud:rslave
```

> **安全提示：** `WEB_PASSWORD` 不要使用示例密码。真实密码只保存在你自己的 NAS `docker-compose.yml` / `.env` 中，不要提交到 GitHub。

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

## Web 界面

| 页面 | 作用 |
|---|---|
| 治理总览 | 网盘/分享库统计、总库分类统计、订阅、晨报状态、快速操作与策略快照 |
| 影视探索 | 浏览 TMDB 内容，展示「已完整 / 连载中 X/Y / 未入库」入库进度，可订阅追更 |
| 双库治理 | 扫描双库生成治理清单（待删本地 / 待淘汰分享 / 受保护 / 白名单），确认后执行 |
| 片库映射 | Emby 库总览，剧集/电影分库归类，展示已入库集数与缺集 |
| 追更订阅 | 管理订阅，后台定期检查新集入库/缺集 |
| 每日晨报 | 定时推送统计到 Telegram |
| 每日汇报 | 近 24 小时入库统计 |
| 治理计划 | 每次扫描的 Plan 历史，保留 7 天供审计 |
| 洗版残留 | 扫描并清理无 STRM 的残留目录（一键全选删除） |
| 执行记录 | 治理/清理操作的审计日志 |
| 规则设置 | 治理策略、特别篇策略、入库监控、定时巡检、白名单、Emby/TMDB/Telegram 配置 |

## 更新

```bash
docker compose pull
docker compose up -d
```

固定版本：

```yaml
image: ghcr.io/xcgtb/ttd-guard:1.3.6
```

## 项目目录

```text
ttd-guard/
├── app/                       # 后端核心代码
├── static/                    # Web 前端
├── scripts/                   # 健康检查、诊断、发布检查、预览构建
├── docs/                      # 预览构建说明、发布审计
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

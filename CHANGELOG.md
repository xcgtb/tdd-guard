# Changelog

## 1.3.7

- 应用版本号改为构建时按 git tag 自动注入（`APP_VERSION`），不再在代码里手动维护；推送 `vX.Y.Z` tag 即同步镜像标签与界面显示的版本号。本地直接运行显示 `dev`。

## 1.3.6

### 部署简化
- 容器内媒体路径固定为 `/media/local`、`/media/share`、`/media/cloud`、`/data`，不再需要在 compose 里配置 `L_ROOT` / `S_ROOT` / `CLOUD_L_ROOT` / `AGENT_DATA`，用户只需在 `volumes` 中挂载宿主机目录。

### 仓库整理
- `PREVIEW_BUILD.md`、`RELEASE_AUDIT.md` 移入 `docs/`；`build-preview.sh`、`docker-compose.preview.yml` 移入 `scripts/`，并修正相对路径。
- 修正遗留的 `tdd-guard` 拼写（Dockerfile OCI source label、预览脚本镜像名）。

### 版本号
- 统一应用版本号、界面侧栏版本号、README 与 compose 示例到 `1.3.6`。

## 1.3.0

### 治理决策逻辑重构
- 画质打分改为「分辨率 > HDR 维度 > 编码档次 > 帧率 > 色深」五元组分层次比较，修复 1080p DV 误判高于 4K HDR 的问题。
- 剧集治理取消「双向保留」，改为逐集达标率择优：集数对齐后逐集比画质，达标率高者保留，另一方淘汰。
- 分享独有 / 本地独有 Season 静默，不再生成「受保护」列表项。
- 多季合集保护改为三档（关闭 / 开启 / 全量豁免），默认「开启」；分享对本地全季逐集达标才整体删本地。
- 白名单剔除内置「百家讲坛」关键词，统一由 Web 配置。

### 文件处理
- 删除 STRM 时连带同名前缀的附属文件（-mediainfo.json / .nfo / .srt / .ass 等）彻底粉碎。
- 新增孤儿目录扫描：识别两库中「已无 STRM、只剩附属文件或为空」的媒体目录，可勾选确认删除。

## 1.2.1

### 发布与部署
- 修复误放在 `.github/workflows/` 下的 Docker Compose 示例，避免 GitHub 把 compose 文件错误识别为 Actions workflow。
- GHCR 预构建镜像部署改为真正的单文件 `docker-compose.yml` 配置方式，不再强制依赖 `.env`。
- Dockerfile 增加 OCI source label，便于 GHCR 与 GitHub 仓库关联。
- 统一应用版本号、README 和发布示例到 `1.2.1`。

## Unreleased

### Release preparation
- 清理发布包中的运行时数据、缓存、`.git` 和本地配置。
- Docker 镜像不再包含测试代码。
- CloudDrive2 启动看门狗改为显式开启，避免无 CloudDrive2 环境启动后被误退出。
- 修复测试导入 `app.main` 后因非 daemon 看门狗线程导致进程无法退出的问题。
- 增加发布前敏感信息与运行产物检查。

## 1.2.0

- 双库定时巡检与治理清单时效同步。
- 探索页面筛选扩展。
- 片库映射改为网格卡片布局。
- 统一 root 运行，修复 yml 配置被历史 `config.json` 覆盖的问题。

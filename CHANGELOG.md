# Changelog

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

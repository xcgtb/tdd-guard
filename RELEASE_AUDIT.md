# 发布前审计记录 — 2026-09-28

## 本地 v1.2.1 发布包审计

本发布包仅包含源码、Docker 构建文件、GitHub Actions、Web 静态文件、测试与发布文档，不包含运行时数据和本机密钥。

已排除：

- `.git/`：Git 历史与对象。
- `.env`：本机真实配置与密钥。
- `data/`：数据库、配置、计划、缓存、审计记录。
- `__pycache__/`、`*.pyc`：Python 运行缓存。
- `.pytest_cache/`：测试缓存。
- 本机 `docker-compose.yml`：NAS 实际路径配置。
- `root-and-yml-config.patch`：一次性开发补丁，不作为正式源码文件。

## v1.2.1 发布修正

1. 删除 `.github/workflows/docker-compose.example.yml`。
   - Docker Compose 示例必须位于仓库根目录。
   - 放进 `.github/workflows/` 会被 GitHub 识别为 Actions workflow。
2. 保留 `.github/workflows/docker.yml` 和 `tests.yml`。
3. `docker-compose.yml.example` 改为 GHCR 预构建镜像的一文件配置方式。
4. Dockerfile 增加 `org.opencontainers.image.source` OCI label。
5. FastAPI 版本号统一为 `1.2.1`。
6. README 与 CHANGELOG 同步到 `1.2.1`，明确 GHCR 拉取、固定版本与更新流程。
7. 发布检查脚本增加错误 Compose workflow 文件检测。

## 验证

- Compose YAML 解析：通过。
- `python3 scripts/release_check.py`：通过。
- Python `compileall`：通过。
- `pytest -q`：**98 passed**。
- 本环境未安装 Docker，因此没有在这里执行真实 `docker build` / 多架构推送；GitHub Actions 负责正式的 amd64/arm64 构建。

## 正式镜像边界

正式镜像只 COPY：

```text
/app/app
/app/static
/app/scripts/healthcheck.py
/app/requirements.txt
```

不包含：

```text
/app/tests
/data
.env
.git
__pycache__
.pytest_cache
```

运行数据通过 `/data` volume 持久化。

## GitHub 发布前人工检查

1. 确认仓库根目录不存在 `root-and-yml-config.patch` 等一次性开发补丁。
2. 确认 `.github/workflows/` 下只有真正的 Actions workflow。
3. 确认 GHCR Package `ghcr.io/xcgtb/tdd-guard` 已设置为 Public，公开用户才能匿名 pull。
4. `master` 测试通过后创建 `v1.2.1` tag。
5. Docker workflow 成功后再让用户使用 `ghcr.io/xcgtb/tdd-guard:1.2.1` 或 `:latest`。

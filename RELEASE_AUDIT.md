# 发布前审计记录 — 2026-09-28

## 本地 gz 清理结果

原始备份包含：

- `.git/`：完整 Git 历史与对象，不应进入源码发布压缩包。
- `.env`：包含真实 Web 密码、Telegram ID、内网 Emby 地址；已排除。
- `data/`：配置、状态、计划、缓存、审计记录；已排除。
- `data/trash/`：真实媒体回收站内容；已排除。
- `__pycache__/`、`*.pyc`：Python 运行缓存；已排除。
- `.pytest_cache/`：测试缓存；已排除。
- `docker-compose.yml`：当前 NAS 的本机路径配置；已排除。
- `root-and-yml-config.patch`：一次性开发补丁；已删除，不作为项目源码发布。

## GitHub 同步状态

上传的本地 Git 工作区：

- remote: `https://github.com/xcgtb/tdd-guard.git`
- branch: `master`
- HEAD: `dc8a70f`
- `origin/master`: `dc8a70f`

因此本地源码与当前 GitHub `master` 没有落后/领先差异；本次工作属于发布前清理和修正，而不是重新同步旧代码。

GitHub 当前仓库结构仍保留 `.github/workflows`、`app`、`scripts`、`static`、`tests`、Dockerfile、README 等发布所需内容。citeturn0view0

## 本次修正

1. CloudDrive2 启动看门狗改为 `ENABLE_CD2_WATCHDOG=1` 才启用。
   - 不使用 CloudDrive2 的用户不会因为 `/media/cloud` 不存在而被强制退出。
   - 现有使用 CloudDrive2 的部署把该变量设为 `1` 即可保持原行为。

2. 看门狗线程改为 daemon。
   - 正常 Docker 主进程不受影响。
   - 测试/工具只导入 `app.main` 时可以正常退出。

3. `auth()` 增加兼容处理。
   - 兼容旧式单元测试/内部调用。
   - Cookie 会话与 Basic Auth 仍保持。

4. Docker 镜像不再 COPY `tests/`。
   - GitHub 源码保留测试。
   - 正式镜像只包含运行所需文件。

5. `.dockerignore` 增加 `tests`。

6. `.env.example` 补全常用配置说明，但不放任何真实密钥。

7. 重写 README：
   - 优先 GHCR 预构建镜像。
   - 本地 build。
   - 配置说明。
   - 首次运行安全流程。
   - 更新与 tag 发布。
   - 测试与发布检查。

8. 新增：
   - `PREVIEW_BUILD.md`
   - `docker-compose.preview.yml`
   - `build-preview.sh`
   - `scripts/release_check.py`

9. FastAPI 版本号同步到 `1.2.0`。

## 验证

- Python 静态编译：通过
- 发布敏感信息/运行产物检查：通过
- `pytest tests/ -q`：**98 passed**
- Docker 实际 build：当前执行环境没有 Docker/Podman，因此未在这里执行真实镜像构建；已准备完整 Dockerfile、compose 和预镜像构建脚本。

## 镜像边界

正式镜像包含：

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

运行数据全部由宿主机 `/data` volume 持久化。

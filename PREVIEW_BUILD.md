# 预镜像构建说明

这个项目没有把真实 NAS 数据、`.env`、测试代码或 Git 历史放进 Docker 镜像。

## 飞牛 OS / 任意 Linux Docker 主机

进入项目目录后执行：

```bash
python3 scripts/release_check.py
docker build --pull --no-cache -t tdd-guard:preview .
docker image inspect tdd-guard:preview --format '{{.Id}} {{.Size}}'
```

如果需要导出为镜像文件：

```bash
docker save -o tdd-guard-preview.tar tdd-guard:preview
```

检查镜像内部不应存在测试目录：

```bash
docker run --rm --entrypoint sh tdd-guard:preview -c   'test ! -d /app/tests && test -f /app/scripts/healthcheck.py'
```

## 最小启动冒烟测试

```bash
docker run -d   --name tdd-guard-preview   --network host   -e WEB_USER=admin   -e WEB_PASSWORD='CHANGE-ME'   -e ENABLE_CD2_WATCHDOG=0   tdd-guard:preview
```

等待几秒后：

```bash
curl -fsS http://127.0.0.1:8321/api/health
docker inspect tdd-guard-preview --format '{{.State.Status}} {{.State.ExitCode}}'
docker logs --tail 100 tdd-guard-preview
```

结束测试：

```bash
docker rm -f tdd-guard-preview
```

> 正式部署不要使用 `CHANGE-ME`。正式环境必须设置自己的强密码，并按实际情况挂载 `/data`、本地库、分享库和 CloudDrive2。

## 发布 GHCR

创建版本 tag：

```bash
git tag v1.2.1
git push origin v1.2.1
```

仓库的 GitHub Actions 会按现有 workflow 构建 `linux/amd64` 和 `linux/arm64`，并推送：

```text
ghcr.io/xcgtb/tdd-guard:<version>
ghcr.io/xcgtb/tdd-guard:latest
```

## 发布前检查清单

- [ ] `python3 scripts/release_check.py` 通过
- [ ] `pytest tests/ -v` 全部通过
- [ ] `docker build --pull --no-cache` 成功
- [ ] 镜像不含 `/app/tests`
- [ ] 未提交 `.env`
- [ ] 未提交 `data/`
- [ ] 未提交 `.git/`
- [ ] 未提交 `__pycache__` / `.pytest_cache`
- [ ] 首次运行使用治理计划预览，不直接执行删除
- [ ] 已检查 GHCR tag 与 README 版本说明

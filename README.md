# 🎬 TDD Guard

![License](https://img.shields.io/badge/License-MIT-6C63FF?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.x-3776AB?style=for-the-badge&logo=python)
![Docker](https://img.shields.io/badge/Docker-ready-2496ED?style=for-the-badge&logo=docker)

**双库影视媒体治理系统：自动比较「本地库」与「分享库」中同一部剧/电影的画质与完整度，
按规则清理冗余副本，配合 Emby 状态看板、TMDB 缺集检查与 Telegram Bot 远程通知/操作。**

---

#### 主要功能

| 功能 | 说明 |
|---|---|
| 双库对比治理 | 比较本地库与分享库中同一影视的画质/完整度，自动判定保留哪一份、清理哪一份 |
| 多季完整度保护 | 剧集按季判断完整度，避免多季合集被"挖空" |
| TMDB 联动 | 根据 TMDB 信息检测缺集、缺季 |
| Emby 看板 | 展示 Emby 媒体库状态 |
| Telegram Bot | 远程查看治理状态、下发操作指令 |
| 二次确认执行 | 先生成治理计划预览，确认后才真正执行删除，避免误删 |

---

#### 快速部署

##### 1. 准备环境

- Docker 20.10+
- Docker Compose 2.x
- 一台跑着 Emby、115 网盘挂载（如 TgtoDrive / CloudDrive2）的 NAS 或 Linux 服务器

##### 2. 拉取代码并准备配置文件

    git clone https://github.com/xcgtb/tdd-guard.git
    cd tdd-guard
    cp .env.example .env
    cp docker-compose.yml.example docker-compose.yml

##### 3. 编辑 .env

只需要填一项：

| 变量 | 说明 |
|---|---|
| WEB_PASSWORD | 登录 Web 管理界面的密码，**必填**，不填容器拒绝启动 |

##### 4. 编辑 docker-compose.yml

按文件注释，把 volumes: 里**冒号左边**的路径改成你自己 NAS 上的真实目录：

| 变量 | 说明 |
|---|---|
| AGENT_DATA | 运行时数据目录（数据库、日志、执行记录），选一个空目录即可 |
| L_ROOT | 本地/主库路径（要治理的媒体库之一） |
| S_ROOT | 分享库路径（要治理的媒体库之一） |
| CLOUD_L_ROOT | 网盘挂载点（如 CloudDrive2），用于联动删除网盘源文件；不需要此功能可先随便挂一个空目录 |

冒号**右边**的容器内路径可保持默认，也可自己改，但改了要连同 environment: 里对应的
L_ROOT / S_ROOT / CLOUD_L_ROOT 一起改，两边必须一致。

（可选）非 root 运行：容器默认以 root 运行；如果想用非 root 用户，在 environment: 里加：

    - PUID=1000
    - PGID=1000

（填你 NAS 上媒体目录属主的 uid/gid）

##### 5. 启动服务

    docker compose up -d --build

访问 Web 管理台：

    http://你的NAS-IP:8321

首次登录使用 .env 中设置的 WEB_PASSWORD。

---

#### 开始使用

1. 登录 Web 管理台。
2. 在「设置」页填写 Emby 地址 / API Key、TMDB API Key、Telegram Bot Token（如需通知）——
   这些是运行时配置，保存在容器内的 data/config.json 里，**不需要**也不应该写进 .env。
3. 查看「探索」「治理」页面确认双库扫描结果。
4. 需要时在治理计划页预览待清理项，确认无误后再执行。

---

#### 常用命令

查看运行状态：

    docker compose ps

查看日志：

    docker compose logs -f

更新并重启：

    docker compose pull
    docker compose up -d --build

停止服务：

    docker compose down

备份时建议至少保留 AGENT_DATA 对应的目录（数据库、配置、执行记录）。

---

## License

MIT

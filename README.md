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

完整内容如下，把标了注释的几处路径改成你自己 NAS 上的真实目录即可，其余保持不变：

    services:
      tdd-guard:
        build: .
        image: tdd-guard:latest
        container_name: tdd-guard
        restart: unless-stopped
        network_mode: host
        env_file: .env
        environment:
          - TZ=Asia/Shanghai
          # 下面三个是"容器内部"路径，随便起名，但必须跟下面 volumes 里冒号右边完全一致
          - AGENT_DATA=/data
          - L_ROOT=/media/local          # 你的本地/网盘媒体库（主库）
          - S_ROOT=/media/share          # 你的分享媒体库（用于双库对比治理）
          - CLOUD_L_ROOT=/media/cloud    # CloudDrive2 等网盘挂载的本地库（用于联动删源）
        volumes:
          # 冒号左边＝宿主机（你的 NAS）上的真实路径，改成你自己的
          # 冒号右边＝容器内部路径，必须和上面 environment 里的 L_ROOT/S_ROOT/CLOUD_L_ROOT 一一对应
          - /path/to/your/data:/data
          - /path/to/your/local-media-library:/media/local
          - /path/to/your/share-media-library:/media/share
          - /path/to/your/clouddrive-mount:/media/cloud:rslave
          - /path/to/your/static:/app/static   # 可选：想不重新 build 就改前端页面时挂载
        logging:
          driver: json-file
          options:
            max-size: "10m"
            max-file: "3"
        healthcheck:
          test: ["CMD", "python", "/app/scripts/healthcheck.py"]
          interval: 30s
          timeout: 10s
          retries: 3
          start_period: 20s

简单说：
- `environment:` 里的 `L_ROOT` / `S_ROOT` / `CLOUD_L_ROOT` 是容器内部路径名，一般不用改。
- `volumes:` 里每一行**冒号左边**是你 NAS 上真实的文件夹路径，改成你自己的；**冒号右边**要和上面
  `environment:` 里的值一一对应，两边必须一致。
- 不需要联动删除网盘源文件的话，`CLOUD_L_ROOT` 对应的那行可以随便挂一个空目录。
- 容器统一以 root 运行，不提供非 root（PUID/PGID）模式——媒体目录多是 NAS 上不同用户/挂载源混合的权限，非 root 收益不大，反而增加部署复杂度。

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

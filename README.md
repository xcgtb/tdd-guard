# 🎬 TDD Guard

双库影视媒体治理系统 · Emby + 115 网盘 + CloudDrive2

自动比较「本地库」与「分享库」中同一部剧/电影的画质、完整度，按规则清理冗余副本，
支持 TMDB 缺集检查、Telegram Bot 通知与远程操作、两步确认执行防误删。

---

## 功能特性

- 双库画质/完整度对比，自动判定保留哪一份、清理哪一份
- 剧集按季完整度保护，避免多季合集被"挖空"
- TMDB 联动，检测缺集/缺季
- Emby 库状态看板
- Telegram Bot 远程查看与操作
- 二次确认执行（预览计划 → 确认后才真正删除），避免误删

---

## 环境要求

- Docker + Docker Compose
- 一台跑着 Emby、115 网盘挂载（如 TgtoDrive/CloudDrive2）的 NAS 或服务器

---

## 快速开始

### 1. 准备配置文件

```bash
cp .env.example .env
cp docker-compose.yml.example docker-compose.yml
# -*- coding: utf-8 -*-
"""从 engine.py 拆出。共享状态在 state，跨模块符号按所属模块 `模块.名字` 在调用时访问（monkeypatch 所属模块即可穿透）。"""
import os, re, sys, json, threading, shutil, fcntl, time, argparse, datetime, hashlib, logging, traceback
import urllib.parse, urllib.request, urllib.error
from pathlib import Path
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import contextlib, dataclasses, html

from . import config as _cfg
from . import logger
from .core import (esc, parse_season_dir, get_ep, title_key, governance_title_key,
                   analyze_season_episodes, parse_emby_library, quality_label, RE_SXXEXX)
from . import state, tmdb, emby, lib, storage, tg, media, sync
from .tgmsg import blocks as tgmsg_blocks, reports as tgmsg_reports

log = logging.getLogger('media_agent')


def get_subscriptions() -> list:
    """订阅列表（SQLite subscriptions 表，按保存顺序）。规范接口：config.get_subscriptions 只是兼容壳。"""
    return storage.db_subs_list()


def set_subscriptions(subs: list) -> list:
    """整份替换订阅列表，返回实际保存的列表。"""
    return storage.db_subs_replace(subs)


def _load_sub_state() -> dict:
    """读取订阅状态（SQLite sub_state 表，store = state.SUB_STORE）。
    旧版「删除状态文件即重置」的语义改为 reset_sub_state()。"""
    return storage.db_load_sub_state(state.SUB_STORE)


load_sub_state = _load_sub_state  # 公开名（bot / 路由用这个）

def _save_sub_state(sub_state: dict):
    sync.bump('subscriptions', invalidate=False, reason='sub_state')
    if not storage.db_save_sub_state(state.SUB_STORE, sub_state):
        log.warning('保存追更状态失败')

def reset_sub_state():
    """清空全部追更状态（下一轮检查按首次运行处理）。"""
    return storage.db_clear_sub_state(state.SUB_STORE)

def _save_subscription_report(updates):
    """保存最近一次真正成功推送的追更汇报（SQLite 文档 subscription_report）。晨报只引用这份实际汇报。"""
    payload = {'ts': time.time(), 'date': time.strftime('%Y-%m-%d', time.localtime()), 'updates': updates or []}
    if not storage.db_doc_put(storage.DOC_SUB_REPORT, payload):
        log.warning('保存追更实际汇报失败')

def _load_subscription_report(today_only=False):
    data = storage.db_doc_get(storage.DOC_SUB_REPORT)
    if not isinstance(data, dict): return None
    if today_only and data.get('date') != time.strftime('%Y-%m-%d', time.localtime()): return None
    return data

def _emby_series_latest_ep(series_tmdb_id: str):
    """查 Emby 里某剧（按 tmdb_id）的所有副本并 union 分集（薄包装 media.series_live）。

    同一剧可能同时存在本地/分享两个 Series 条目；对所有匹配 Series 聚合 (季,集)。
    数据访问统一走 media（含 ProviderIds 校验与幽灵过滤），这里只把结果整理成
    追更状态机需要的形状；追更要求「现查现报」，不走 15 秒微缓存；完全查不到分集时
    返回 None，交由 _disk_series_eps 兜底。
    """
    live = media.series_live(series_tmdb_id, use_cache=False)
    if not live or not live.get('episodes'):
        return None
    have = set(live['episodes'])
    created = live.get('created') or {}
    sn, en = max(have)
    return {
        'series_id': live.get('series_id'),
        'series_ids': list(live.get('series_ids') or []),
        'series_name': live.get('series_name') or '',
        'season': sn, 'episode': en,
        'date_created': created.get((sn, en), ''),
        'episodes': have,
    }

def _disk_series_eps(series_tmdb_id: str):
    """追更磁盘兜底：按目录名 tmdb 定位剧集分集（不依赖 Emby ProviderIds.Tmdb）。

    上游 TgtoDrive 把 tmdb 写在目录名 `{tmdb-xxx}` 里，而 Emby 的刮削器不会把
    目录名里的 tmdb 后缀解析进 ProviderIds，导致 ``_emby_series_latest_ep`` 按
    ``ProviderIds.Tmdb`` 查不到 → 追更拿不到任何分集、「不能用」。
    这里直接扫描两库磁盘，复用 lib.Lib 已经建好的 `tmdb_refs['tv:<id>']` 索引，
    按目录名 tmdb 匹配并 union 分集。返回结构与 ``_emby_series_latest_ep`` 一致，
    多一个 ``source='disk'`` 便于日志区分。
    """
    if not series_tmdb_id:
        return None
    eps = set()
    names = []
    try:
        for disk_lib in (lib._get_lib(state.L_ROOT), lib._get_lib(state.S_ROOT)):
            for key in disk_lib.tmdb_refs.get('tv:' + str(series_tmdb_id), ()):
                disp = disk_lib.meta.get(key, ('', '', None))[0]
                if disp and disp not in names:
                    names.append(disp)
                for sn, files in disk_lib.tv.get(key, {}).items():
                    if sn <= 0:
                        continue
                    for f in files:
                        try:
                            ep = get_ep(f.name, f.parent.name, allow_bare_ep=True)
                        except Exception:
                            continue
                        if ep and ep[0] > 0 and ep[1] > 0:
                            eps.add(ep)
    except Exception as e:
        log.warning('追更磁盘兜底扫描失败 %s: %s', series_tmdb_id, e)
    if not eps:
        return None
    sn, en = max(eps)
    return {
        'series_id': None, 'series_ids': [],
        'series_name': names[0] if names else '',
        'season': sn, 'episode': en,
        'date_created': '', 'episodes': eps, 'source': 'disk',
    }

def _ep_key_num(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)) * 10000 + int(m.group(2))) if m else 0

def _ep_key(sn, en):
    """(1, 7) -> 'S01E07'"""
    return f'S{int(sn):02d}E{int(en):02d}'

def _parse_ep_key(k):
    """'S01E07' -> (1, 7)；解析不了返回 None"""
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)), int(m.group(2))) if m else None

def _eps_to_keys(eps):
    """[(季,集)] -> {'S01E01', ...}"""
    return {_ep_key(sn, en) for sn, en in (eps or []) if sn > 0 and en > 0}

def _keys_to_eps(keys):
    """{'S01E01', ...} -> {(季,集)}（忽略解析不出的字符串）"""
    out = set()
    for k in (keys or []):
        p = _parse_ep_key(k)
        if p:
            out.add(p)
    return out


keys_to_eps = _keys_to_eps  # 公开名（bot 用这个）

_fmt_ep_ranges = tgmsg_blocks.ep_ranges  # 实现已移到 app/tgmsg/blocks.py，这里保留旧名兼容

_CHECK_LOCK = threading.Lock()
CHECK_LOCK_TIMEOUT = 300


def check_subscriptions(send_notify=True) -> dict:
    """追更订阅检查入口：全局串行化，避免定时 / 网页 / bot 同时检查重复推送。

    第二个调用者最多等 CHECK_LOCK_TIMEOUT 秒；拿到锁后基于第一个调用者已保存的
    状态重新计算，通常得到「无变化」，因此不会重复发通知。等待超时返回 status='busy'。
    """
    if not _CHECK_LOCK.acquire(timeout=CHECK_LOCK_TIMEOUT):
        log.warning('追更订阅检查繁忙：等待超过 %d 秒，本轮跳过', CHECK_LOCK_TIMEOUT)
        return {'updates': [], 'total': 0, 'status': 'busy'}
    try:
        return _run_subscription_check(send_notify)
    finally:
        _CHECK_LOCK.release()


def _run_subscription_check(send_notify=True) -> dict:
    """追更订阅检查（集合差集状态机）。

    核心口径（修复「吞通知 / 重复通知 / 跳集不报警」）：

    1. **集合差集**：每次检查取 Emby 实际存在的集 ``have``，与状态文件里
       ``notified_episodes``（已成功通知过的集）求差 → ``new_eps``；与 TMDB 已播集
       求差 → ``missing_eps``。不再只看「最大集号」，所以中间插入的集也能被发现。
    2. **发送成功才记账**：``notify_telegram()`` 返回 True 后才把 ``new_eps`` 并入
       ``notified_episodes``。发送失败则状态原样不动，下一轮自动重试 —— 既不丢通知
       也不重复刷屏。
    3. **跳集报警**：``newly_missing``（本次新出现的缺集）与 ``refilled``（本次补齐的
       缺集）分别通知，提示精确到集号区间。
    4. **免迁移**：首次遇到只有旧 ``latest_ep`` 的订阅时，把它当成「已通知过的最后一集」
       向前播种，避免升级后把全库旧集刷一遍。
    """
    cfg = _cfg.load_config()
    if cfg.get('subscribe_enabled', '1') != '1':
        return {'updates': [], 'skipped': 'disabled'}
    subs = get_subscriptions()
    if not subs:
        return {'updates': [], 'total': 0}

    check_tmdb = cfg.get('subscribe_check_tmdb', '1') == '1'
    state = _load_sub_state()
    updates = []
    now_str = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    # 记录每个订阅本轮「发送成功后应写回」的状态，发送前不落盘
    staged = {}

    for sub in subs:
        if not sub.get('enabled', True): continue
        sid = sub.get('id') or sub.get('tmdb_id') or sub.get('name')
        tmdb_id = sub.get('tmdb_id')
        if not tmdb_id: continue

        latest = _emby_series_latest_ep(tmdb_id)
        # Emby 的 ProviderIds.Tmdb 缺失（上游 TgtoDrive 目录名有 tmdb 但 Emby 没刮进去）
        # 时，回退到磁盘目录名 tmdb 兜底，避免追更拿不到分集而「不能用」。
        if not latest:
            latest = _disk_series_eps(tmdb_id)
        tmdb_info = tmdb._tmdb_series_info(tmdb_id) if check_tmdb else None

        # 无 Emby 数据且无 TMDB → 跳过（保持旧行为）
        if not latest and not tmdb_info: continue

        prev = state.get(sid) or {}
        have = (latest or {}).get('episodes') or set()

        # ── 免迁移播种 ──
        # 老结构只有 latest_ep：把它当成「已通知过的最后一集」，该季 1..N 全部视为
        # 已通知，避免升级后一次刷出几十条历史集。全新订阅则把当前已有的集全部播种，
        # 首次检查保持安静（避免「刚加订阅就被刷屏」）。
        prev_notified_raw = prev.get('notified_episodes')
        if prev_notified_raw is None:
            prev_latest = prev.get('latest_ep') or ''
            seed = set()
            lp = _parse_ep_key(prev_latest)
            if lp:
                if have:
                    # 只播种 latest_ep 所属季；其它季由后续轮次自然发现
                    seed |= {(sn, en) for (sn, en) in have if sn == lp[0] and en <= lp[1]}
                else:
                    # Emby 查不到分集时保守播种到 latest_ep（假设中间无断层）
                    seed |= {(lp[0], e) for e in range(1, lp[1] + 1)}
            else:
                seed = set(have)          # 全新订阅：当前已有的都算已知
            notified = seed
            migrated = True
        else:
            notified = _keys_to_eps(prev_notified_raw)
            migrated = False

        prev_missing = _keys_to_eps(prev.get('missing_episodes'))

        new_eps = sorted(have - notified)                       # 真实新增
        missing_eps = set()
        if tmdb_info and latest:
            aired = tmdb_info.get('aired') or set()
            # ── 缺集只认「从未入库过的集」──
            # 若只算 aired - have，那么「曾经有、后来被删」的集会被误报成缺集
            # （下架 / 洗版删旧时最容易遇到）。取 aired - (have | notified)：
            # 已通知过的集视为「到过库」，即使当下不在也不算缺集。
            missing_eps = aired - have - notified
        newly_missing = sorted(missing_eps - prev_missing)      # 本次新出现的缺集
        refilled = sorted(set(new_eps) & prev_missing)          # 本次被补齐的缺集

        # 通知只由「状态相对上次的变化」驱动，不看绝对值
        has_change = bool(new_eps or newly_missing or refilled)

        # 新基线：notified_episodes 只在发送成功后才并入 new_eps，所以这里先按
        # 「已通知集 + 本轮新增」算出候选值，发送失败时回退到旧 notified。
        st = {
            'tmdb_id': tmdb_id,
            'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
            'notified_episodes': sorted(_eps_to_keys(notified | set(new_eps))),
            'missing_episodes': sorted(_eps_to_keys(missing_eps)),
            'tmdb_total': (tmdb_info or {}).get('total_episodes', prev.get('tmdb_total') or 0),
            'tmdb_declared': (tmdb_info or {}).get('declared_total', prev.get('tmdb_declared') or 0),
            'tmdb_status': (tmdb_info or {}).get('status', ''),
            'updated_at': now_str,
        }
        # notified_episodes 为空时保持旧 latest_ep，避免 Emby 短暂异常把展示清空
        all_keys = st['notified_episodes']
        st['latest_ep'] = max(all_keys, key=_ep_key_num) if all_keys else (prev.get('latest_ep') or '')
        staged[sid] = st

        if has_change:
            updates.append({
                'sid': sid,
                'name': (latest and latest.get('series_name')) or (tmdb_info and tmdb_info.get('name')) or sub.get('name'),
                'tmdb_id': tmdb_id,
                'poster': sub.get('poster', ''),
                'new_eps': sorted(_eps_to_keys(new_eps)),
                'missing_eps': sorted(_eps_to_keys(missing_eps)),
                'newly_missing': sorted(_eps_to_keys(newly_missing)),
                'refilled': sorted(_eps_to_keys(refilled)),
            })

    # ── 发送成功才记账 ──
    # send_notify=False（bot 手动查看 / 晨报 / 保存订阅后的即时检查）本身不发送消息，
    # 等价于「已送达」，同样要记账，否则下一轮会把同一批集重复播报。
    # 发送失败时：notified_episodes / missing_episodes 全部回退到本轮开始前的值，
    # 保证下一轮算出同一批 new_eps 继续重试 —— 既不丢通知也不重复刷屏。
    sent_ok = True
    if send_notify and updates:
        # 文案统一由 tgmsg 渲染（与 Bot /sub check、晨报「订阅更新」同一份实现）
        sent_ok = tg.notify_telegram(str(tgmsg_reports.sub_notify(updates)))
        if sent_ok:
            _save_subscription_report(updates)
        else:
            log.warning('追更订阅推送失败，本轮不记账，下次继续重试')

    if not sent_ok:
        for u in updates:
            st = staged.get(u['sid'])
            if not st:
                continue
            # 回退新增集：把本轮 new_eps 从 notified_episodes 里剔除
            kept = _keys_to_eps(st['notified_episodes']) - _keys_to_eps(u['new_eps'])
            st['notified_episodes'] = sorted(_eps_to_keys(kept))
            st['latest_ep'] = (max(st['notified_episodes'], key=_ep_key_num)
                               if st['notified_episodes'] else '')
            # 缺集基线也不推进：下一轮重新判定 newly_missing
            st['missing_episodes'] = sorted(
                _eps_to_keys(_keys_to_eps(st['missing_episodes']) - _keys_to_eps(u['newly_missing'])))

    state.update(staged)
    _save_sub_state(state)

    return {'updates': updates, 'total': len(subs), 'sent': bool(sent_ok) if send_notify and updates else None}

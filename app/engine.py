# -*- coding: utf-8 -*-
"""media_agent.py —— 飞牛 NAS 端治理引擎（兼容壳）。

实现已全部拆到 state / governance / wash / morning / tmdb / ingest / subscribe / emby /
lib / stats / storage / tg / core。本模块只保留 CLI 入口（python -m app.engine）与
ACTIONS 表；engine.X 读取按 _OWNERS 顺序转发到所属模块，写入一律报错——
monkeypatch 必须打在所属模块上，否则调用方根本看不到。
"""
import json, sys, types, argparse, traceback

from . import (state, governance, wash, morning, tmdb, ingest, subscribe,
               emby, lib, stats, storage, tg, core)

ACTIONS = {
    'inter_check':  governance.action_inter_check,
    'inter_clean':  governance.action_inter_clean,
    'stats':        ingest.action_stats,
    'played':       ingest.action_played,
    'search':       ingest.action_search,
    'logs':         ingest.action_logs,
    'explore':      tmdb.action_explore,
    'emby_library': morning.action_emby_library,
    'library_stats': stats.action_library_stats,
    'orphans':       wash.action_scan_orphans,
    'clean_orphans': wash.action_clean_orphan_dirs,
}
MUTATING = {'inter_clean', 'clean_orphans'}

_OWNERS = (state, governance, wash, morning, tmdb, ingest, subscribe, emby, lib, stats, storage, tg, core)


def __getattr__(name):
    for m in _OWNERS:
        if name in vars(m):
            return getattr(m, name)
    raise AttributeError(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--action', required=True, choices=list(ACTIONS))
    ap.add_argument('--kw', default='')
    ap.add_argument('--plan', default='')
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    # CLI 也可能是升级后的第一个入口：先做一次性存储迁移（幂等，失败不阻断）
    try:
        mig = storage.db_migrate()
        if any(mig.values()):
            state.log.info('SQLite 存储层迁移: %s', mig)
    except Exception as e:
        state.log.warning('存储层迁移失败: %s', e)
    # 互斥锁统一由 action_inter_clean 自己持有，CLI / Web / Telegram Bot 三个入口共用同一把锁。
    try:
        res = ACTIONS[args.action](args)
    except Exception as e:
        traceback.print_exc(file=sys.stderr)
        res = {'status': 'error', 'message': f'{type(e).__name__}: {e}'}
    print(json.dumps(res, ensure_ascii=False))


class _CompatShim(types.ModuleType):
    def __setattr__(self, name, value):
        owner = next((m.__name__.rsplit('.', 1)[-1] for m in _OWNERS if name in vars(m)), '所属模块')
        raise AttributeError(f'engine 为兼容壳：请 patch 所属模块（{owner}）')


sys.modules[__name__].__class__ = _CompatShim

if __name__ == '__main__':
    main()

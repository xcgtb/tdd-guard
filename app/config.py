# -*- coding: utf-8 -*-
"""运行时配置管理：/data/config.json 优先，内置默认值兜底"""
import os
import json
from pathlib import Path

DATA_DIR = Path(os.environ.get('AGENT_DATA', '/data'))
CONFIG_FILE = DATA_DIR / 'config.json'

DEFAULTS = {
    # 服务
    'emby_host':              os.environ.get('EMBY_HOST',              'http://127.0.0.1:8096'),
    'emby_key':               os.environ.get('EMBY_KEY',               ''),
    'tmdb_key':               os.environ.get('TMDB_KEY',               ''),

    # Telegram
    'telegram_bot_token':     os.environ.get('TG_BOT_TOKEN',           ''),
    'telegram_chat_id':       os.environ.get('TG_CHAT_ID',             ''),
    'telegram_allowed_users': os.environ.get('TG_ALLOWED_USERS',       ''),

    # 治理策略
    'strategy_decision':             'quality_first',
    'strategy_multi_season_protect': 'compare',  # off | compare | full —— 多季合集保护档位（compare 即“开启”）
    'strategy_tie_keep_local':       os.environ.get('TIE_KEEP_LOCAL', '0'),
    'strategy_exempt_keywords':      '',
    'strategy_special_action':       'compare', # compare | ignore | delete  —— 特别篇 S00 策略

    # 入库监控
    'ingest_enabled':        '1',      # 后台入库监控开关
    'ingest_interval_min':   '5',      # 轮询间隔（分钟）

    # 追更订阅
    'subscribe_enabled':         '1',
    'subscribe_interval_min':    '30',
    'subscribe_check_tmdb':      '1',   # 是否对照 TMDB 已播集
    'subscriptions':             '[]',

    # 晨报
    'morning_report_enabled':    '0',
    'morning_report_hour':       '9',
    'morning_report_minute':     '0',
    'morning_report_items':      'stats,subscriptions,emby_gap',
    'morning_report_last_date':  '',
    'morning_prescan_min':       '5',   # 提前 N 分钟后台静默预热
    'morning_prescan_last_date': '',    # 内部

    'tmdb_scan_enabled':         '1',
    'tmdb_scan_interval_hours':  '24',
    'tmdb_scan_last_ts':         '0',
}

# 这些字段如果在环境变量（.env / docker-compose.yml）里显式设置了非空值，
# 每次 load_config() 都会用环境变量覆盖 config.json 里的落盘值——
# 保证纯 yml 部署、不想碰 Web 设置页的用户，改 yml 能一直生效，
# 不会被"曾经在 Web 页保存过一次"这件事锁死。
# 用户如果想改回用 Web 页管理，把 yml 里对应这行删掉/留空即可。
ENV_OVERRIDE_KEYS = {
    'emby_host':              'EMBY_HOST',
    'emby_key':               'EMBY_KEY',
    'tmdb_key':               'TMDB_KEY',
    'telegram_bot_token':     'TG_BOT_TOKEN',
    'telegram_chat_id':       'TG_CHAT_ID',
    'telegram_allowed_users': 'TG_ALLOWED_USERS',
}

INTERNAL_KEYS = {'tmdb_scan_last_ts', 'morning_prescan_last_date',
                 'morning_report_last_date', 'subscriptions'}
EDITABLE_KEYS = [k for k in DEFAULTS if k not in INTERNAL_KEYS]
SENSITIVE_KEYS = ['emby_key', 'tmdb_key', 'telegram_bot_token']
ALL_SENSITIVE = SENSITIVE_KEYS


def mask_value(v: str, head: int = 4, tail: int = 4) -> str:
    if not v: return ''
    v = str(v)
    if len(v) <= head + tail + 2: return '*' * len(v)
    return v[:head] + '****' + v[-tail:]


def mask_config(cfg: dict) -> dict:
    out = dict(cfg)
    for k in ALL_SENSITIVE:
        if k in out and out[k]: out[k] = mask_value(out[k])
    return out


def is_masked_value(v: str) -> bool:
    if not v: return False
    if '****' in v: return True
    if v and all(c == '*' for c in v): return True
    return False


def load_config() -> dict:
    cfg = DEFAULTS.copy()
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        if isinstance(data, dict):
            cfg.update({k: str(v) for k, v in data.items() if v is not None})
    except (OSError, ValueError):
        pass
    # yml/.env 显式设置的连接类配置始终生效，优先级高于历史落盘值
    for k, env_name in ENV_OVERRIDE_KEYS.items():
        v = os.environ.get(env_name)
        if v:
            cfg[k] = v
    return cfg


def save_config(cfg: dict) -> dict:
    # 全量落盘：DEFAULTS 里有定义的键都写
    # （EDITABLE_KEYS 只控制 /api/config 接口能改哪些，不控制磁盘写入）
    clean = {}
    for k in DEFAULTS:
        if k in cfg:
            clean[k] = str(cfg.get(k, '')).strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix('.tmp')
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(CONFIG_FILE)
    return clean


# ═══════════════════ 结构化访问 ═══════════════════
def get_strategy() -> dict:
    cfg = load_config()
    dec = cfg.get('strategy_decision', 'quality_first')
    if dec == 'balanced':
        dec = 'quality_first'
    if dec not in ('quality_first', 'keep_local', 'keep_share'):
        dec = 'quality_first'
    sa = cfg.get('strategy_special_action', 'compare')
    if sa == 'keep':
        sa = 'ignore'
    if sa not in ('compare', 'ignore', 'delete'):
        sa = 'compare'
    mp = cfg.get('strategy_multi_season_protect', 'compare')
    # 兼容旧布尔值 '1'/'0'
    if mp in ('1', 'true', 'on'):
        mp = 'full'
    elif mp in ('0', 'false', 'off'):
        mp = 'off'
    if mp not in ('off', 'compare', 'full'):
        mp = 'compare'
    return {
        'decision': dec,
        'multi_season_protect': mp,
        'tie_keep_local': cfg.get('strategy_tie_keep_local', '0') == '1',
        'exempt_keywords': [x.strip() for x in (cfg.get('strategy_exempt_keywords') or '').split(',') if x.strip()],
        'special_action': sa,
    }


def update_strategy(**kwargs) -> dict:
    cfg = load_config()
    if 'decision' in kwargs:
        d = str(kwargs['decision'])
        if d == 'balanced':
            d = 'quality_first'
        if d not in ('quality_first', 'keep_local', 'keep_share'):
            d = 'quality_first'
        cfg['strategy_decision'] = d
    if 'multi_season_protect' in kwargs:
        mp = str(kwargs['multi_season_protect'])
        if mp in ('1', 'true', 'on'):
            mp = 'full'
        elif mp in ('0', 'false', 'off'):
            mp = 'off'
        if mp not in ('off', 'compare', 'full'):
            mp = 'compare'
        cfg['strategy_multi_season_protect'] = mp
    if 'tie_keep_local' in kwargs:
        cfg['strategy_tie_keep_local'] = '1' if kwargs['tie_keep_local'] else '0'
    if 'exempt_keywords' in kwargs:
        kws = kwargs['exempt_keywords']
        cfg['strategy_exempt_keywords'] = ','.join(str(x).strip() for x in kws if str(x).strip()) if isinstance(kws, list) else str(kws)
    if 'special_action' in kwargs:
        sa = str(kwargs['special_action'])
        if sa == 'keep':
            sa = 'ignore'
        if sa in ('compare', 'ignore', 'delete'):
            cfg['strategy_special_action'] = sa
    save_config(cfg)
    return get_strategy()


def get_subscriptions() -> list:
    cfg = load_config()
    try:
        data = json.loads(cfg.get('subscriptions') or '[]')
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def set_subscriptions(subs: list) -> list:
    cfg = load_config()
    cfg['subscriptions'] = json.dumps(subs, ensure_ascii=False)
    save_config(cfg)
    return subs


def get_morning_report() -> dict:
    cfg = load_config()
    return {
        'enabled': cfg.get('morning_report_enabled', '0') == '1',
        'hour': int(cfg.get('morning_report_hour') or 9),
        'minute': int(cfg.get('morning_report_minute') or 0),
        'items': [x.strip() for x in (cfg.get('morning_report_items') or '').split(',') if x.strip()],
        'last_date': cfg.get('morning_report_last_date') or '',
        'prescan_min': int(cfg.get('morning_prescan_min') or 5),
        'prescan_last_date': cfg.get('morning_prescan_last_date') or '',
    }


def update_morning_report(**kwargs) -> dict:
    cfg = load_config()
    if 'enabled' in kwargs: cfg['morning_report_enabled'] = '1' if kwargs['enabled'] else '0'
    if 'hour' in kwargs: cfg['morning_report_hour'] = str(int(kwargs['hour']))
    if 'minute' in kwargs: cfg['morning_report_minute'] = str(int(kwargs['minute']))
    if 'items' in kwargs:
        items = kwargs['items']
        cfg['morning_report_items'] = ','.join(str(x).strip() for x in items if str(x).strip()) if isinstance(items, list) else str(items)
    if 'prescan_min' in kwargs: cfg['morning_prescan_min'] = str(int(kwargs['prescan_min']))
    save_config(cfg)
    return get_morning_report()


def mark_morning_report_sent(date_str: str):
    cfg = load_config()
    cfg['morning_report_last_date'] = date_str
    save_config(cfg)


def mark_morning_prescan(date_str: str):
    cfg = load_config()
    cfg['morning_prescan_last_date'] = date_str
    save_config(cfg)


def get_ingest_cfg() -> dict:
    cfg = load_config()
    return {
        'enabled': cfg.get('ingest_enabled', '1') == '1',
        'interval_min': int(cfg.get('ingest_interval_min') or 5),
    }
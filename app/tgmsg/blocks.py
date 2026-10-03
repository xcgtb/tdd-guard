# -*- coding: utf-8 -*-
"""Telegram 卡片「排版积木」：标题 / 行 / 时间戳 / 扫描块 / 集号区间 / 缺集清单。

全部是纯函数，返回 ``Html`` 或普通字符串；所有外部数据默认转义，
调用方要嵌入已拼好的片段时传 ``Html`` 即可。放在这里是为了让 bot / subscribe /
morning / governance 共用同一套排版，不再各自复制字符串。
"""
import datetime
import re
import time

from . import html as th


def tg_title(icon, title, sub=''):
    """消息标题：粗体标题 + 斜体副标题。"""
    s = '%s <b>%s</b>' % (th.h(icon), th.h(title))
    if sub:
        s += '\n<i>%s</i>' % th.h(sub)
    return th.Html(s)


def tg_row(icon, label, value, note=''):
    """一行「图标 标签　值（斜体备注）」。"""
    s = '%s %s　<b>%s</b>' % (th.h(icon), th.h(label), th.h(value))
    if note:
        s += '　<i>%s</i>' % th.h(note)
    return th.Html(s)


def tg_stamp():
    return datetime.datetime.now().strftime('%m-%d %H:%M')


def fmt_scan_text(icon, title, loc, shr, keep, ex_cnt, ex_groups=0, sub=''):
    """扫描结果块：待清理本地/分享、受保护、白名单豁免（+合并组数备注）。"""
    note = '合并为 %d 组' % ex_groups if ex_groups and ex_groups != ex_cnt else ''
    return th.Html('\n'.join([
        str(tg_title(icon, title, sub or tg_stamp())), '',
        str(tg_row('🧹', '待清理本地', loc)),
        str(tg_row('📤', '待淘汰分享', shr)),
        str(tg_row('🛡️', '受保护', keep)),
        str(tg_row('🏷️', '白名单豁免', ex_cnt, note)),
    ]))


def age_str(ts, empty='—', minutes_only=False):
    """把时间戳格式化成「N 秒/分钟/小时前」。

    minutes_only=True 用于「TMDB 对照」这类只报分钟/小时、不报秒的场景（不足 1 分钟报 0 分钟前）。
    ts 为 0/None 时返回 empty。
    """
    if not ts:
        return empty
    try:
        age = int(time.time() - float(ts))
    except (TypeError, ValueError):
        return empty
    if minutes_only:
        return '%d 小时前' % (age // 3600) if age >= 3600 else '%d 分钟前' % max(age // 60, 0)
    if age < 60:
        return '%d 秒前' % age
    if age < 3600:
        return '%d 分钟前' % (age // 60)
    return '%d 小时前' % (age // 3600)


def _ep_key(sn, en):
    return 'S%02dE%02d' % (int(sn), int(en))


def _parse_ep_key(k):
    m = re.match(r'S(\d+)E(\d+)', k or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


def keys_to_eps(keys):
    """{'S01E01', ...} -> {(季,集)}（忽略解析不出的字符串）。"""
    out = set()
    for k in (keys or []):
        p = _parse_ep_key(k)
        if p:
            out.add(p)
    return out


def ep_ranges(eps):
    """把集列表格式化成人类可读的区间串。

    同一季内连续集号合并：[(1,16),(1,17),(1,18)] -> 'S01E16–E18'
    单集保留完整写法：[(1,16)] -> 'S01E16'
    跨季用 ', ' 连接：[(1,10),(2,1),(2,2)] -> 'S01E10, S02E01–E02'
    """
    items = sorted({(int(sn), int(en)) for sn, en in (eps or []) if sn > 0 and en > 0})
    if not items:
        return ''
    parts = []
    i, n = 0, len(items)
    while i < n:
        sn, lo = items[i]
        hi = lo
        j = i + 1
        while j < n and items[j][0] == sn and items[j][1] == hi + 1:
            hi = items[j][1]
            j += 1
        parts.append(_ep_key(sn, lo) if hi == lo else 'S%02dE%02d–E%02d' % (sn, lo, hi))
        i = j
    if len(parts) > 6:
        parts = parts[:6] + ['…等 %d 集' % n]
    return ', '.join(parts)


def _diff(x):
    return abs((x.get('tmdb_info') or {}).get('diff') or 0)


def gap_list(broken, top=None):
    """缺集明细行：按缺得最多的排最前；top 限制条数。"""
    items = sorted(broken or [], key=lambda x: -_diff(x))
    if top is not None:
        items = items[:top]
    return [th.Html('• 《%s》缺 <b>%d</b> 集' % (th.h(x.get('name')), _diff(x))) for x in items]


def _sub_change_lines(u):
    """单部订阅的「已补齐 / 新增入库 / 缺集」行（三种风格共用）。"""
    out = []
    name = th.h(u.get('name') or '')
    if u.get('refilled'):
        out.append(th.Html('✅ 《%s》已补齐 <b>%s</b>' % (name, ep_ranges(keys_to_eps(u['refilled'])))))
    if u.get('new_eps'):
        out.append(th.Html('📺 《%s》新增 <b>%s</b>' % (name, ep_ranges(keys_to_eps(u['new_eps'])))))
    if u.get('newly_missing'):
        out.append(th.Html('⚠️ 《%s》缺集 <b>%s</b>' % (name, ep_ranges(keys_to_eps(u['newly_missing'])))))
    return out


def sub_updates_lines(updates, style='notify', limit=None):
    """订阅变化的多行文本，供三种场景复用：

      style='notify'  追更推送（subscribe.check_subscriptions 发 Telegram）
      style='check'   Bot /sub check 手动检查卡片
      style='morning' 晨报「订阅更新」一段（含空态与「另有 N 部」收尾）

    返回 ``list[Html]``（'morning' 含表头行；其余含标题 + 空行 + 分片行）。
    """
    updates = updates or []
    n = len(updates)
    passed = updates if limit is None else updates[:limit]
    if style == 'morning':
        lines = [th.Html('🔔 <b>订阅更新</b>　<i>%d 部</i>' % n)]
        if not passed:
            lines.append(th.Html('✅ 无变化'))
        for u in passed:
            lines += _sub_change_lines(u)
        if limit is not None and n > limit:
            lines.append(th.Html('<i>…另有 %d 部有变化</i>' % (n - limit)))
        return lines

    title = '订阅检查' if style == 'check' else '追更订阅'
    lines = [tg_title('🔔', title, '%d 部有变化' % n)]
    for u in passed:
        lines.append(th.Html(''))
        lines.append(th.Html('📺 <b>《%s》</b>' % th.h(u.get('name') or '')))
        if u.get('refilled'):
            lines.append(th.Html('　✅ 已补齐 <b>%s</b>' % ep_ranges(keys_to_eps(u['refilled']))))
        if u.get('new_eps'):
            lines.append(th.Html('　🆕 新增入库 <b>%s</b>' % ep_ranges(keys_to_eps(u['new_eps']))))
        if u.get('newly_missing'):
            lines.append(th.Html('　⚠️ 缺集 <b>%s</b>' % ep_ranges(keys_to_eps(u['newly_missing']))))
    return lines

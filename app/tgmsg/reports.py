# -*- coding: utf-8 -*-
"""纯渲染器集合：把结构化数据渲染成 Telegram HTML 卡片。

这里不碰网络、不读配置、不写状态；调用方（bot / morning / governance）负责取数，
本模块只负责「数据 -> Html」。所有外部数据经 ``h()`` 转义，输出可用
``tg_html_problems`` 校验。
"""
from . import blocks
from . import html as th
from .. import state

# Bot 失败卡片里需要「重新扫描」按钮的错误码
RESCAN_CODES = frozenset({'plan_expired', 'plan_rule_changed', 'plan_not_found', 'plan_used'})
# 晨报明细上限
MORNING_GAP_TOP = 50
MORNING_INGEST_TOP = 60
_WEEK = '一二三四五六日'
_SRC_SHORT = {'本地影视库': '本地', '分享影视库': '分享', '其它库': '其它'}


# ═══════════════════ 扫描 / 清理 ═══════════════════
def scan_card(icon, title, res):
    """双库扫描结果卡片（不含「状态良好 / 清单有效」等尾巴）。"""
    ex_items = res.get('exempted_items') or []
    ex_cnt = res.get('exempted_count', len(ex_items))
    return blocks.fmt_scan_text(icon, title, res.get('del_local_cnt', 0), res.get('del_share_cnt', 0),
                                len(res.get('protected_items') or []), ex_cnt, len(ex_items))


def auto_scan_card(res):
    """定时巡检推送卡片：扫描块 + 干净/清单有效期提示。"""
    text = str(scan_card('⏰', '双库定时巡检', res))
    loc, shr = res.get('del_local_cnt', 0), res.get('del_share_cnt', 0)
    if loc + shr == 0:
        text += '\n\n✅ 双库状态良好，无需清理'
    else:
        text += ('\n\n<i>清单 %d 小时内有效，过期需重新扫描；'
                 '清理不会自动执行，确认后请点下方按钮</i>' % (state.PLAN_TTL // 3600))
    return th.Html(text)


def auto_scan_error(error):
    return th.Html('⚠️ <b>定时巡检失败</b>\n%s' % th.h(error))


def clean_done(loc_cnt, sh_cnt, refreshed, skipped=0):
    rows = [str(blocks.tg_title('🗑️', '清理完成', blocks.tg_stamp())), '',
            str(blocks.tg_row('💾', '释放本地', loc_cnt)),
            str(blocks.tg_row('📤', '淘汰分享', sh_cnt)),
            str(blocks.tg_row('🔄', 'Emby 刷新', '✅' if refreshed else '❌'))]
    text = '\n'.join(rows)
    if skipped:
        text += '\n' + str(blocks.tg_row('⏭️', '跳过', skipped, '状态已变化，未执行'))
    return th.Html(text)


def failure_card(kind, res):
    """任务失败/忙卡片正文；调用方据此错误码选择键盘。"""
    status, code = res.get('status'), res.get('code')
    msg = res.get('message') or ('已有治理任务正在执行，请稍后再试' if status == 'busy' else '未知错误')
    if status == 'busy':
        return th.Html('⏳ %s' % th.h(msg))
    if code in RESCAN_CODES:
        return th.Html('⌛ <b>%s</b>' % th.h(msg))
    # 异常 / 错误信息统一走 error_line（转义），避免直接拼进 HTML
    return error_line('%s 失败' % kind, msg)


def error_line(prefix, exc):
    return th.Html('❌ %s: %s' % (th.h(prefix), th.h(exc)))


def unknown_command(cmd):
    return th.Html('❓ 未知命令: %s\n发送 /help 查看帮助' % th.h(cmd))


# ═══════════════════ 搜索 / 播放 / 日志 ═══════════════════
def search_result(items, total, kw):
    """搜片结果：从结构化 items 渲染（Web API 仍用 ingest 的 Markdown text）。"""
    items = items or []
    if not items:
        return th.Html('❌ 未在两库中检索到包含关键词《%s》的资源。' % th.h(kw))
    found = []
    for it in items[:8]:
        m_type = it.get('type') or '影视库'
        icon = '🎬' if ('电影' in m_type or '演唱会' in m_type) else '📺'
        found.append('%s <b>《%s》</b>\n  ├ 📂 归属库: <code>%s</code>\n'
                     '  ├ 🏷️ 分类: <code>%s / %s</code>\n  └ 📍 路径: <code>%s</code>' % (
                         icon, th.h(it.get('name')), th.h(it.get('lib')),
                         th.h(m_type), th.h(it.get('cat')), th.h(it.get('path'))))
    text = '\n\n'.join(found)
    if total > 8:
        text += '\n\n… 共 %d 条，仅显示前 8 条，请补充关键词缩小范围' % total
    return th.Html(text)


def played_card(records, alerts):
    records, alerts = records or [], alerts or []
    parts = []
    if alerts:
        parts.append('<b>🔔 追更提醒</b>')
        for a in alerts[:5]:
            parts.append('• 👤 %s %s，《%s》今日已入库 E%02d' % (
                th.h(a.get('user')), th.h(a.get('watched')), th.h(a.get('series')), int(a.get('new_ep') or 0)))
    if records:
        parts.append('')
        parts.append('<b>👤 正在观看</b>')
        for r in records[:8]:
            if r.get('type') == 'movie':
                parts.append('• 👤 %s 正在看 🎬《%s》 (进度 %d%%)' % (
                    th.h(r.get('user')), th.h(r.get('name')), int(r.get('pct') or 0)))
            else:
                parts.append('• 👤 %s 正在看 📺《%s》S%02dE%02d (进度 %d%%)' % (
                    th.h(r.get('user')), th.h(r.get('name')), int(r.get('s') or 0),
                    int(r.get('e') or 0), int(r.get('pct') or 0)))
    body = '\n'.join(parts) if parts else '✅ 暂无播放记录'
    return th.Html('▶️ <b>24H 播放记录</b>\n\n' + body)


def logs_card(text):
    if not str(text or '').strip():
        text = '暂无日志'
    inner = th.safe_truncate(th.h(text), 3500)
    return th.Html('📜 <b>审计日志</b>\n\n<pre>%s</pre>' % inner)


# ═══════════════════ 入库 ═══════════════════
def ingest_card(res):
    st = res.get('stats') or {}
    tree = res.get('tree') or {}
    tv_tree = tree.get('tv') or {}
    mov_tree = tree.get('mov') or {}
    age = blocks.age_str(res.get('cache_ts') or 0, empty='—')
    text = '\n'.join([
        str(blocks.tg_title('📊', '24 小时入库',
                            '%s · %s' % ('📦 来自缓存' if res.get('from_cache') else '🔄 现场扫描', age))), '',
        str(blocks.tg_row('🎬', '电影', '+%s 部' % st.get('movies', 0))),
        str(blocks.tg_row('📺', '剧集', '+%s 部' % st.get('series', 0), '+%s 集' % st.get('episodes', 0)))])
    tv_lines, mov_lines = [], []
    for src in ('本地影视库', '分享影视库'):
        for cat in sorted(tv_tree.get(src, {})):
            shows = tv_tree[src][cat]
            tv_lines.append(th.Html('<b>📂 %s · %s</b> (%d部)' % (th.h(src), th.h(cat), len(shows))))
            for n, c in sorted(shows.items(), key=lambda x: x[1], reverse=True):
                tv_lines.append(th.Html('• 《%s》+%s集' % (th.h(n), c)))
    for src in ('本地影视库', '分享影视库'):
        for cat in sorted(mov_tree.get(src, {})):
            names = mov_tree[src][cat]
            mov_lines.append(th.Html('<b>📂 %s · %s</b> (%d部)' % (th.h(src), th.h(cat), len(names))))
            for n in names:
                mov_lines.append(th.Html('• 《%s》' % th.h(n)))
    if tv_lines:
        text += '\n\n📺 <b>剧集明细</b>（点开展开全部 / 再点收起）\n' + str(th.bq(tv_lines))
    if mov_lines:
        text += '\n\n🎬 <b>电影明细</b>（点开展开全部 / 再点收起）\n' + str(th.bq(mov_lines))
    return th.Html(text)


# ═══════════════════ Emby 总览 / 缺集 ═══════════════════
def _gap_stats(icon, title, st, sub='', extra=''):
    rows = [str(blocks.tg_title(icon, title, sub)), '',
            str(blocks.tg_row('📚', '剧集总数', st.get('total', 0))),
            str(blocks.tg_row('✅', '对齐', st.get('aligned', 0), '在更 %s' % st.get('ongoing', 0))),
            str(blocks.tg_row('⚠️', '缺集', st.get('missing', 0),
                              '超集 %s · 未匹配 %s' % (st.get('extra', 0), st.get('unmatched', 0))))]
    if extra:
        rows.append(extra)
    return '\n'.join(rows)


def _broken_block(broken):
    return ('\n\n<b>全部缺集（%d 部）</b>（点开展开 / 再点收起）\n%s'
            % (len(broken), th.bq(blocks.gap_list(broken))))


def emby_overview(res):
    st = res.get('stats') or {}
    broken = res.get('missing') or []
    text = _gap_stats('📺', 'Emby 库总览', st,
                      extra=str(blocks.tg_row('🎬', '电影总数', res.get('movies_total', 0))))
    if broken:
        text += _broken_block(broken)
    return th.Html(text)


def gap_card(res):
    st = res.get('stats') or {}
    broken = res.get('missing') or []
    age = blocks.age_str(res.get('cache_ts') or 0, empty='刚刚对照', minutes_only=True)
    text = _gap_stats('🧩', '缺集检测', st, sub='TMDB 对照 · %s' % age)
    if broken:
        text += _broken_block(broken)
    return th.Html(text)


# ═══════════════════ 订阅 ═══════════════════
def sub_check(updates, total):
    if not updates:
        return th.Html('%s\n\n✅ 无新集、无缺集' % blocks.tg_title('🔔', '订阅检查完成', '共 %d 部' % total))
    return th.Html('\n'.join(str(x) for x in blocks.sub_updates_lines(updates, 'check')))


def sub_notify(updates):
    return th.Html('\n'.join(str(x) for x in blocks.sub_updates_lines(updates, 'notify')))


def sub_list(subs, sub_state):
    if not subs:
        return th.Html('🔔 暂无订阅。\n在 Web「追更订阅」页添加，或从「影视探索」点订阅按钮。')
    lines = [str(blocks.tg_title('🔔', '追更订阅', '共 %d 部' % len(subs))), '']
    for s in subs[:20]:
        sid = s.get('id') or s.get('tmdb_id') or s.get('name')
        st = sub_state.get(sid) or {}
        cur_ep = st.get('latest_ep') or '—'
        tmdb_total = st.get('tmdb_total') or 0
        flag = '' if s.get('enabled', True) else ' (已暂停)'
        extra = ' · TMDB %s 集' % tmdb_total if tmdb_total else ''
        lines.append('• 《%s》 <code>%s</code>%s%s' % (th.h(s.get('name')), th.h(cur_ep), extra, flag))
    if len(subs) > 20:
        lines.append('… 共 %d 部' % len(subs))
    return th.Html('\n'.join(lines))


# ═══════════════════ 晨报 ═══════════════════
def morning_report(data):
    """晨报渲染器。``data`` 由 morning.build_morning_report 采集：

      now         : datetime（标题日期）
      rule_sig    : 统一快照规则指纹
      items       : ['stats', 'subscriptions', 'emby_gap'] 子集
      ingest      : {'stats', 'rows'} 或 {'error'}
      subs        : updates 列表 或 {'error'}
      gap         : gap_report 结果 或 {'error'}
    """
    items = data.get('items') or []
    now = data['now']
    lines = [str(blocks.tg_title('☀️', 'TTD Guard 晨报',
                                 '%s 周%s' % (now.strftime('%Y-%m-%d'), _WEEK[now.weekday()]))),
             '🧭 <i>统一快照 · 规则 %s</i>' % th.h(data.get('rule_sig') or '')]

    if 'stats' in items:
        ing = data.get('ingest') or {}
        if ing.get('error'):
            lines += ['', '📊 入库统计失败: %s' % th.h(ing['error'])]
        else:
            st = ing.get('stats') or {}
            lines += ['', '📊 <b>近 24 小时入库</b>',
                      '🎬 电影　<b>+%s</b> 部' % st.get('movies', 0),
                      '📺 剧集　<b>+%s</b> 部 · <b>+%s</b> 集' % (st.get('series', 0), st.get('episodes', 0))]
            rows = ing.get('rows') or []
            if rows:
                det = [th.Html('• [%s·%s]《%s》%s' % (th.h(_SRC_SHORT.get(src, src)), th.h(cat), th.h(name),
                                                    (' +%d集' % cnt) if cnt else ''))
                       for _ts, src, cat, name, cnt in rows[:MORNING_INGEST_TOP]]
                lines.append(str(th.bq(det)))
                if len(rows) > len(det):
                    lines.append('<i>…另有 %d 条，完整清单见 Web「每日简报」</i>' % (len(rows) - len(det)))

    if 'subscriptions' in items:
        subs = data.get('subs')
        if isinstance(subs, dict) and subs.get('error'):
            lines += ['', '🔔 订阅汇报读取失败: %s' % th.h(subs['error'])]
        else:
            lines += [''] + [str(x) for x in blocks.sub_updates_lines(subs or [], 'morning', limit=10)]

    if 'emby_gap' in items:
        gap = data.get('gap') or {}
        if gap.get('error'):
            lines += ['', '🧩 Emby 检查失败: %s' % th.h(gap['error'])]
        elif gap.get('status') == 'empty':
            lines += ['', '🧩 <b>Emby 缺集</b>　<i>%s</i>' % th.h(gap.get('message') or '暂无缓存')]
            return th.Html('\n'.join(lines))
        else:
            broken = gap.get('missing') or []
            lines += ['', '🧩 <b>Emby 缺集</b>　<i>%d 部 · 共缺 %d 集</i>'
                      % (len(broken), sum(blocks._diff(x) for x in broken))]
            if broken:
                shown = blocks.gap_list(broken, top=MORNING_GAP_TOP)
                lines.append(str(th.bq(shown)))
                if len(broken) > len(shown):
                    lines.append('<i>…另有 %d 部，完整清单见 Web「片库映射」</i>' % (len(broken) - len(shown)))
            else:
                lines.append('✅ 全部对齐')

    return th.Html('\n'.join(lines))

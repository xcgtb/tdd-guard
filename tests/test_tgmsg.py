# -*- coding: utf-8 -*-
"""app/tgmsg 单元测试：转义安全（EVIL 输入）+ 切段/截断性质。

EVIL 同时含「未转义的 HTML 片段」「raw < 与裸 &」「script 标签」：
对每个渲染器的每个文本字段喂一遍，断言输出是合法 Telegram HTML 且不含 <script>。
"""
import random
import re

import pytest

from app.tgmsg import blocks, html as th, reports

EVIL = '<b>&amp;x</b><script>&'

RENDER_OUTPUTS = []


def _add(name, out):
    RENDER_OUTPUTS.append((name, str(out)))


def _collect_evil():
    RENDER_OUTPUTS.clear()
    # blocks
    _add('tg_title', blocks.tg_title(EVIL, EVIL, EVIL))
    _add('tg_row', blocks.tg_row(EVIL, EVIL, EVIL, EVIL))
    _add('fmt_scan_text', blocks.fmt_scan_text(EVIL, EVIL, EVIL, EVIL, EVIL, EVIL, EVIL, EVIL))
    _add('gap_list', '\n'.join(blocks.gap_list([{'name': EVIL, 'tmdb_info': {'diff': -2}}])))
    for style in ('notify', 'check', 'morning'):
        up = [{'name': EVIL, 'refilled': ['S01E01'], 'new_eps': ['S01E02'], 'newly_missing': ['S01E03']}]
        _add('sub_updates_lines/' + style, '\n'.join(str(x) for x in blocks.sub_updates_lines(up, style, limit=5)))
    # reports
    _add('scan_card', reports.scan_card(EVIL, EVIL, {
        'del_local_cnt': 1, 'del_share_cnt': 1, 'protected_items': [1],
        'exempted_items': [1], 'exempted_count': 1}))
    _add('auto_scan_card', reports.auto_scan_card({
        'del_local_cnt': 1, 'del_share_cnt': 1, 'protected_items': [1],
        'exempted_items': [1], 'exempted_count': 1}))
    _add('auto_scan_error', reports.auto_scan_error(EVIL))
    _add('clean_done', reports.clean_done(EVIL, EVIL, True, EVIL))
    _add('failure_error', reports.failure_card(EVIL, {'status': 'error', 'message': EVIL}))
    _add('failure_rescan', reports.failure_card('x', {'status': 'error', 'code': 'plan_expired', 'message': EVIL}))
    _add('failure_busy', reports.failure_card('x', {'status': 'busy', 'message': EVIL}))
    _add('error_line', reports.error_line(EVIL, EVIL))
    _add('unknown_command', reports.unknown_command(EVIL))
    _add('search_item', reports.search_result(
        [{'name': EVIL, 'lib': EVIL, 'type': EVIL, 'cat': EVIL, 'path': EVIL}], 1, EVIL))
    _add('search_empty', reports.search_result([], 0, EVIL))
    _add('played', reports.played_card(
        [{'user': EVIL, 'name': EVIL, 'type': 'tv', 's': 1, 'e': 2, 'pct': 3},
         {'user': EVIL, 'name': EVIL, 'type': 'movie', 'pct': 5}],
        [{'user': EVIL, 'series': EVIL, 'watched': EVIL, 'new_ep': 3}]))
    _add('logs', reports.logs_card(EVIL))
    _add('emby_overview', reports.emby_overview({
        'stats': {'total': 1, 'aligned': 0, 'missing': 1, 'extra': 0, 'ongoing': 0, 'unmatched': 0},
        'movies_total': 1, 'missing': [{'name': EVIL, 'tmdb_info': {'diff': -2}}]}))
    _add('gap_card', reports.gap_card({
        'stats': {'total': 1, 'aligned': 0, 'missing': 1, 'extra': 0, 'ongoing': 0, 'unmatched': 0},
        'cache_ts': 0, 'missing': [{'name': EVIL, 'tmdb_info': {'diff': -2}}]}))
    _add('sub_check', reports.sub_check(
        [{'name': EVIL, 'new_eps': ['S01E01'], 'refilled': [], 'newly_missing': []}], 1))
    _add('sub_list', reports.sub_list(
        [{'id': '1', 'name': EVIL, 'enabled': True}], {'1': {'latest_ep': EVIL, 'tmdb_total': 5}}))
    _add('sub_notify', reports.sub_notify(
        [{'name': EVIL, 'new_eps': ['S01E01'], 'refilled': [], 'newly_missing': []}]))
    # morning：标题/规则/入库明细/订阅/缺集/错误信息全部喂 EVIL
    import datetime
    now = datetime.datetime(2026, 1, 1, 12, 0, 0)
    data = {'now': now, 'rule_sig': EVIL, 'items': ['stats', 'subscriptions', 'emby_gap'],
            'ingest': {'stats': {'movies': 1, 'series': 1, 'episodes': 1},
                       'rows': [(1.0, EVIL, EVIL, EVIL, 1)]},
            'subs': [{'name': EVIL, 'new_eps': ['S01E01'], 'refilled': [], 'newly_missing': []}],
            'gap': {'status': 'success', 'missing': [{'name': EVIL, 'tmdb_info': {'diff': -1}}]}}
    _add('morning', reports.morning_report(data))
    data['ingest'] = {'error': EVIL}
    data['subs'] = {'error': EVIL}
    data['gap'] = {'error': EVIL}
    _add('morning_errors', reports.morning_report(data))
    data['gap'] = {'status': 'empty', 'message': EVIL}
    _add('morning_empty', reports.morning_report(data))


def test_all_renderer_outputs_are_valid_html():
    _collect_evil()
    for name, out in RENDER_OUTPUTS:
        assert th.tg_html_problems(out) == [], (name, out, th.tg_html_problems(out))


def test_evil_script_never_survives():
    _collect_evil()
    for name, out in RENDER_OUTPUTS:
        assert '<script' not in out and '</script' not in out, (name, out)


# ═══════════════════ safe_truncate 性质 ═══════════════════
def test_safe_truncate_closes_tags():
    out = th.safe_truncate('<b>abcdef</b>', 8)
    assert len(out) <= 8
    assert th.tg_html_problems(out) == []
    assert out.startswith('<b>') and out.endswith('</b>')


@pytest.mark.parametrize('limit', [10, 16, 40, 4096])
def test_safe_truncate_never_cuts_tag_or_entity(limit):
    text = '<b>' + 'x' * 50 + '&amp;' + 'y' * 50 + '</b>'
    out = th.safe_truncate(text, limit)
    assert len(out) <= limit
    assert th.tg_html_problems(out) == []


def test_safe_truncate_noop_when_short():
    assert th.safe_truncate('abc', 10) == 'abc'


# ═══════════════════ split_telegram_html 性质 ═══════════════════
_TAG_RE = re.compile(r'<[^>]*>')


def _gen(rand, depth=0):
    tags = ['b', 'i', 'code', 'blockquote']
    out = []
    for _ in range(rand.randint(1, 6)):
        if depth < 3 and rand.random() < 0.5:
            t = rand.choice(tags)
            op = '<blockquote expandable>' if t == 'blockquote' else '<%s>' % t
            out.append(op + _gen(rand, depth + 1) + '</%s>' % t)
        else:
            out.append('x' * rand.randint(1, 300))
    return ''.join(out)


# limit 必须大于「最深重开标签 + 其闭合标签」的长度，否则数学上不存在合法段
@pytest.mark.parametrize('limit', [120, 500, 3800])
def test_split_properties(limit):
    rand = random.Random(1234)
    text = ''.join(_gen(rand) for _ in range(4))
    chunks = th.split_telegram_html(text, limit)
    assert chunks, '至少一段'
    for c in chunks:
        assert len(c) <= limit, (limit, len(c))
        assert th.tg_html_problems(c) == [], (limit, c[:80])
    # 文本内容不丢：拼起来去掉标签应与原文一致
    joined = _TAG_RE.sub('', ''.join(chunks))
    assert joined == _TAG_RE.sub('', text)


def test_split_short_is_single_chunk():
    assert th.split_telegram_html('hello', limit=3800) == ['hello']


def test_split_hard_breaks_a_single_long_line():
    text = 'x' * 100
    chunks = th.split_telegram_html(text, limit=30)
    assert len(chunks) == 4
    assert ''.join(chunks) == text
    for c in chunks:
        assert len(c) <= 30

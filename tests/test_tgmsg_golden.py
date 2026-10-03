# -*- coding: utf-8 -*-
"""金样回归：重构后各渲染器对「正常输入」的输出必须与重构前逐字一致。

fixtures/tgmsg_golden.json 由 scripts/_capture_tgmsg_golden.py 在重构前驱动旧代码采集
（含 bot._notify_result / notify_auto_scan / morning.build_morning_report 的真实输出）。
"""
import datetime
import json
import time
from pathlib import Path

from app.tgmsg import blocks, reports

FIXTURES = json.loads((Path(__file__).resolve().parent / 'fixtures' / 'tgmsg_golden.json')
                      .read_text(encoding='utf-8'))

UPS = [
    {'sid': 's1', 'name': '甲剧', 'tmdb_id': '1', 'new_eps': ['S01E16', 'S01E17'],
     'missing_eps': [], 'newly_missing': [], 'refilled': []},
    {'sid': 's2', 'name': '乙剧', 'tmdb_id': '2', 'new_eps': [],
     'missing_eps': ['S02E03'], 'newly_missing': ['S02E03'], 'refilled': ['S01E09']},
]
SUBS = [{'id': 's1', 'name': '甲剧', 'enabled': True}, {'id': 's2', 'name': '乙剧', 'enabled': False}]
SUB_STATE = {'s1': {'latest_ep': 'S01E17', 'tmdb_total': 20}, 's2': {'latest_ep': 'S02E03'}}


def _fixed_stamp(monkeypatch):
    monkeypatch.setattr(blocks, 'tg_stamp', lambda: '01-02 03:04')


def test_blocks_golden(monkeypatch):
    _fixed_stamp(monkeypatch)
    assert str(blocks.tg_title('🔍', '扫描完成', '副标题')) == FIXTURES['tg_title']
    assert str(blocks.tg_title('🔍', '扫描完成')) == FIXTURES['tg_title_nosub']
    assert str(blocks.tg_row('📚', '剧集总数', 12, '备注')) == FIXTURES['tg_row']
    assert str(blocks.tg_row('📚', '剧集总数', 12)) == FIXTURES['tg_row_nonote']
    assert str(blocks.fmt_scan_text('🔍', '双库扫描完成', 3, 2, 5, 4, 2)) == FIXTURES['fmt_scan_text']
    assert str(blocks.fmt_scan_text('🔍', '双库扫描完成', 3, 2, 5, 4, 2, '自定义')) == FIXTURES['fmt_scan_text_stamp']


def test_clean_and_logs_golden(monkeypatch):
    _fixed_stamp(monkeypatch)
    assert str(reports.clean_done(2, 1, True, 3)) == FIXTURES['card_clean'][0]
    assert str(reports.clean_done(0, 0, False)) == FIXTURES['card_clean_noskip'][0]
    assert str(reports.logs_card('line1\nline2')) == FIXTURES['card_logs'][0]


def test_ingest_emby_gap_golden():
    assert str(reports.ingest_card({
        'from_cache': True, 'cache_ts': time.time() - 130,
        'stats': {'movies': 2, 'series': 3, 'episodes': 18},
        'tree': {'tv': {'本地影视库': {'国产剧': {'剧甲': 5, '剧乙': 2}}},
                 'mov': {'分享影视库': {'外语电影': ['电影甲']}}}})) == FIXTURES['card_ingest'][0]
    assert str(reports.emby_overview({
        'stats': {'total': 10, 'aligned': 7, 'missing': 3, 'extra': 1, 'ongoing': 2, 'unmatched': 0},
        'movies_total': 5,
        'missing': [{'name': '甲剧', 'tmdb_info': {'diff': -2}},
                    {'name': '乙剧', 'tmdb_info': {'diff': -7}}]})) == FIXTURES['card_emby'][0]
    assert str(reports.gap_card({
        'stats': {'total': 10, 'aligned': 7, 'missing': 3, 'extra': 1, 'ongoing': 2, 'unmatched': 0},
        'cache_ts': time.time() - 7200,
        'missing': [{'name': '甲剧', 'tmdb_info': {'diff': -2}}]})) == FIXTURES['card_gap'][0]


def test_auto_scan_and_failure_golden(monkeypatch):
    _fixed_stamp(monkeypatch)
    assert str(reports.auto_scan_card({
        'status': 'success', 'del_local_cnt': 3, 'del_share_cnt': 2,
        'protected_items': [1, 2, 3, 4, 5], 'exempted_items': [1, 2, 3, 4],
        'exempted_count': 4, 'plan_id': None})) == FIXTURES['auto_scan'][0]
    assert str(reports.auto_scan_card({
        'status': 'success', 'del_local_cnt': 0, 'del_share_cnt': 0,
        'protected_items': [], 'exempted_items': [], 'plan_id': None})) == FIXTURES['auto_scan_clean'][0]
    assert str(reports.auto_scan_error('扫描炸了')) == FIXTURES['auto_scan_error'][0]
    assert str(reports.failure_card('emby', {'status': 'error', 'message': '拉取失败'})) == FIXTURES['card_emby_error'][0]


def test_sub_golden():
    assert str(reports.sub_check(UPS, 12)) == FIXTURES['sub_check'][1]
    assert str(reports.sub_check([], 12)) == FIXTURES['sub_check_empty'][1]
    assert str(reports.sub_list(SUBS, SUB_STATE)) == FIXTURES['sub_list'][0]
    assert str(reports.sub_list([], {})) == FIXTURES['sub_list_empty'][0]


def test_morning_golden():
    now = datetime.datetime.fromtimestamp(1767225600.0)
    data = {'now': now, 'rule_sig': 'abc123', 'items': ['stats', 'subscriptions', 'emby_gap'],
            'ingest': {'stats': {'movies': 2, 'series': 3, 'episodes': 18},
                       'rows': [(100.0, '本地影视库', '国产剧', '剧甲', 5),
                                (80.0, '分享影视库', '外语电影', '电影甲', 0),
                                (50.0, '本地影视库', '国产剧', '剧乙', 2)]},
            'subs': UPS,
            'gap': {'status': 'success', 'missing': [{'name': '甲剧', 'tmdb_info': {'diff': -2}},
                                                     {'name': '乙剧', 'tmdb_info': {'diff': -7}}]}}
    assert str(reports.morning_report(data)) == FIXTURES['morning']
    data2 = {'now': now, 'rule_sig': 'abc123', 'items': ['stats', 'subscriptions', 'emby_gap'],
             'ingest': {'stats': {'movies': 0, 'series': 0, 'episodes': 0}, 'rows': []},
             'subs': [], 'gap': {'status': 'empty', 'message': '暂无缓存', 'missing': []}}
    assert str(reports.morning_report(data2)) == FIXTURES['morning_empty_gap']

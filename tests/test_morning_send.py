# -*- coding: utf-8 -*-
from app import morning, tg


class _Cfg:
    def __init__(self):
        self.marked = []
    def mark_morning_report_sent(self, value):
        self.marked.append(value)


class _Tg:
    def __init__(self):
        self.sent = []
    def notify_telegram(self, text):
        self.sent.append(text)
        return True


def _run(mark_sent):
    cfg = _Cfg(); fake_tg = _Tg()
    old_cfg, old_notify, old_build = morning._cfg, tg.notify_telegram, morning.build_morning_report
    try:
        morning._cfg = cfg
        tg.notify_telegram = fake_tg.notify_telegram   # morning 在调用时取 tg.notify_telegram
        morning.build_morning_report = lambda items, force_refresh=False: 'cached'
        assert morning.send_morning_report(['stats'], force_refresh=False, mark_sent=mark_sent) is True
        return cfg, fake_tg
    finally:
        morning._cfg, tg.notify_telegram, morning.build_morning_report = old_cfg, old_notify, old_build


def test_manual_send_does_not_mark_daily_send():
    cfg, fake_tg = _run(False)
    assert cfg.marked == []
    assert fake_tg.sent == ['cached']


def test_scheduled_send_marks_daily_send():
    cfg, fake_tg = _run(True)
    assert len(cfg.marked) == 1
    assert fake_tg.sent == ['cached']

# -*- coding: utf-8 -*-
"""
main.py 测试：鉴权（auth）+ 任务系统（TASKS 清理）。

main.py 之前完全没有测试保护——它是外部输入直接进来的那一层
（Web 端点、Basic Auth），所以这里先补最基本的两块：
  - auth()：正确/错误用户名密码、缺失凭证都要挡住
  - 未设置 WEB_PASSWORD 且未显式 ALLOW_NO_AUTH=1 时，进程必须拒绝启动
    （这条用子进程测，因为 WEB_PASSWORD 是 main.py 导入时就读掉的模块级常量，
    没法在同一个进程里换着测两种分支）
  - _prune_tasks()：TASKS 字典不能无限堆积

运行方式同其它测试：`python3 tests/run_engine_tests.py` 或 `pytest tests/ -v`。
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix='ttdguard_test_main_'))
os.environ['L_ROOT'] = str(_TMP / 'local')
os.environ['S_ROOT'] = str(_TMP / 'share')
os.environ['CLOUD_L_ROOT'] = str(_TMP / 'cloud')
os.environ['AGENT_DATA'] = str(_TMP / 'data')
os.environ['TMDB_KEY'] = ''
os.environ['TG_BOT_TOKEN'] = ''
os.environ['WEB_USER'] = 'admin'
os.environ['WEB_PASSWORD'] = 'test-pass-123'

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
sys.path.insert(0, _REPO_ROOT)

import app.main as main  # noqa: E402
from fastapi.security import HTTPBasicCredentials  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _cred(u, p):
    return HTTPBasicCredentials(username=u, password=p)


class TestAuth:
    def test_correct_credentials_pass(self):
        assert main.auth(_cred('admin', 'test-pass-123')) is True

    def test_wrong_username_rejected(self):
        try:
            main.auth(_cred('nope', 'test-pass-123'))
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401

    def test_wrong_password_rejected(self):
        try:
            main.auth(_cred('admin', 'wrong'))
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401

    def test_missing_credentials_rejected(self):
        # HTTPBasic(auto_error=False) 在没带 Authorization 头时会传 None 进来
        try:
            main.auth(None)
            assert False, '应该抛出 401'
        except HTTPException as e:
            assert e.status_code == 401


class TestStartupRequiresPassword:
    def _run(self, env_overrides):
        env = dict(os.environ)
        env.update(env_overrides)
        code = (
            "import sys; sys.path.insert(0, %r)\n"
            "import app.main\n"
            "print('IMPORTED_OK')\n"
        ) % _REPO_ROOT
        return subprocess.run(
            [sys.executable, '-c', code],
            env=env, capture_output=True, text=True, timeout=90,
        )

    def test_refuses_without_password_and_without_allow_no_auth(self):
        r = self._run({'WEB_PASSWORD': '', 'ALLOW_NO_AUTH': ''})
        assert r.returncode != 0, f'应该拒绝启动，实际 returncode={r.returncode}'
        assert 'IMPORTED_OK' not in r.stdout

    def test_starts_without_password_when_allow_no_auth_set(self):
        r = self._run({'WEB_PASSWORD': '', 'ALLOW_NO_AUTH': '1'})
        assert r.returncode == 0, f'应该正常导入，实际 stderr={r.stderr[-2000:]}'
        assert 'IMPORTED_OK' in r.stdout


class TestPruneTasks:
    def _fake_task(self, status, ts):
        t = main.Task('fake')
        t.status = status
        t.ts = ts
        return t

    def test_keeps_running_task_and_caps_finished(self):
        main.TASKS.clear()
        old_cap = main.TASKS_MAX_KEEP
        main.TASKS_MAX_KEEP = 3
        try:
            running = self._fake_task('running', time.time())
            main.TASKS[running.id] = running
            finished = []
            for i in range(6):
                t = self._fake_task('success', time.time() + i)
                main.TASKS[t.id] = t
                finished.append(t)

            main._prune_tasks()

            assert running.id in main.TASKS, '正在运行的任务不该被清理'
            remaining_finished = [t for t in finished if t.id in main.TASKS]
            assert len(remaining_finished) == 3, \
                f'完成态任务应该只保留 3 个，实际剩 {len(remaining_finished)}'
            # 保留下来的应该是时间戳最新的那几个
            kept_ts = sorted(t.ts for t in remaining_finished)
            assert kept_ts == sorted(t.ts for t in finished)[-3:]
        finally:
            main.TASKS_MAX_KEEP = old_cap
            main.TASKS.clear()

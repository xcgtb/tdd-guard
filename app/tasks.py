# -*- coding: utf-8 -*-
"""统一任务总线：Web / Telegram Bot / 定时巡检共用同一份任务登记与互斥。

以前 Web 有自己的 TASKS/CURRENT，Bot 自己起线程跑扫描/清理，两边互相看不见：
Bot 发起的扫描可以和 Web 的扫描同时跑，Web 的「已有任务在执行」也拦不住 Bot。
现在所有要遍历双库或改文件的重任务（exclusive=True）都经这里登记，同一时刻只允许一个；
轻量只读任务（exclusive=False）只登记、不占位，方便排查。
"""
import logging
import threading
import time
import uuid

log = logging.getLogger('media_agent')

MAX_KEEP = 200      # 完成态任务最多保留这么多份，超出按时间淘汰最旧的
FINISHED_TTL = 3600  # 完成态任务保留 1 小时；运行中的任务永远不清理
MAX_LOGS = 500


class TaskBusy(Exception):
    def __init__(self, current):
        self.current = current
        super().__init__(f'已有任务 [{current.kind}] 正在执行，请稍候')


class _TaskStream:
    """logging.StreamHandler 的目标：按行写进任务日志，前端轮询 /api/task/{id} 展示"""
    def __init__(self, task):
        self.task = task; self.buf = ''

    def write(self, s):
        self.buf += s
        while '\n' in self.buf:
            line, self.buf = self.buf.split('\n', 1)
            self._append(line)

    def flush(self):
        if self.buf.strip():
            self._append(self.buf); self.buf = ''

    def _append(self, line):
        line = line.strip()
        if line:
            self.task.logs.append(line)
            if len(self.task.logs) > MAX_LOGS: del self.task.logs[:100]


class Task:
    def __init__(self, kind, source='web', exclusive=True):
        self.id = uuid.uuid4().hex[:12]; self.kind = kind
        self.source = source; self.exclusive = exclusive
        self.status = 'running'; self.result = None; self.error = None
        self.logs = []; self.ts = time.time(); self.stream = _TaskStream(self)
        self.done = threading.Event()

    def to_dict(self, with_logs=True):
        d = {'id': self.id, 'kind': self.kind, 'source': self.source, 'status': self.status,
             'result': self.result, 'error': self.error, 'ts': self.ts}
        if with_logs: d['logs'] = self.logs[-200:]
        return d


class TaskManager:
    def __init__(self, logger_name='media_agent'):
        self.tasks = {}
        self.lock = threading.Lock()
        self.current = None
        self.logger_name = logger_name
        self.max_keep = MAX_KEEP
        self.finished_ttl = FINISHED_TTL

    def running(self):
        """当前占位的重任务（没有则 None）"""
        cur = self.current
        return cur if cur is not None and cur.status == 'running' else None

    def get(self, tid):
        return self.tasks.get(tid)

    def spawn(self, kind, fn, *fargs, exclusive=True, source='web', on_done=None):
        """后台线程执行 fn(*fargs)。exclusive 任务在已有重任务运行时抛 TaskBusy。
        on_done(task) 在任务结束后于同一线程回调（Bot 用它回消息）。"""
        with self.lock:
            # 检查和占位在同一把锁里完成：以前是先查 CURRENT、等线程跑起来才占位，
            # 两个请求前后脚进来能同时通过检查
            if exclusive and self.running() is not None:
                raise TaskBusy(self.current)
            t = Task(kind, source=source, exclusive=exclusive)
            self.tasks[t.id] = t
            if exclusive:
                self.current = t
        self.prune()
        threading.Thread(target=self._run, args=(t, fn, fargs, on_done),
                         daemon=True, name=f'task-{kind}').start()
        return t

    def _run(self, t, fn, fargs, on_done):
        me = threading.get_ident()
        handler = logging.StreamHandler(t.stream)
        handler.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
        # 只收本任务线程打出的日志；以前 handler 挂在全局 logger 上，
        # 任务运行期间后台轮询、别的请求打的日志也会混进任务日志
        handler.addFilter(lambda r: r.thread == me)
        lg = logging.getLogger(self.logger_name)
        lg.addHandler(handler)
        try:
            res = fn(*fargs)
            t.result = res
            # busy（跨入口文件锁被占用）也按失败处理，否则网页会当成"执行成功"，显示 0 项/undefined
            if isinstance(res, dict) and res.get('status') in ('error', 'busy'):
                t.status = 'error'; t.error = res.get('message', '未知错误')
            else:
                t.status = 'success'
        except Exception as e:
            lg.exception('任务 [%s] 执行失败', t.kind)
            t.error = f'{type(e).__name__}: {e}'; t.status = 'error'
        finally:
            lg.removeHandler(handler)
            with self.lock:
                if self.current is t:
                    self.current = None
            t.done.set()
        if on_done is not None:
            try:
                on_done(t)
            except Exception:
                log.exception('任务 [%s] 回调失败', t.kind)

    def prune(self):
        """淘汰完成态任务：超过保留时长的、以及超出数量上限的最旧那些。
        运行中的任务（哪怕跑了一个多小时）永远不动，否则前端轮询会拿到 404。"""
        cut = time.time() - self.finished_ttl
        with self.lock:
            done = sorted((t for t in self.tasks.values() if t.status != 'running'),
                          key=lambda t: t.ts)
            overflow = len(done) - self.max_keep
            for i, t in enumerate(done):
                if i < overflow or t.ts < cut:
                    self.tasks.pop(t.id, None)


manager = TaskManager()

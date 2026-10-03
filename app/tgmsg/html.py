# -*- coding: utf-8 -*-
"""Telegram HTML 拼装与校验（叶子模块，只依赖标准库）。

设计要点：
  - ``Html`` 是「已经是安全片段」的标记类型（str 子类）。``h()`` 遇到它原样返回，
    遇到普通字符串才转义 —— 这样内部拼接不会二次转义，而外部数据一定被转义。
  - ``b() / i() / code() / pre()`` 默认转义，同样放行 ``Html``。
  - ``safe_truncate`` / ``split_telegram_html`` 保证截断/切段后标签闭合、实体完整。
  - ``tg_html_problems`` 是唯一校验器：只认 Telegram 支持的标签、要求标签配平、
    拒绝裸 ``<`` 与裸 ``&``（在 tests 里对每个渲染器的文本字段喂 EVIL 输入）。
"""
import re
from html import escape as _escape
from html.parser import HTMLParser

# Telegram Bot API 支持的 HTML 标签（tg-spoiler 是唯一带连字符的）
ALLOWED_TAGS = frozenset({'b', 'strong', 'i', 'em', 'u', 's', 'code', 'pre', 'a', 'blockquote', 'tg-spoiler'})


class Html(str):
    """一段已经拼好、无需再转义的 Telegram HTML 片段。"""


def h(x):
    """转义为安全片段；``Html`` 原样返回（不二次转义），None -> ''。"""
    if isinstance(x, Html):
        return x
    return Html(_escape(str(x) if x is not None else '', quote=False))


def _wrap(tag, x):
    return Html('<' + tag + '>' + str(h(x)) + '</' + tag + '>')


def b(x):
    return _wrap('b', x)


def i(x):
    return _wrap('i', x)


def code(x):
    return _wrap('code', x)


def pre(x):
    return _wrap('pre', x)


def bq(lines, expandable=True):
    """折叠引用块：每行先 ``h()``（Html 原样），默认可点击展开。"""
    body = '\n'.join(str(h(x)) for x in (lines or []))
    tag = '<blockquote expandable>' if expandable else '<blockquote>'
    return Html(tag + body + '</blockquote>')


def join(sep, parts):
    """用 sep 连接若干片段（每段先转义，Html 原样）。"""
    return Html(str(sep).join(str(h(p)) for p in (parts or [])))


# ── 轻量分词：完整标签 / 实体 / 单个字符 ──
_TOKEN_RE = re.compile(r'<[^>]*>|&[#0-9A-Za-z]+;|.', re.S)


def _open_name(tok):
    """开标签的标签名；不是开标签返回 None。"""
    if not (tok.startswith('<') and tok.endswith('>') and len(tok) > 2):
        return None
    inner = tok[1:-1].strip()
    if not inner or inner[0] in '/!?':
        return None
    return inner.split()[0].rstrip('/').lower() or None


def _close_name(tok):
    if tok.startswith('</') and tok.endswith('>'):
        return tok[2:-1].strip().lower().split()[0] if tok[2:-1].strip() else ''
    return ''


def _track_stack(tok, stack):
    """按 token 更新未闭合标签栈（存完整开标签串，便于重开）。"""
    name = _open_name(tok)
    if name is not None:
        if not tok.endswith('/>'):
            stack.append(tok)
        return
    close = _close_name(tok)
    if close:
        for j in range(len(stack) - 1, -1, -1):
            if _open_name(stack[j]) == close:
                del stack[j:]
                return


def _closings(stack):
    return ''.join('</%s>' % _open_name(t) for t in reversed(stack))


def safe_truncate(text, limit=4096, marker='…'):
    """按 limit（结果总长上限）截断，绝不切在标签/实体中间，并闭合所有未闭合标签。"""
    text = str(text)
    if len(text) <= limit:
        return text
    if limit <= len(marker):
        return marker[:limit]
    kept, stack = [], []
    n = 0
    budget = limit - len(marker)
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0)
        if n + len(tok) > budget:
            break
        kept.append(tok)
        n += len(tok)
        _track_stack(tok, stack)
    # 闭合标签要占长度：逐字回退，直到「正文 + 省略号 + 闭合标签」不超限
    while kept:
        closings = _closings(stack)
        if n + len(marker) + len(closings) <= limit:
            return ''.join(kept) + marker + closings
        # 退回一个 token 并重建栈
        kept.pop()
        stack = []
        for t in kept:
            _track_stack(t, stack)
        n = sum(len(t) for t in kept)
    return marker[:limit]


def split_telegram_html(text, limit=3800):
    """把超长 HTML 切成每段 ≤ limit 且标签自洽的多段。

    通用实现（不再只认 blockquote）：维护未闭合标签栈，在某段放不下下一个 token 时，
    本段补上闭合标签、下一段把同样的开标签重开，文本内容一字不丢（标签会被成对复制）。
    """
    text = str(text)
    if len(text) <= limit:
        return [text]
    tokens = [m.group(0) for m in _TOKEN_RE.finditer(text)]
    chunks, cur, stack = [], [], []
    curlen = 0
    prefix_len = 0        # 当前段开头的「重开标签」长度，低于它说明整段还没有正文
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        # 试算「加上这个 token 后」本段总长（含闭合标签）——开标签会拉长闭合串，必须一起算
        trial = list(stack)
        _track_stack(tok, trial)
        if curlen > prefix_len and curlen + len(tok) + len(_closings(trial)) > limit:
            chunks.append(''.join(cur) + _closings(stack))
            reopen = ''.join(stack)
            cur = [reopen] if reopen else []
            curlen = prefix_len = len(reopen)
            continue      # 同一个 token 在新段里重试
        cur.append(tok)
        curlen += len(tok)
        stack = trial
        i += 1
    if cur:
        chunks.append(''.join(cur))
    return chunks


class _Validator(HTMLParser):
    """convert_charrefs=False：实体走 handle_entityref，裸 & 会留在 data 里被发现。"""

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.problems = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.problems.append('不允许的标签 <%s>' % tag)
            return
        self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.problems.append('不支持自闭合标签 <%s/>' % tag)

    def handle_endtag(self, tag):
        if tag not in ALLOWED_TAGS:
            self.problems.append('不允许的标签 </%s>' % tag)
            return
        if not self.stack or self.stack[-1] != tag:
            self.problems.append('标签未正确配平 </%s>' % tag)
            while self.stack and self.stack.pop() != tag:
                pass
            return
        self.stack.pop()

    def handle_data(self, data):
        if '<' in data:
            self.problems.append('裸 < 未转义')
        if '&' in data:
            self.problems.append('裸 & 未转义')

    def handle_entityref(self, name):
        pass

    def handle_charref(self, name):
        pass

    def handle_comment(self, data):
        self.problems.append('不支持 HTML 注释')

    def handle_decl(self, decl):
        self.problems.append('不支持声明语句')

    def handle_pi(self, data):
        self.problems.append('不支持处理指令')


def tg_html_problems(text):
    """返回 Telegram HTML 的问题列表，空列表代表合法。"""
    v = _Validator()
    try:
        v.feed(str(text))
        v.close()
    except Exception as e:  # HTMLParser 极少抛，但绝不能让校验器把测试搞崩
        v.problems.append('解析失败: %s' % e)
    if v.stack:
        v.problems.append('标签未闭合: %s' % ','.join(v.stack))
    return v.problems

# -*- coding: utf-8 -*-
"""启动进度条动画：一个 Q 版小炼丹炉，炉腔里随进度装丹药，最后开盖冒烟。

设计目标（用户要求）：简单、Q 版、易懂。
只讲清三件事：炉腔里的丹药装了多少、炉口的火有多旺、下面一行中文现在在干嘛。
不加转场动画 —— 黑窗口宽度不统一，窄窗口里复杂的画会折行，反而更难懂。

兼容性：
- main() 已把 stdout 重配成 UTF-8；这里仍保留**纯文本降级**：
  非 TTY（重定向到文件、CI、管道）时自动换成单行勾选列表，
  不然进度条的 \\r 回车覆盖会在日志里堆成一团乱码。
"""
import os
import sys
import time

WIDTH = 20          # 炉腔内腔宽度（格）
FIRE_MAX = 4        # 火苗最大高度

# 阶段：短句，扫一眼就知道卡在哪
PHASES = [
    ('准备中', 10),
    ('检查运行环境', 25),
    ('寻找 ComfyUI', 55),
    ('连接丹炉', 80),
    ('点火开炉', 95),
    ('完成', 100),
]

_FACES = ['^.^', '-.-', '^.^', 'o.o']   # Q 版眼睛表情轮换
_FLAME = ['.', '|', '/', '#', '@']


def _app_title():
    try:
        from server import APP_TITLE
        return APP_TITLE
    except Exception:
        return 'HX-验丹炉'


def _furnace(pct, face):
    """画一个炉子。返回多行字符串。

    造型是「三脚炼丹炉」：炉口一团火，炉身两只眼睛，中间炉腔装丹药。
    丹药**从左往右填满炉腔**（不是上涨的液面）—— 横向填充在窄终端里
    更好读，也不会因为终端高度不够被截掉。
    """
    inner = WIDTH
    filled = int(round(inner * max(0, min(100, pct)) / 100.0))
    # 每格丹药占 2 列，炉腔宽度才等于炉身宽度
    cavity = ''.join(('##' if i < filled else '..') for i in range(inner))

    # 火苗随进度从 . 长到 @
    idx = min(len(_FLAME) - 1, int(pct / 100.0 * len(_FLAME)))
    fire = _FLAME[idx]

    #炉身内部净宽（不含两侧壁）
    body_w = inner * 2
    # 眼睛在净宽里居中
    eye = face.center(body_w)

    return [
        '     %s   ' % fire,
        '   .%s.' % ('-' * (body_w + 2)),
        '  /%s\\' % eye,
        ' |%s|' % cavity,
        '  \\%s/' % ('_' * body_w),
    ]


class Furnace:
    """Q 版炼丹炉进度条。

        fz = Furnace()
        fz.start()
        fz.step('寻找 ComfyUI')
        fz.done()
    """

    def __init__(self, enabled=None):
        no_bar = os.environ.get('LAB_NO_BAR') or '--no-bar' in sys.argv
        if enabled is None:
            enabled = (sys.stdout is not None
                       and getattr(sys.stdout, 'isatty', lambda: False)()
                       and not no_bar)
        self.enabled = bool(enabled)
        self.pct = 0
        self.frame = 0
        self._last_len = 0
        self._started = False
        self._step_no = 1

    # ---------- 底层
    def _write(self, s):
        try:
            sys.stdout.write(s)
            sys.stdout.flush()
        except Exception:
            self.enabled = False

    def _clear_line(self):
        if self._last_len:
            self._write('\r' + ' ' * self._last_len + '\r')
            self._last_len = 0

    # ---------- 降级模式：一行一个勾
    def _plain(self, text):
        # 首行已经由 start() 打过头了，这里跳过避免重复
        if not self._started:
            self._started = True
            self._write('  [1/5] %s\n' % (text or ''))
            return
        self._step_no = min(self._step_no + 1, 99)
        self._write('  [%d/5] %s\n' % (self._step_no, text or ''))

    # ---------- 对外
    def start(self):
        if not self.enabled:
            return
        self._clear_line()
        self._write('\n  %s 正在开炉 ...\n' % _app_title())
        self.frame = 0
        self.paint('准备中', 0)

    def paint(self, text, pct=None):
        """原地重画一帧（不清屏、不换行）。"""
        if not self.enabled:
            return
        pct = self.pct if pct is None else pct
        face = _FACES[self.frame % len(_FACES)]
        art = _furnace(pct, face)
        # 阶段文案放在炉子右边，与炉身同高
        art[2] = '%s  %s' % (art[2], text or '')
        art[3] = '%s  [%s] %d%%' % (
            art[3], '#' * int(round(WIDTH * pct / 100.0)), pct)
        for r in art:
            self._write('\r' + '  ' + r.ljust(self._last_len) + '\n\r')
        self._last_len = max(len('  ' + r) for r in art)
        self.frame += 1

    def step(self, text=None, pct=None):
        if pct is None:
            pct = self._next_pct(text)
        pct = max(self.pct, min(100, int(pct)))
        self.pct = pct
        if not self.enabled:
            self._plain(text)
            return
        self.paint(text, pct)
        # 帧与帧之间留一点点停顿，「在动」这件事要能被眼睛看见
        try:
            time.sleep(0.15)
        except Exception:
            pass

    def _next_pct(self, text):
        for t, p in PHASES:
            if text == t:
                return p
        for t, p in PHASES:
            if text and t in text:
                return p
        return min(96, max(self.pct + 20, 10))

    def note(self, text):
        """插一行补充说明，不推进进度。"""
        if not self.enabled:
            self._write('  · %s\n' % text)
            return
        self._finish_frame()
        self._write('  · %s\n' % text)

    def _finish_frame(self):
        """把当前帧留在屏幕上（光标停在帧末尾），供 note/下一步接着写。"""
        if not self.enabled:
            return
        for _ in range(6):
            self._write('\r')
        self._last_len = 0

    def done(self):
        if not self.enabled:
            self._write('  [ok] 炉火已旺，等待投料\n')
            return
        self._finish_frame()
        # 开盖冒烟
        self._write('      ~ ~ ~\n')
        self._write('  炉火已旺，等待投料\n')

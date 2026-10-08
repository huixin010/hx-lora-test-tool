# -*- coding: utf-8 -*-
"""
lab_diag.py — 报错收集与反馈文档生成

V0.4.3 新增。

为什么要有这个
    工具是本地跑的，用户的机器千差万别（不同的 Python、不同的 ComfyUI
    版本、中文路径、笔记本休眠、无独显……）。以前一出问题，用户只能说
    "报错了"，能给的只有一张黑窗口截图 —— 而黑窗口会滚走、会被关掉，
    关掉之后关键那几行就没了。作者拿到的信息几乎为零。

    这个模块干两件事：
      1. 把每次异常连同环境信息落盘到logs/ 目录，**关掉窗口也不会丢**
      2. 把这些记录整理成一份可以直接发给作者的文字（.txt）

设计上的几条硬规矩
  · **绝不能因为记录日志本身而出错。** 诊断代码一旦自己抛异常，
    就会把真正的原始错误盖掉 —— 那比不记录更糟。所以这里每个写盘
    的动作都包在 try 里，失败就静默跳过。
  · **不碰用户的数据。** 只读配置里那几项路径，LoRA 库、报告一律不看。
  · **自动脱敏。** 导出时把用户名、盘符用户目录这些能定位到人的信息
    替换掉。工具是发在GitHub 上的，不能因为收集日志就把别人的
    Windows 用户名、邮箱路径发出去。
  · **日志要能自证。** 每条记录带一个"这是第几次运行"的标记，
    这样用户报「第 3 次点开始测试就报错」时，能对上是哪一份。
"""
import os
import platform
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, 'logs')

# 单个日志文件的上限。太大既没意义（用户看得完的只有最后几十行），
# 又会慢慢吃掉用户的磁盘。超过就滚一个新文件，最旧的删掉。
MAX_LOG_BYTES = 512 * 1024
KEEP_LOGS = 5

# 运行序号。同一次启动里所有记录共用一个，
# 方便把「这次启动」和「上次启动」的记录分开看。
_RUN_ID = '%s-%s' % (time.strftime('%Y%m%d'), os.getpid())


def log_path():
    return os.path.join(LOG_DIR, '错误日志-%s.txt' % _RUN_ID)


def _rotate(path):
    """超限就滚存。返回 True 表示发生了滚存。"""
    try:
        if os.path.isfile(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            for i in range(KEEP_LOGS - 1, 0, -1):
                old = path.replace('.txt', '.%d.txt' % i)
                prev = path.replace('.txt', '.%d.txt' % (i - 1))
                if os.path.isfile(prev):
                    try:
                        if i == KEEP_LOGS - 1:
                            os.remove(prev)
                        else:
                            os.replace(prev, old)
                    except OSError:
                        pass
            os.replace(path, path.replace('.txt', '.1.txt'))
            return True
    except Exception:
        pass
    return False


def _env_block():
    """环境信息。这一段是排查的起点，缺了它很多问题没法判断。"""
    lines = []
    try:
        import importlib
        pillow = '未安装'
        try:
            m = importlib.import_module('PIL')
            pillow = getattr(m, '__version__', '已安装（版本未知）')
        except Exception:
            pass
        lines.append('Python     : %s' % sys.version.split()[0])
        lines.append('Python路径 : %s' % os.path.dirname(sys.executable or '?'))
        lines.append('Pillow     : %s' % pillow)
        lines.append('系统       : %s %s' % (platform.system(),
                                             platform.release()))
        lines.append('机器架构   : %s' % (platform.machine() or '?'))
    except Exception:
        pass
    return lines


def _scrub(text):
    """把能定位到人的信息替换掉。

    这一步是**必须有**的：日志会被用户原样发到网上。不脱敏就等于
    帮别人泄露自己的 Windows 用户名和目录结构。
    """
    if not text:
        return text
    try:
        home = os.path.expanduser('~')
        if home and len(home) > 2:
            text = text.replace(home, '<用户目录>')
        # 形如 C:\Users\xxx 或 D:\Users\xxx
        text = re.sub(r'[A-Za-z]:\\Users\\[^\\\s"\']+',
                      '<用户目录>', text)
        # 邮箱
        text = re.sub(r'[\w.+-]+@[\w-]+\.[\w.-]+', '<邮箱已隐去>', text)
    except Exception:
        pass
    return text


def record(where, exc=None, note=''):
    """记一条错误。

    参数
        where : 出错的位置，用人能看懂的话写，例如 '打标签' / '/api/lib/list'
        exc   : 异常对象。不传就用当前正在处理的异常
        note  : 补充说明

    永远不抛异常。这条规矩比什么都重要 —— 记录失败不能影响主流程。
    """
    try:
        path = log_path()
        os.makedirs(LOG_DIR, exist_ok=True)
        _rotate(path)

        if exc is None:
            exc = sys.exc_info()[1]
        # traceback.format_exc 在没有活动异常时会返回 "NoneType: None"，
        # 这种情况（手工调用来记一条日志）就退回只记异常本身。
        if exc is not None:
            tb = ''.join(traceback.format_exception(type(exc), exc,
                                                    exc.__traceback__))
        else:
            tb = ''
        block = [
            '=' * 60,
            '时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'),
            '位置：%s' % where,
        ]
        if note:
            block.append('说明：%s' % note)
        block.append('-' * 60)
        block.append(tb.rstrip() or '(没有异常对象，可能是主动记录的)')
        block.append('')

        with open(path, 'a', encoding='utf-8') as f:
            f.write('\n'.join(block) + '\n')
    except Exception:
        # 到这里就已经是在救火了，绝不能再往上抛。
        pass


def record_startup():
    """启动时记一笔，让日志里能看出"这台机器上的工具跑起来过几次"。"""
    try:
        record('启动', note='工具启动（第 %s 次运行）' % _RUN_ID)
    except Exception:
        pass


def install_excepthook():
    """把未捕获异常的默认行为接到日志上。

    没有这个的话，server 崩了只在黑窗口留一段，用户一关窗口就彻底没了。
    接上之后，崩溃原因会留在 logs/ 里，用户点一下"复制反馈信息"
    就能把它捞出来。
    """
    try:
        old = sys.excepthook

        def hook(etype, value, tb):
            try:
                record('未捕获异常', exc=Exception(
                    ''.join(traceback.format_exception(etype, value, tb))))
            except Exception:
                pass
            try:
                old(etype, value, tb)
            except Exception:
                pass

        sys.excepthook = hook
    except Exception:
        pass


def read_logs(limit=4000):
    """读回所有日志文件，最新的在前。返回 (文本, 文件数)。"""
    files = []
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        for n in os.listdir(LOG_DIR):
            if n.startswith('错误日志-') and n.endswith('.txt'):
                files.append(os.path.join(LOG_DIR, n))
    except Exception:
        return '', 0
    # 最新那个排最前：它不带.1 后缀
    files.sort(key=lambda p: ('.1.txt' in os.path.basename(p),
                              os.path.basename(p)))
    files.reverse()
    out = []
    for p in files:
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                t = f.read()
            if len(t) > limit:
                t = '（前略，只保留最后 %d 字）\n' % limit + t[-limit:]
            out.append('===== %s =====\n%s' % (os.path.basename(p), t))
        except Exception:
            continue
    return '\n'.join(out), len(files)


def build_report(comfy_state_fn=None, app_title='', app_version=''):
    """生成给作者看的反馈文档（纯文本）。

    结构按「从外到内」排：先说这台机器是什么，再说什么时候出的错，
    最后才是错本身 —— 拿到这份文档的人不用回头问任何问题就能开始查。
    """
    L = []
    L.append('=' * 68)
    L.append('HX-验丹炉  报错反馈信息')
    L.append('=' * 68)
    L.append('')
    L.append('这份文件是自动生成的，整个复制发给作者就行，不用删改。')
    L.append('里面已经去掉你的用户名等隐私信息，但路径和模型名会保留 ——')
    L.append('它们是定位问题必需的。')
    L.append('')

    L.append('【1】这是哪个版本')
    L.append('-' * 68)
    L.append(app_title or 'HX-验丹炉')
    L.append('生成时间：%s' % time.strftime('%Y-%m-%d %H:%M:%S'))
    L.append('工具目录：%s' % HERE)
    L.append('')

    L.append('【2】运行环境')
    L.append('-' * 68)
    for line in _env_block():
        L.append(line)
    L.append('控制台编码：%s' % (getattr(sys.stdout, 'encoding', '未知')
                                 or '未知'))
    L.append('')

    L.append('【3】ComfyUI 连接情况')
    L.append('-' * 68)
    if comfy_state_fn:
        try:
            st = comfy_state_fn() or {}
            L.append('是否在线   : %s' % ('是' if st.get('alive') else '否'))
            L.append('版本       : %s' % (st.get('version') or '拿不到'))
            L.append('地址       : %s' % (st.get('url') or '?'))
            if st.get('error'):
                L.append('错误       : %s' % st.get('error'))
            if st.get('message'):
                L.append('说明       : %s' % st.get('message'))
        except Exception as e:
            L.append('（读取 ComfyUI 状态时自己也出错了：%s）' % e)
    else:
        L.append('（未提供状态）')
    L.append('')

    L.append('【4】出错记录')
    L.append('-' * 68)
    text, n = read_logs()
    if not text:
        L.append('没有记录到任何错误。')
        L.append('')
        L.append('如果你是"点了没反应"、"界面卡住"这类问题，日志里可能是空的 ——')
        L.append('那多半不是报错，而是某一步没走到。请在描述里写清：')
        L.append('  · 点了哪个按钮')
        L.append('  · 期望发生什么')
        L.append('  · 实际发生什么')
    else:
        L.append('共%d 份日志文件，以下是全部内容：' % n)
        L.append('')
        L.append(text.rstrip())
    L.append('')

    L.append('【5】顺手提一句（可选）')
    L.append('-' * 68)
    L.append('你当时在做什么、点了什么、原本期望出什么样的图，写在这里：')
    L.append('')
    L.append('')
    L.append('=' * 68)
    L.append('（报告结束）')
    return _scrub('\n'.join(L))


def report_filename(app_version=''):
    v = app_version.replace('.', '-') if app_version else ''
    return '验丹炉报错反馈-%s.txt' % (v or time.strftime('%Y%m%d'))
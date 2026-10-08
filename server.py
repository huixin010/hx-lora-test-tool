# -*- coding: utf-8 -*-
"""
HX验丹炉-V0.5.1 — 本地 LoRA 扫档测试工具（后端服务）
Copyright (c) 2026 HX-会心一姬 · MIT License

浏览器里选 LoRA、勾强度档、选测试方案（人像/风格/物品/功能…），点一下跑完
整个矩阵：自动出对比大图、客观评分和最佳档位推荐。

V0.5.1：三个板块拆成互相独立的子界面（各占一个地址，谁也不带谁的功能）
  · 验丹炉 /run  —— 9 个单元，新增「LoRA 组合」（2 个以上 LoRA 同时上场，
    各自带权重）；右下角条件满足才亮起的「验丹」按钮
  · 鉴定台 /hist —— 鉴定报告 / 历史记录 / 投放丹房
  · 丹房   /lib  —— 查漏补缺 / 品质丹房 / 普通丹房（按品质标签虚拟归档，
    不动磁盘上的文件）
  另外新增 /api/lib/index：整库品质索引，只读标签表不读 safetensors 头，
  丹房那三块靠它秒开。

V0.4.3 修复：
  · 解压即用 —— start.bat 改用 find_comfy.ps1 借 PowerShell 定位 ComfyUI，
    不再出现「只有 ComfyUI 自带 Python 的机器上起不来」的死锁
  · 右键定级后文件名不变色 —— LIB.tags 曾用 null 表示"没打过标"，
    定级时对它取下标赋值直接抛错，界面毫无反应

V0.4.3 新增：
  · 报错收集 —— 所有异常落盘到 logs/，界面上可一键导出反馈文档
"""
import copy
import hashlib
import io
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlparse, unquote, urlencode, parse_qs, quote

# Pillow is imported softly on purpose. If it is missing the tool must still
# boot: the user downloads a fresh copy, has no config.json yet, and the whole
# point is that the web UI is where paths get fixed - a hard ImportError here
# would just show a dead console window instead. Everything that truly needs
# image decoding calls require_pillow() first and fails with a readable hint.
try:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageStat
    PILLOW_ERR = ''
except Exception as _pillow_exc:          # pragma: no cover
    Image = ImageDraw = ImageFilter = ImageFont = ImageStat = None
    PILLOW_ERR = str(_pillow_exc)


def have_pillow():
    return Image is not None


def require_pillow():
    """出图/评分前调用。缺 Pillow 时给一条能照着做的中文提示，而不是 traceback。"""
    if Image is None:
        raise RuntimeError(
            '这个 Python 没装 Pillow，图片评分和报告大图做不了。\n'
            '请换成 ComfyUI 自带的 python.exe：在「模型设置」里填好 ComfyUI 根目录'
            '（comfy_root），或在工具目录里建一个 python_path.txt，'
            '里面单独一行写 python.exe 的完整路径，然后重启工具。')


sys.stdout.reconfigure(encoding='utf-8')

HERE = os.path.dirname(os.path.abspath(__file__))
# 版本号只在这里定义一次。以前散在 5 个地方（启动横幅、报告标题、报告页…），
# 升版本漏改的地方会以「报告里写着 V0.2、程序自报 V0.3」的形式暴露出来。
# V0.4 起产品名改为「HX-验丹炉」。V0.5 起去掉中间那个连字符，全名
# 「HX验丹炉」，并把网页版收进桌面版（同一个后端，两种外壳）。
APP_VERSION = 'V0.5.4'
APP_TITLE = 'HX验丹炉-' + APP_VERSION
APP_NAME = 'HX验丹炉'
CONFIG_PATH = os.path.join(HERE, 'config.json')
EXAMPLE_PATH = os.path.join(HERE, 'config.example.json')
PRESETS_PATH = os.path.join(HERE, 'presets.json')
RUNS_DIR = os.path.join(HERE, 'runs')
# V0.5：一次测试的产物（图 / 对比大图 / 报告 / run.json）**只存一份**，
# 统一落在 ComfyUI 的 output 目录下这个子目录里，不再在工具自己的 runs/ 里
# 另存一份。用户的原话是「测试生成的图片存在两个文件路径」，说的就是
# 以前 ComfyUI 写一份 output/krea2lab/、工具又抄一份 runs/<id>/images/。
RUNS_SUBDIR = 'HX验丹炉'
STATIC_DIR = os.path.join(HERE, 'static')
# V0.5：内置测试工作流（用户在 ComfyUI 里没导出过 API 工作流时的兜底）
WORKFLOWS_DIR = os.path.join(HERE, 'workflows')
CLIENT_ID = 'hx-lora-test-tool'
# V0.3：用户手动确认过的类别存在这里，key 是 loras 目录里的相对路径
TAGS_PATH = os.path.join(HERE, 'tag_overrides.json')
# V0.4：手动指定的品质等级也存在同一文件里，字段名 grade

# 分两个文件是为了让 server.py 不再无限膨胀：方案库是纯数据（可自己改），
# 标签分类器是纯逻辑（带自测脚本 test_lora_tags.py）。
#
# HERE 必须**绝对路径**地进 sys.path：本工具零配置重启时（见
# maybe_relaunch_with_comfy_python）会换成 ComfyUI 自带的 python.exe 再跑一遍
# 脚本，那个解释器的 sys.path[0] 不保证是本目录 —— 只靠 cwd 的话，
# 换完 Python 就 ModuleNotFoundError 起不来，而且日志里很难看出原因。
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import profiles_data
import lora_tags
import lora_grade
import boot_anim
# V0.4.3：报错收集。放在最后导入，因为它依赖上面已经就位的一切
# （它自己不需要，但顺序上晚一点更清楚）。
# 万一这个模块导入失败（比如文件没跟着包发出去）不能让整个工具起不来 ——
# 它是锦上添花，不是必需品。
try:
    import lab_diag
except Exception:                      # pragma: no cover
    lab_diag = None

urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({})))


def bug(where, note=''):
    """记一条报错到 logs/，并照旧把 traceback 打到控制台。

    为什么不让大家直接用 traceback.print_exc()：
    它只往黑窗口写一行。用户一关窗口就什么都没了，而报障时最需要的
    恰恰是那几行 —— 用户能提供的只有"报错了"三个字。

    这里**先记盘再打屏**，两条路都留着：
      · 用户还在眼前 → 黑窗口照旧能看到（老习惯不变）
      · 用户已经关窗 → logs/ 里还在，界面上能一键导出

    记录本身失败不影响主流程（lab_diag.record 内部全包了try），
    所以这里可以放心在 except 块里调。
    """
    if lab_diag:
        try:
            lab_diag.record(where, note=note)
        except Exception:
            pass
    traceback.print_exc()


# ============================================================ 配置 / 预设
def load_json(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# config.json 缺失或只填了一部分时，用这些兜底，保证工具总能启动
CONFIG_DEFAULTS = {
    'comfy_url': 'http://127.0.0.1:8188',
    'comfy_root': '',
    'comfy_output': '',
    'unet': '',
    'clip': '',
    'clip_type': 'krea2',
    'vae': '',
    'lora_node': 'LoraLoaderModelOnly',
    'comfy_launch': '',            # 留空 = 自动从 comfy_root 里找启动脚本
    'comfy_autostart': True,       # 点了开始测试但 ComfyUI 没开 → 自己拉起来
    'comfy_autostop': False,       # 跑完是否关掉「由本工具启动的」ComfyUI
    'lora_dir': '',                # loras 物理目录；留空 = 从 comfy_root 自动找
    'watch_dirs': [],              # 收件箱监视目录；留空 = 自动用 Downloads
    'port': 8760,
    'defaults': {
        'strengths': [0, 0.4, 0.6, 0.8, 1.0, 1.2],
        'seed': 20261002,
        'width': 768,
        'height': 768,
        'steps': 8,
        'cfg': 1.0,
        'sampler': 'euler',
        'scheduler': 'simple',
    },
}


def load_config():
    cfg = dict(CONFIG_DEFAULTS)
    on_disk = load_json(CONFIG_PATH, None)
    if on_disk is None:
        # 没有 config.json = 刚从 GitHub 下载解开的全新副本。
        # 先套用模板（里面有 Krea 2 的模型名和默认参数），这样用户不必
        # 自己填 unet/clip/vae，只需要工具自己把 ComfyUI 根目录找出来。
        on_disk = load_json(EXAMPLE_PATH, {})
    if isinstance(on_disk, dict):
        cfg.update({k: v for k, v in on_disk.items() if not k.startswith('_')})
    d = dict(CONFIG_DEFAULTS['defaults'])
    d.update(cfg.get('defaults') or {})
    cfg['defaults'] = d
    return cfg


# ==================================================== 一次测试的产物放哪里
# V0.5 的核心改动之一。
#
# 改之前：ComfyUI 按 filename_prefix 把原图写到 output/krea2lab/<时间戳>/，
# 工具再用 /view 或本地读取抄一份到 runs/<run_id>/images/。同一张图存在两处，
# 删报告只删得掉工具那一份，ComfyUI 那边会越积越多。
#
# 改之后：只有一处 —— ComfyUI 的 output/HX验丹炉/<run_id>/。
#   · 出图时 filename_prefix 直接指向这个目录，fetch_image 只做「同目录改名」，
#     不再复制，所以每个格子恰好一个文件；
#   · 对比大图 / report.html / run.json 也写在同一个目录里，
#     报告里的相对路径 images/xxx.png 因此仍然成立（双击文件也能看）；
#   · 「鉴定台」删一条记录 = 删这一个目录，图跟着一起没了。
#
# comfy_output 没配或不可写时退回工具自己的 runs/，这样离线重算、
# 以及 ComfyUI 装在别的机器上（只走 HTTP /view）的用法都不受影响。
def _writable_dir(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, '.hx-write-test')
        with open(probe, 'w', encoding='utf-8') as f:
            f.write('x')
        os.remove(probe)
        return True
    except OSError:
        return False


def runs_root(cfg=None):
    """本次运行的产物根目录（新位置优先）。"""
    cfg = cfg or load_config()
    out = (cfg.get('comfy_output') or '').strip()
    if out and os.path.isdir(out):
        d = os.path.join(out, RUNS_SUBDIR)
        if _writable_dir(d):
            return d
    if _writable_dir(RUNS_DIR):
        return RUNS_DIR
    return RUNS_DIR


def runs_roots(cfg=None):
    """所有可能出现历史产物的根目录：新位置在前，老的 runs/ 兜底。

    必须同时认两处 —— 用户升级到 V0.5 以后，V0.4 之前跑出来的记录
    还躺在 runs/ 里，历史列表不能因此变空。
    """
    out = []
    for r in (runs_root(cfg), RUNS_DIR):
        if r and os.path.isdir(r):
            rn = os.path.normpath(r)
            if rn not in out:
                out.append(rn)
    return out


def resolve_run_dir(run_id):
    """把 run_id 解析成产物目录的绝对路径；找不到返回 None。

    严格按「直接子目录」判定，防 `..` 和带分隔符的写法穿出根目录。
    """
    if not run_id:
        return None
    name = str(run_id)
    if os.path.basename(name) != name or name in ('.', '..'):
        return None
    for root in runs_roots():
        full = os.path.normpath(os.path.join(root, name))
        if os.path.dirname(full) == os.path.normpath(root) and os.path.isdir(full):
            return full
    return None


# ================================================ 首次运行：自己找出 ComfyUI
# 仓库里故意不带 config.json（那里面有作者本机的真实路径），所以新解开的副本
# 必须能自己定位 ComfyUI —— 否则用户双击 bat 看到的就是一句「找不到 Python」，
# 而不是一个能用的工具。判定标准跟 lora_root() 保持一致：要么自己就是
# ComfyUI 源码目录（main.py + models），要么是便携版根目录（下面有 ComfyUI/）。
_SKIP_SCAN = {
    'windows', 'program files', 'program files (x86)', 'programdata',
    'users', '$recycle.bin', 'system volume information', 'recovery',
    'perflogs', 'documents and settings', 'msocache', 'appdata',
    'node_modules', 'winsxs', 'installer', 'driver', 'drivers',
}

_COMFY_COMMON_NAMES = (
    'ComfyUI', 'comfyui', 'ComfyUI_windows_portable',
    'ComfyUI_windows_portable_nvidia', 'ComfyUI-aki', 'ComfyUI-aki-v1.3',
    'ComfyUI-aki-v2', 'ComfyUI-aki-v3', 'AI\\ComfyUI', 'AI\\comfyui',
    'AI\\ComfyUI_windows_portable', 'ComfyUI\\ComfyUI',
)

# V0.4.2：便携版目录名带版本号后缀是常态（ComfyUI_windows_portable-G312-0122、
# ComfyUI-aki-v2.5 这种），写死名字必然漏。补一条「名字里含 comfy」的兜底规则，
# 命中后仍然要用_looks_like_comfy() 确认，不会把别的目录误认成 ComfyUI。
_COMFY_NAME_HINT = 'comfy'


def _looks_like_comfy(path):
    """是不是 ComfyUI 的（便携版或源码版）根目录。"""
    try:
        if (os.path.isfile(os.path.join(path, 'main.py'))
                and os.path.isdir(os.path.join(path, 'models'))):
            return True          # 源码版：ComfyUI/main.py + models/
        inner = os.path.join(path, 'ComfyUI')
        if os.path.isdir(inner):
            if os.path.isfile(os.path.join(inner, 'main.py')):
                return True      # 便携版：<root>/ComfyUI/main.py
            if os.path.isdir(os.path.join(inner, 'models')):
                return True
        if (os.path.isfile(os.path.join(path, 'python_embeded', 'python.exe'))
                and os.path.isdir(inner)):
            return True          # 便携版：带自带的 python
    except OSError:
        return False
    return False


def _scan_dirs(path):
    try:
        names = os.listdir(path)
    except OSError:
        return []
    out = []
    for n in names:
        if n.startswith('.') or n.lower() in _SKIP_SCAN:
            continue
        p = os.path.join(path, n)
        try:
            if os.path.isdir(p) and not os.path.islink(p):
                out.append(p)
        except OSError:
            continue
    return out


def _drive_roots():
    out = []
    for ch in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        p = ch + ':\\'
        if os.path.isdir(p):
            out.append(p)
    return out


def _profile_roots():
    """不少人把 ComfyUI 放在桌面 / 文档 / 下载里，这些也当扫描起点。"""
    home = os.path.expanduser('~')
    out = []
    for sub in ('', 'Desktop', 'Documents', 'Downloads', 'OneDrive',
                'OneDrive\\Desktop', 'OneDrive\\Documents'):
        p = os.path.join(home, sub) if sub else home
        if os.path.isdir(p):
            out.append(p)
    return out


def _root_from_cmdline(cmd):
    """从一条 python 进程命令行里反推 ComfyUI 根目录。

    运行中的 ComfyUI 一定是 `python.exe ...\\ComfyUI\\main.py ...` 这样，
    main.py 所在的那个目录就是 ComfyUI 本体；它的上一级如果带
    python_embeded，那才是便携版根目录（自动启动要用的就是这一级）。
    认不出来就返回 ''，绝不猜。

    实现用「按空格切成 token，找以 main.py 结尾的那个」而不是一条大正则：
    大正则（`([A-Za-z]:[/\\\\].*?)[/\\\\]main\\.py`）会从**第一个**盘符开始
    一路贪到main.py，把整条命令行都吞进分组里 —— 实测抽出来的是
    「…\\python.exe -s G:\\…\\ComfyUI」，还得再切一次，徒增出错面。
    另外字符类里表示「反斜杠或斜杠」只能写一个反斜杠（[/\\]）；
    写成两个（[\\/]）在正则里是「反斜杠」再跟一个「/」，真实路径一个都匹配不上。
    """
    for tok in (cmd or '').split():
        tok = tok.strip('"\'')
        if not tok.lower().endswith('main.py'):
            continue
        d = os.path.normpath(os.path.dirname(tok))
        if not os.path.isdir(d):
            continue
        if not _looks_like_comfy(d):
            # 命中的可能是别的项目的 main.py，往上退一级再试
            parent = os.path.dirname(d)
            if not _looks_like_comfy(parent):
                continue
            d = parent
        # 便携版：本体在 <root>\ComfyUI，返回 root
        parent = os.path.dirname(d)
        if os.path.isdir(os.path.join(parent, 'ComfyUI')) and \
                _looks_like_comfy(parent):
            return parent
        return d
    return ''


def detect_comfy_from_process():
    """正在运行的 ComfyUI 进程 —— 这是最可靠的一条线索。

    排在所有"猜路径"的方法之前：用户在跑 ComfyUI，说明路径一定是可用的，
    而且命令行里是白纸黑字写出来的，不用赌目录名、不用赌扫描深度。
    本机实测（ComfyUI 装在 G:\\HeiHe\\... 三层深的非标准名目录）就是靠这条
    在 0.5 秒内定位到的。
    """
    if os.name != 'nt':
        return ''
    ps = ('Get-CimInstance Win32_Process -Filter "Name=\'python.exe\' '
          'or Name=\'pythonw.exe\'" -ErrorAction SilentlyContinue | '
          'ForEach-Object { $_.CommandLine }')
    try:
        r = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', ps],
            capture_output=True, timeout=20)
    except Exception:
        return ''
    if r.returncode != 0:
        return ''
    for line in (r.stdout or b'').decode('utf-8', 'ignore').splitlines():
        line = line.strip()
        if not line or 'main.py' not in line.lower():
            continue
        root = _root_from_cmdline(line)
        if root:
            return root
    return ''


def detect_comfy_root(max_seconds=25):
    """comfy_root 没填时，自己找一个像 ComfyUI 的目录。

    返回 (路径, 是怎么找到的)。找不到返回 ('', '')。
    扫描有超时上限，免得在大硬盘上把启动卡住。
    """
    deadline = time.time() + max_seconds

    # 0) 先问正在跑的 ComfyUI（最准，见 detect_comfy_from_process 的说明）
    root = detect_comfy_from_process()
    if root:
        return root, '正在运行的 ComfyUI'

    seeds = _drive_roots() + _profile_roots()

    # 1) 最常见的位置先试一遍 —— 命中率最高，也几乎不耗时
    for s in seeds:
        for sub in _COMFY_COMMON_NAMES:
            cand = os.path.join(s, sub)
            if _looks_like_comfy(cand):
                return cand, '常见位置'
        if time.time() > deadline:
            return '', ''

    # 2) 从当前 Python 往上找：便携版的 python_embeded 就在 ComfyUI 里
    exe = os.path.dirname(sys.executable or '')
    for _ in range(4):
        if exe and _looks_like_comfy(exe):
            return exe, '从当前 Python 位置推断'
        parent = os.path.dirname(exe)
        if not parent or parent == exe:
            break
        exe = parent

    # 3) 广度优先扫，带超时
    # 深度放到 4：便携版常被塞在「下载\AI 绘图\xxx」这种三层嵌套里，
    # 只扫三层会漏掉。（本机实测路径就是三层：G:\HeiHe\comfyuizhb20260122\...）
    seen = set()
    queue = [(s, 0) for s in seeds]
    while queue:
        if time.time() > deadline:
            break
        path, depth = queue.pop(0)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        if _looks_like_comfy(path):
            return path, '自动扫描到'
        if depth >= 4:
            continue
        for sub in _scan_dirs(path):
            queue.append((sub, depth + 1))
    return '', ''


def comfy_candidates(max_seconds=20):
    """列出本机所有可能是 ComfyUI 的目录（给界面上的「自动扫描」用）。

    返回 [{path, how, loras}]，按可信度排序。跟 detect_comfy_root 的区别：
    那个只返回**一个**答案（启动时用，够 了）；这个返回全部候选，
    让人能自己挑 —— 万一机器上有好几个 ComfyUI（真事），或者一个都没找到，
    至少还有个手动选择的入口，而不是只能干瞪眼。
    """
    found = []

    def add(p, how):
        # 候选列表是给人看的，所以**必须**真的存在且确实是 ComfyUI。
        # 少了这道校验，「常见位置」那十几个名字会在每一台机器上
        # 凭空造出上百条 C:\ComfyUI\ComfyUI 这种不存在的路径，
        # 真有 ComfyUI 的那条反而被淹在中间（实测 121 条里只有 1 条能用）。
        if not p or not _looks_like_comfy(p):
            return
        p = os.path.normpath(p)
        for f in found:
            if os.path.normcase(f['path']) == os.path.normcase(p):
                return
        # 只读地看一眼这个目录下有没有 loras 目录。
        # 刻意**不**调 lora_root(refresh=True)：那会把模块级的 _LORA_ROOT
        # 缓存覆盖成正在探测的这个候选，用户真正的 loras 目录就丢了。
        loras = ''
        for rel in (('ComfyUI', 'models', 'loras'), ('models', 'loras')):
            cand = os.path.join(p, *rel)
            if os.path.isdir(cand):
                loras = cand
                break
        found.append({'path': p, 'how': how, 'loras': loras})

    # 1) 正在跑的 ComfyUI：最可信
    add(detect_comfy_from_process(), '正在运行的 ComfyUI')

    # 2) 常见位置
    for s in _drive_roots() + _profile_roots():
        for sub in _COMFY_COMMON_NAMES:
            add(os.path.join(s, sub), '常见位置')

    # 3) 按名字含 comfy 兜底 —— 便携版目录常带版本号后缀
    deadline = time.time() + max_seconds
    seen = set()
    queue = [(s, 0) for s in _drive_roots() + _profile_roots()]
    while queue and time.time() < deadline:
        path, depth = queue.pop(0)
        key = os.path.normcase(path)
        if key in seen:
            continue
        seen.add(key)
        name = os.path.basename(path).lower()
        if _COMFY_NAME_HINT in name:
            add(path, '按目录名匹配')
        if depth >= 3:
            continue
        for sub in _scan_dirs(path):
            queue.append((sub, depth + 1))

    # 带 loras 的排前面：那是"能直接拿来跑测试"的完整安装，
    # 只有本体没 loras 的多半是个壳（比如只拉了代码没下模型）。
    found.sort(key=lambda f: (0 if f['loras'] else 1,))
    return found


def _cfg_write_back(key, value):
    """把单个键写回 config.json，其余内容一个不动。

    读的是**磁盘上的原文件**（找不到才退到模板），否则会把用户没在
    CONFIG_DEFAULTS 里出现的自定义键一并抹掉。
    """
    try:
        on_disk = load_json(CONFIG_PATH, None)
        if on_disk is None:
            on_disk = load_json(EXAMPLE_PATH, {})
        if not isinstance(on_disk, dict):
            on_disk = {}
        on_disk.pop('_说明', None)
        on_disk[key] = value
        save_json(CONFIG_PATH, on_disk)
        return True
    except OSError:
        return False


def comfy_output_candidates(cfg):
    """顺着 comfy_root 猜 ComfyUI 的 output 目录。

    两种真布局各一条：
      · 便携版 <root>/ComfyUI/output —— 先确认 <root>/ComfyUI 确实是个
        ComfyUI（main.py / models / 自带 python 任一条）；
      · 源码版 <root>/output         —— 确认 <root> 本身是 ComfyUI。

    为什么要确认这一下：产物是**要写进磁盘**的，一旦 comfy_root 填歪了，
    工具就会在别人一个不相干的文件夹里凭空造一个 output/ 出来 ——
    这比"没找到、退回工具自己的 runs/"难排查得多。
    （这里必须复用上面那个严格的 _looks_like_comfy，别另写一套松判定：
      本机实测有过教训，一个只带 models 目录的视频工具目录
      G:\\HeiHe\\TopazVideo 就被误认成过 ComfyUI。）
    """
    root = (cfg.get('comfy_root') or '').strip()
    out = []
    if not root:
        return out
    inner = os.path.join(root, 'ComfyUI')
    if _looks_like_comfy(inner):
        out.append(os.path.join(inner, 'output'))
    if _looks_like_comfy(root):
        out.append(os.path.join(root, 'output'))
    return out


def ensure_output_dir(cfg=None):
    """comfy_output 没配就自己接上，接上后写回 config.json。

    为什么要有这一步（V0.5 · 更新要求第 7 条）：
    测试产物要统一落在 ComfyUI 的 output 下新开的子目录里，这样
    ComfyUI 自己也能看到这些图；而发布出去的 config.json 里
    comfy_output 是**空串**（不能把作者机器的路径发出去），
    光靠 runs_root() 会一路退回工具自己的 runs/，要求就落不了地。
    所以启动时顺着已经认出来的 comfy_root 把它补上。

    只在**空串**时补：用户明确填过的值哪怕是错的也留着不动，
    免得哪天手滑改错、程序又偷偷改回去，人会以为是灵异事件。
    """
    cfg = load_config() if cfg is None else cfg
    if (cfg.get('comfy_output') or '').strip():
        return False
    for d in comfy_output_candidates(cfg):
        if _writable_dir(d):
            cfg['comfy_output'] = d
            _cfg_write_back('comfy_output', d)
            return True
    return False


def ensure_config():
    """启动时跑一次：comfy_root / comfy_output 还是空的就自己找，找到写回。

    返回 (cfg, 提示语)。写盘是为了下次直接读，不必每次启动都扫一遍。
    """
    cfg = load_config()
    notes = []
    if not (cfg.get('comfy_root') or '').strip():
        root, how = detect_comfy_root()
        if root:
            cfg['comfy_root'] = root
            _cfg_write_back('comfy_root', root)
            notes.append('自动找到 ComfyUI（%s）' % how)
    # R7：产物统一落在 ComfyUI 的 output 下。这一步不能因为
    # comfy_root 早就配好了就跳过 —— 升级上来的用户 comfy_output 多半是空的。
    if ensure_output_dir(cfg):
        notes.append('测试图片输出目录：%s' % cfg['comfy_output'])
    return cfg, '；'.join(notes)


def maybe_relaunch_with_comfy_python(cfg):
    """当前这个 Python 没装 Pillow 时，用 ComfyUI 自带的那个重启自己。

    场景：用户从 GitHub 下载解压后双击，bat 只找到了系统里的裸 Python。
    工具已经知道 ComfyUI 在哪，那就直接换它的 python 重开一次 ——
    对用户来说是「双击就能用」，而不是「双击后要自己去装 Pillow」。
    用环境变量兜底，保证最多只重开一次，不会来回套娃。
    """
    if have_pillow() or os.environ.get('HX_RELAUNCHED'):
        return
    root = (cfg.get('comfy_root') or '').strip()
    if not root:
        return
    cand = os.path.join(root, 'python_embeded', 'python.exe')
    if not os.path.isfile(cand):
        return
    if os.path.normcase(os.path.abspath(cand)) == \
            os.path.normcase(os.path.abspath(sys.executable or '')):
        return
    try:
        r = subprocess.run([cand, '-c', 'import PIL'], timeout=90,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if r.returncode != 0:
            return
    except Exception:
        return
    print('  当前 Python 没有 Pillow，改用 ComfyUI 自带的 Python 重新启动：')
    print('    %s' % cand)
    sys.stdout.flush()
    env = dict(os.environ)
    env['HX_RELAUNCHED'] = '1'
    code = subprocess.call(
        [cand, os.path.abspath(__file__)] + list(sys.argv[1:]), env=env)
    sys.exit(code)


# ================================================================ HTTP 基础
def cget(url, timeout=30):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def cpost(url, payload, timeout=180):
    data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(
        url, data=data, headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def comfy_alive(cfg):
    try:
        s = cget(cfg.get('comfy_url', '') + '/system_stats', timeout=5)
        return True, s['system']['comfyui_version']
    except Exception as e:
        return False, str(e)


# ============================================== 自动拉起 / 关闭 ComfyUI
# 本工具自己不产出图片，出图能力完全来自 ComfyUI。所以「关着 ComfyUI 也能用」
# 只有两条路：要么把它拉起来，要么只做离线的事（重算/重出报告/导入图片）。
COMFY = {'pid': None, 'busy': False, 'message': '', 'started': 0.0, 'by_us': False}
COMFY_LOCK = threading.Lock()
# 把「我启动的那个 ComfyUI」的 pid 落盘：工具重启后仍然关得掉它。
# （重启后内存里的 by_us 会丢，只靠内存的话按钮就失效了。）
COMFY_PID_FILE = os.path.join(HERE, 'comfy_pid.txt')

# 便携版常见的几个启动脚本，按优先级
LAUNCH_CANDIDATES = (
    'run_nvidia_gpu.bat',
    'run_nvidia_gpu_fast_fp16_accumulation.bat',
    'run_cpu.bat',
)


def _remember_pid(pid):
    try:
        with open(COMFY_PID_FILE, 'w', encoding='utf-8') as f:
            f.write(str(pid))
    except OSError:
        pass


def _forget_pid():
    """清掉记录。写成 0 而不是删文件 —— 少一次删除操作，也不怕文件被占用。"""
    try:
        with open(COMFY_PID_FILE, 'w', encoding='utf-8') as f:
            f.write('0')
    except OSError:
        pass


def _recall_pid():
    try:
        with open(COMFY_PID_FILE, encoding='utf-8') as f:
            pid = int((f.read() or '0').strip() or 0)
        return pid or None
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.run(['tasklist', '/FI', 'PID eq %d' % pid, '/NH'],
                             capture_output=True, text=True, timeout=15,
                             encoding='utf-8', errors='ignore').stdout
        return str(pid) in (out or '')
    except Exception:
        return False


def comfy_launch_cmd(cfg):
    """找出启动 ComfyUI 的命令。返回 (命令, 工作目录)；找不到返回 (None, root)。"""
    explicit = (cfg.get('comfy_launch') or '').strip()
    if explicit and os.path.exists(explicit):
        return explicit, (os.path.dirname(explicit) or None)
    root = (cfg.get('comfy_root') or '').strip()
    if not root or not os.path.isdir(root):
        return None, root or None
    for name in LAUNCH_CANDIDATES:
        p = os.path.join(root, name)
        if os.path.exists(p):
            return p, root
    # 没有 .bat 就退回直接跑 main.py
    py = os.path.join(root, 'python_embeded', 'python.exe')
    mj = os.path.join(root, 'ComfyUI', 'main.py')
    if os.path.exists(py) and os.path.exists(mj):
        return '""%s" -s "%s" --windows-standalone-build"' % (py, mj), root
    return None, root


def _comfy_launch_worker(cmd, cwd, timeout):
    try:
        flags = 0
        if os.name == 'nt':
            # 独立控制台：用户能看到 ComfyUI 自己的加载日志，也便于手动关掉
            flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
        proc = subprocess.Popen(cmd, cwd=cwd or None, shell=True,
                                creationflags=flags, close_fds=True)
        with COMFY_LOCK:
            COMFY['pid'] = proc.pid
        _remember_pid(proc.pid)
        t0 = time.time()
        cfg = load_config()
        while time.time() - t0 < timeout:
            alive, _ = comfy_alive(cfg)
            if alive:
                with COMFY_LOCK:
                    COMFY.update({'busy': False, 'message': 'ComfyUI 已就绪'})
                return
            if proc.poll() is not None:
                _forget_pid()
                with COMFY_LOCK:
                    COMFY.update({'pid': None, 'by_us': False, 'busy': False,
                                  'message': '启动进程已退出，端口没起来 —— '
                                             '请手动开一次 ComfyUI 看看报什么错'})
                return
            with COMFY_LOCK:
                COMFY['message'] = '正在启动 ComfyUI…已等 %d 秒' % int(time.time() - t0)
            time.sleep(2)
        with COMFY_LOCK:
            COMFY.update({'busy': False,
                          'message': '等了 %d 秒还没就绪，先不自动开了' % timeout})
    except Exception as e:
        bug('自动启动 ComfyUI')
        _forget_pid()
        with COMFY_LOCK:
            COMFY.update({'busy': False, 'message': '启动失败：%s' % e})


def comfy_state():
    cfg = load_config()
    alive, info = comfy_alive(cfg)
    with COMFY_LOCK:
        st = dict(COMFY)
    file_pid = _recall_pid()
    if not st['by_us'] and file_pid:           # 工具重启过：靠 pid 文件恢复记忆
        st['by_us'] = True
    if not alive and not st['busy']:
        pid_check = st['pid'] or file_pid
        if pid_check and not _pid_alive(pid_check):
            # 进程没了就清掉记录：既避免误关到别的进程，界面也不会一直说「是我启动的」
            _forget_pid()
            with COMFY_LOCK:
                COMFY.update({'pid': None, 'by_us': False})
            st['pid'], st['by_us'] = None, False
    cmd, _ = comfy_launch_cmd(cfg)
    st.update({
        'alive': alive,
        'version': info if alive else '',
        'error': '' if alive else info,
        'busy': st['busy'] and not alive,
        'elapsed': round(time.time() - st['started'], 1) if st['busy'] else 0,
        'launch_cmd': cmd or '',
        'comfy_root': cfg.get('comfy_root', ''),
        'can_launch': bool(cmd),
        'we_started': st['by_us'],
    })
    return st


def comfy_start(cfg, timeout=600):
    """异步拉起 ComfyUI。返回 (ok, message)。

    实测冷启动到端口就绪约 200 秒（便携版要加载 torch + 模型索引），
    所以默认给到 10 分钟，别设太短。
    """
    alive, _ = comfy_alive(cfg)
    if alive:
        return True, 'ComfyUI 已经在运行'
    with COMFY_LOCK:
        if COMFY['busy']:
            return True, '已经在启动中，请稍候'
    cmd, cwd = comfy_launch_cmd(cfg)
    if not cmd:
        return False, ('没找到 ComfyUI 的启动脚本。\n'
                       '最简单：手动把 ComfyUI 打开，再回来点一次。\n'
                       '想让它自动启动：在「模型设置」里把「ComfyUI 根目录」填成 '
                       '便携版根目录（有 run_nvidia_gpu.bat 的那一层），'
                       '或直接在 config.json 里改 comfy_root。')
    with COMFY_LOCK:
        COMFY.update({'busy': True, 'message': '正在启动 ComfyUI…',
                      'started': time.time(), 'by_us': True})
    threading.Thread(target=_comfy_launch_worker,
                     args=(cmd, cwd, timeout), daemon=True).start()
    return True, '已开始启动 ComfyUI'


def comfy_stop():
    """只关闭「由本工具启动的」ComfyUI，避免误关用户自己开的那一个。"""
    with COMFY_LOCK:
        pid, by_us = COMFY['pid'], COMFY['by_us']
    if not by_us:
        pid = _recall_pid()
        by_us = bool(pid)
    if not pid or not by_us:
        return {'error': '没有由本工具启动的 ComfyUI。为避免误关你自己开的那一个，'
                         '这里只关我启动的进程，请手动关。'}
    # 先确认对面真的在跑再动手 —— 万一 pid 被系统回收给了别的进程，
    # 也不会误杀（端口没人听就说明本来就没在跑）。
    alive, _ = comfy_alive(load_config())
    if not alive:
        _forget_pid()
        with COMFY_LOCK:
            COMFY.update({'pid': None, 'by_us': False, 'busy': False})
        return {'ok': True, 'message': '它本来就已经不在了，已清掉记录'}
    try:
        subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                       capture_output=True, timeout=30)
    except Exception as e:
        return {'error': '关闭失败：%s' % e}
    _forget_pid()
    with COMFY_LOCK:
        COMFY.update({'pid': None, 'by_us': False, 'busy': False, 'message': '已关闭'})
    return {'ok': True, 'message': '已关闭由本工具启动的 ComfyUI'}


# ================================================================== 启动自检
def _node_options(cfg, node, key):
    """读某个节点某个下拉框的可选值。读不到就返回 []。"""
    try:
        d = cget(cfg['comfy_url'] + '/object_info/' + node, timeout=30)
        return list(d[node]['input']['required'][key][0])
    except Exception:
        return []


# 缺模型时用来猜一个最可能对的名字（顺序即优先级）
SUGGEST_HINTS = {
    'unet': ('krea2_turbo', 'krea_turbo', 'krea2', 'krea'),
    'clip': ('qwen3vl_4b', 'qwen3vl', 'qwen3', 'qwen'),
    'vae': ('qwen_image_vae', 'qwen_image', 'vae', 'qwen'),
}


def _base(name):
    return os.path.basename(str(name).replace('/', '\\')).lower()


def _guess(pool, hints, want=''):
    """从候选里挑一个名字最像的，作为下拉框的默认选项。

    三种情形，按优先级：
      1. 同一个文件、只是子目录写法不同（最常见）——按文件名精确命中
      2. 名字里含关键词（krea2_turbo / qwen3vl 等）
      3. 兜底取第一个
    """
    if want:
        wb = _base(want)
        for x in pool:
            if _base(x) == wb:
                return x
    low = [(x, str(x).lower()) for x in pool]
    for h in hints:
        for name, name_l in low:
            if h in name_l:
                return name
    return pool[0] if pool else ''


def preflight(cfg):
    """启动自检：把「能不能跑」这件事在开跑前就说清楚。

    返回的诊断信息同时用于控制台打印和界面提示条，
    目的是让它自己说明哪里不对，而不是等到点开始才报错。
    """
    diag = {
        'python': '%s (%s)' % (sys.version.split()[0],
                               '64位' if sys.maxsize > 2 ** 32 else '32位'),
        'pillow': '',
        'comfy_url': cfg.get('comfy_url', ''),
        'comfy_alive': False,
        'comfy_version': '',
        'comfy_error': '',
        'available': {'unets': [], 'clips': [], 'clip_types': [], 'vaes': []},
        'missing': {},
        'suggested': {},
        'warnings': [],
    }
    if have_pillow():
        import PIL
        diag['pillow'] = PIL.__version__

    alive, ver = comfy_alive(cfg)
    if not alive:
        diag['comfy_error'] = ver
        diag['warnings'].append(
            '连不上 ComfyUI（%s）：%s。'
            '点「开始测试」时本工具会尝试自己把它拉起来（需要 config.json 里的 '
            'comfy_root 指向 ComfyUI 便携版根目录）；也可以先手动启动 ComfyUI。'
            % (cfg.get('comfy_url', '?'), ver))
        return diag

    diag['comfy_alive'] = True
    diag['comfy_version'] = ver
    avail = diag['available']
    avail['unets'] = _node_options(cfg, 'UNETLoader', 'unet_name')
    avail['clips'] = _node_options(cfg, 'CLIPLoader', 'clip_name')
    avail['clip_types'] = _node_options(cfg, 'CLIPLoader', 'type')
    avail['vaes'] = _node_options(cfg, 'VAELoader', 'vae_name')

    # 逐项校验：配的名字在不在 ComfyUI 现有候选里。不在就记下来，
    # 并猜一个最像的候选，让界面上「改选一下」就能修好，不用去动配置文件。
    for key, label, pool in (
            ('unet', '底模(unet)', avail['unets']),
            ('clip', '文本编码器(clip)', avail['clips']),
            ('vae', 'VAE', avail['vaes'])):
        want = (cfg.get(key) or '').strip()
        if want and want in pool:
            continue
        diag['missing'][key] = {'want': want, 'pool': pool,
                                'suggest': _guess(pool, SUGGEST_HINTS[key], want)}
        if diag['missing'][key]['suggest']:
            diag['suggested'][key] = diag['missing'][key]['suggest']
        if not want:
            diag['warnings'].append('没有指定%s。' % label)
        elif not pool:
            diag['warnings'].append(
                '%s配置的是「%s」，但 ComfyUI 里一个都没有 —— '
                '多半是模型文件没放进对应目录。' % (label, want))
        else:
            diag['warnings'].append(
                '%s配置的是「%s」，ComfyUI 里没有这个名字。'
                '在下面「模型设置」里改选一个即可。' % (label, want))

    cts = avail['clip_types']
    ct = (cfg.get('clip_type') or '').strip()
    if cts and ct and ct not in cts:
        diag['missing']['clip_type'] = {
            'want': ct, 'pool': cts,
            'suggest': 'krea2' if 'krea2' in cts else cts[0]}
        diag['suggested']['clip_type'] = diag['missing']['clip_type']['suggest']
        diag['warnings'].append(
            'CLIP 类型配置的是「%s」，当前 ComfyUI 支持的是：%s。'
            % (ct, '、'.join(cts[:8])))
    elif cts and 'krea2' not in cts:
        diag['warnings'].append(
            '当前 ComfyUI 的 CLIPLoader 里没有 krea2 类型 —— '
            '如果你测的不是 Krea2，把 unet / clip / vae / CLIP类型 都改成'
            '你自己的模型即可，本工具不绑定具体底座。')

    if not diag['pillow']:
        diag['warnings'].append('当前 Python 没装 Pillow，拼对比图和评分会失败。'
                                '请改用 ComfyUI 自带的 python_embeded\\python.exe 启动。')
    return diag


# ============================================================ 图像客观指标
_FONT_CACHE = {}


def load_font(size):
    if size not in _FONT_CACHE:
        f = None
        for p in (r'C:\Windows\Fonts\msyh.ttc', r'C:\Windows\Fonts\msyhbd.ttc',
                  r'C:\Windows\Fonts\simhei.ttf', r'C:\Windows\Fonts\arial.ttf'):
            if os.path.exists(p):
                try:
                    f = ImageFont.truetype(p, size)
                    break
                except Exception:
                    pass
        _FONT_CACHE[size] = f or ImageFont.load_default()
    return _FONT_CACHE[size]


def _thumb_arrays(path, size=256):
    """返回 (灰度缩略图 bytes, 灰度图, 彩色缩略图)"""
    im = Image.open(path).convert('RGB').resize((size, size), Image.Resampling.LANCZOS)
    g = im.convert('L')
    return im, g


def analyze(path, baseline=None):
    """无参考图像指标。baseline 为基线图路径时额外算风格偏移量。"""
    require_pillow()
    im, g = _thumb_arrays(path)
    edges = g.filter(ImageFilter.FIND_EDGES)
    sharpness = ImageStat.Stat(edges).stddev[0]
    contrast = ImageStat.Stat(g).stddev[0]
    sat = ImageStat.Stat(im.convert('HSV')).mean[1]

    hist = g.histogram()
    total = float(sum(hist)) or 1.0
    entropy = 0.0
    for c in hist:
        if c:
            p = c / total
            entropy -= p * math.log2(p)

    # 暖色偏移（风格 LoRA 常见的调色倾向）
    r, gr, b = ImageStat.Stat(im).mean
    warmth = (r - b) / 255.0

    diff = None
    if baseline and os.path.exists(baseline):
        a = Image.open(path).convert('RGB').resize((64, 64)).convert('L')
        bb = Image.open(baseline).convert('RGB').resize((64, 64)).convert('L')
        pa, pb = a.tobytes(), bb.tobytes()
        diff = sum(abs(x - y) for x, y in zip(pa, pb)) / (len(pa) * 255.0)

    return {
        'sharpness': round(sharpness, 2),
        'contrast': round(contrast, 2),
        'saturation': round(sat, 1),
        'entropy': round(entropy, 3),
        'warmth': round(warmth, 4),
        'drift': round(diff, 4) if diff is not None else None,
    }


def score_run(cells, strengths):
    """在整批内归一化后打分，并推荐最佳档位。"""
    vals = [c for c in cells if c.get('metrics')]
    if len(vals) < 2:
        for c in cells:
            c['score'] = None
            c['recommended'] = False
        return None

    def norm(key, invert=False):
        xs = [c['metrics'][key] for c in vals if c['metrics'].get(key) is not None]
        if not xs:
            return lambda v: 0.0
        lo, hi = min(xs), max(xs)
        if hi - lo < 1e-9:
            return lambda v: 0.5
        if invert:
            return lambda v: (hi - v) / (hi - lo)
        return lambda v: (v - lo) / (hi - lo)

    n_sharp = norm('sharpness')
    n_ent = norm('entropy')
    n_sat = norm('saturation')
    n_drift = norm('drift')

    for c in cells:
        m = c.get('metrics')
        if not m:
            c['score'] = None
            c['recommended'] = False
            c['degraded'] = False
            continue
        # 质量分：锐度 + 细节 + 饱和度落在合理区间
        quality = 0.45 * n_sharp(m['sharpness']) + 0.35 * n_ent(m['entropy']) \
            + 0.20 * (1.0 - abs(n_sat(m['saturation']) - 0.6) / 0.6)
        drift = n_drift(m.get('drift') or 0.0)
        if c['strength'] == 0:
            c['quality'] = round(quality, 3)
            c['style_gain'] = 0.0
            c['score'] = None
            c['recommended'] = False
            c['degraded'] = False
            continue
        c['quality'] = round(quality, 3)
        c['style_gain'] = round(drift, 3)
        c['score'] = round(0.6 * quality + 0.4 * drift, 3)
        c['recommended'] = False      # 重算时必须显式清掉上一次的推荐标记
        c['degraded'] = False

    # 崩坏判定 + 相对基线的直接比值
    # 比值是「绝对可比」的：不依赖本批归一化，能直接说“比基线锐了/糊了”多少。
    for c in cells:
        c['sharp_ratio'] = None
        c['ent_ratio'] = None
        if c['strength'] == 0 or not c.get('metrics'):
            continue
        base = None
        for b in cells:
            if b['strength'] == 0 and b['prompt_i'] == c['prompt_i'] \
                    and b['lora'] == c['lora']:
                base = b
                break
        bm = (base or {}).get('metrics')
        if bm:
            if bm.get('sharpness'):
                c['sharp_ratio'] = round(c['metrics']['sharpness'] / bm['sharpness'], 3)
            if bm.get('entropy'):
                c['ent_ratio'] = round(c['metrics']['entropy'] / bm['entropy'], 3)
            if (c['metrics']['sharpness'] < bm['sharpness'] * 0.55
                    or c['metrics']['entropy'] < bm['entropy'] * 0.85):
                c['degraded'] = True

    best = None
    for c in cells:
        if c['strength'] == 0 or c.get('score') is None or c.get('degraded'):
            continue
        if best is None or c['score'] > best['score']:
            best = c
    if best:
        best['recommended'] = True
        return best['strength']
    # 全崩了：退而求其次给质量最高的
    pool = [c for c in cells if c['strength'] != 0 and c.get('score') is not None]
    if pool:
        b2 = max(pool, key=lambda x: x['score'])
        b2['recommended'] = True
        return b2['strength']
    return None


def summarize_lora(cells, is_combo=False):
    """把一个 LoRA 的全部格子归纳成一句能直接下判断的话。

    报告顶部只需要回答三件事：选哪个档、这个档比基线好还是差、哪些档不能碰。

    is_combo：V0.5.1 的 LoRA 组合。组合只有一列、没有强度轴，所以
    「推荐强度 1」这种说法对它没意义，改说「组合（各层按设定权重同时上场）」。
    """
    var = [c for c in cells if c.get('state') == 'done' and c.get('metrics')
           and c['strength'] != 0]
    if not var:
        return None

    by_s = {}
    for c in var:
        by_s.setdefault(c['strength'], []).append(c)

    curve = []
    for s in sorted(by_s):
        grp = by_s[s]
        sc = [c['score'] for c in grp if c.get('score') is not None]
        # 这里必须用「真实像素差异」而不是批内归一化后的 style_gain ——
        # 归一化值在同一批里必然有一个 1.0，写进结论就成了「风格偏移 1.00」这种假数字。
        dr = [(c.get('metrics') or {}).get('drift') or 0.0 for c in grp]
        curve.append({
            'strength': s,
            'score': round(sum(sc) / len(sc), 3) if sc else None,
            'drift': round(sum(dr) / len(dr), 4) if dr else 0.0,
            'crashed': any(c.get('degraded') for c in grp),
        })

    best = next((c for c in var if c.get('recommended')), None)
    best_s = best['strength'] if best else None
    crashed = [x['strength'] for x in curve if x['crashed']]
    ok_curve = [x for x in curve if not x['crashed'] and x['score'] is not None]
    # 风格偏移取「推荐档」那一档的值，和「推荐强度 X」对得上；
    # 没有推荐档时退而取非崩坏档里的最大值。
    drift = None
    if best_s is not None:
        drift = next((x['drift'] for x in curve
                      if abs(x['strength'] - best_s) < 1e-9), None)
    if drift is None:
        drift = max((x['drift'] for x in ok_curve), default=0.0)
    sharp = best.get('sharp_ratio') if best else None

    if drift < 0.03:
        effect, effect_txt = 'none', '画面几乎没变化'
    elif drift < 0.10:
        effect, effect_txt = 'weak', '生效轻微'
    elif drift < 0.25:
        effect, effect_txt = 'good', '生效明显'
    else:
        effect, effect_txt = 'strong', '改动幅度很大'

    if best_s is None:
        verdict, verdict_txt = 'bad', '不建议'
    elif effect == 'none':
        verdict, verdict_txt = 'warn', '疑似无效'
    elif sharp is not None and sharp < 0.9:
        verdict, verdict_txt = 'warn', '慎用'
    else:
        verdict, verdict_txt = 'ok', '可用'

    sentence = []
    if best_s is None:
        sentence.append('没有一个档位可用（全部崩坏）')
    elif is_combo:
        # 组合没有强度轴：只有「这一组整体成不成立」这一个问题
        sentence.append('组合成立，各层按设定权重同时上场')
        if sharp is not None:
            sentence.append('画质较基线 %+.0f%%' % ((sharp - 1) * 100))
        sentence.append('风格偏移 %.2f（%s）' % (drift, effect_txt))
    else:
        sentence.append('推荐强度 %g' % best_s)
        if sharp is not None:
            sentence.append('画质较基线 %+.0f%%' % ((sharp - 1) * 100))
        sentence.append('风格偏移 %.2f（%s）' % (drift, effect_txt))
    if crashed:
        sentence.append('%s 档已崩坏' % '、'.join('%g' % x for x in crashed))
    if effect == 'none' and best_s is not None:
        sentence.append('留意这个 LoRA 是否需要触发词')

    # V0.4：品质等级（劣品/下品/中品/上品）。定级只读上面已经算好的指标，
    # 不重新测量，所以同一份报告重算多少次等级都一样。
    grade, grade_why = lora_grade.grade_of({
        'verdict': verdict, 'best': best_s, 'sharp_ratio': sharp,
        'drift': drift, 'crashed': crashed, 'effect': effect,
    })

    return {
        'best': best_s,
        'curve': curve,
        'drift': round(drift, 3),
        'effect': effect,
        'effect_txt': effect_txt,
        'sharp_ratio': sharp,
        'crashed': crashed,
        'verdict': verdict,
        'verdict_txt': verdict_txt,
        'grade': grade,
        'grade_why': grade_why,
        'text': '，'.join(sentence) + '。',
    }


# ============================================================== 工作流构造
def norm_fixed(spec):
    """规范化「固定 LoRA」列表：去空、去强度 0、去重（按加载顺序保留第一个）。

    spec['fixed'] 允许两种写法：[{'name':..,'strength':..}, ...] 或 ['a.safetensors', ...]。
    """
    out, seen = [], set()
    for fx in (spec.get('fixed') or []):
        if isinstance(fx, str):
            fx = {'name': fx, 'strength': 1.0}
        if not isinstance(fx, dict):
            continue
        name = str(fx.get('name') or '').strip()
        if not name or name in seen:
            continue
        try:
            st = float(fx.get('strength', 1.0))
        except (TypeError, ValueError):
            st = 1.0
        if st == 0:
            continue
        seen.add(name)
        out.append({'name': name, 'strength': st})
    return out


def fixed_label(fixed, short=True):
    """固定组的人类可读描述；空组返回「无」。"""
    if not fixed:
        return '无'
    return ' + '.join('%s(%g)' % (short_name(fx['name']) if short else fx['name'],
                                  fx['strength']) for fx in fixed)


def norm_triggers(spec):
    """规范化触发词表：{loRA 全名: '触发词'}。空值、空白一律丢弃。"""
    out = {}
    raw = spec.get('triggers') or {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        name = str(k or '').strip()
        val = ' '.join(str(v or '').split()).strip(' ,')
        if name and val:
            out[name] = val
    return out


# ============================================== V0.5.1：LoRA 组合（多 LoRA 同台）
# 用户的原话：「增加 lora 组合单元，类似权重 lora 加载器那种，两个以上的
# lora 组合生图的单元」。
#
# 语义：一个「组合」= 2 个以上 LoRA 各自带权重，同时挂在模型链上出图
# （和 ComfyUI 里叠好几个 LoraLoader 是一回事）。跟「对比 LoRA」的区别在于
# 对比 LoRA 是「一个 LoRA 换不同强度逐个扫」，组合是「一次上场好几个，
# 各有各的权重」。
#
# 组合走的是**独立的一组**，不跟逐档扫混在同一张 contact sheet 里：
# 混进来就没法画强度-分数曲线了（组合只有一列，没有强度轴）。
COMBO_STRENGTH = 1.0          # 组合在内部当作「1.0 档」，只为复用打分逻辑


def norm_combos(spec):
    """规范化「LoRA 组合」列表。

    只保留 items ≥ 2 的组合：一个 LoRA 的「组合」就是普通扫档，
    留在这一栏没意义，直接丢掉，免得界面上冒出一个只有一项的组合。
    """
    out = []
    for ci, cb in enumerate(spec.get('combos') or []):
        if not isinstance(cb, dict):
            continue
        items, seen = [], set()
        for it in (cb.get('items') or []):
            if isinstance(it, str):
                it = {'name': it, 'strength': 1.0}
            if not isinstance(it, dict):
                continue
            name = str(it.get('name') or '').strip()
            if not name or name in seen:
                continue
            try:
                st = float(it.get('strength', 1.0))
            except (TypeError, ValueError):
                st = 1.0
            if st == 0:
                continue          # 权重 0 = 这一层不加载，等于没写
            seen.add(name)
            items.append({'name': name, 'strength': st})
        if len(items) < 2:
            continue
        auto = ' + '.join(short_name(i['name']) for i in items)
        label = str(cb.get('label') or '').strip() or auto
        out.append({
            'label': label,
            'items': items,
            'ci': ci,
            'detail': ' + '.join('%s(%g)' % (short_name(i['name']), i['strength'])
                                 for i in items),
        })
    return out


def scan_groups(spec):
    """把「这次要验什么」拆成一组一组的对比单元。

    返回 [{i, key, lora, short, is_combo, items, strengths, ci}]，
    顺序 = 先逐个对比 LoRA（各带强度轴），再每个 LoRA 组合（只有一列）。

    **只在这里定义分组**：跑图、出 contact sheet、写报告、重算都调它，
    免得四处各写一份「有哪些组」，改一处漏三处。
    """
    loras = [str(x).strip() for x in (spec.get('loras') or []) if str(x).strip()]
    strengths = []
    for s in (spec.get('strengths') or []):
        try:
            v = float(s)
        except (TypeError, ValueError):
            continue
        if v != 0 and v not in strengths:
            strengths.append(v)
    strengths.sort()

    groups = []
    for li, lora in enumerate(loras):
        groups.append({'i': li, 'ci': None, 'key': slug(lora), 'lora': lora,
                       'short': short_name(lora), 'is_combo': False,
                       'items': None, 'strengths': strengths})
    for cb in norm_combos(spec):
        groups.append({'i': len(loras) + cb['ci'], 'ci': cb['ci'],
                       'key': 'combo%d_%s' % (cb['ci'] + 1, slug(cb['label'])),
                       'lora': cb['label'], 'short': short_name(cb['label']),
                       'is_combo': True, 'items': cb['items'],
                       'detail': cb['detail'],
                       'strengths': [COMBO_STRENGTH]})
    return groups


def extra_names(g):
    """组合组里参与的所有 LoRA 名（普通组返回 None）。

    组合的触发词要按成分各自的来 —— 组合本身没有触发词，
    拿组合的 label 去查表永远查不到。
    """
    if not g.get('is_combo'):
        return None
    return [it['name'] for it in (g.get('items') or [])]


def scan_cell_file(g, pi, s):
    """一个格子在产物目录里的文件名。

    老的两条命名（base_p%d / L%d_p%d_s%g）**必须原样保留** ——
    重算旧记录时要能按同样的规则把老图再找回来。
    """
    if s == 0:
        return 'base_p%d.png' % pi
    if g.get('is_combo'):
        return 'C%d_p%d.png' % (g['ci'], pi)
    return 'L%d_p%d_s%s.png' % (g['i'], pi, ('%g' % s))


def compose_prompt(spec, lora, prompt_text, also=None):
    """把触发词拼到提示词前面。

    顺序：固定组触发词 → 被对比 LoRA 的触发词 → 原始提示词。
    基线格子传 lora=None，所以不会带被对比 LoRA 的触发词 ——
    否则基线和对比图的差异里会混进触发词的影响，测出来就不准了。

    also：V0.5.1 的 LoRA 组合用。组合没有单个「被对比 LoRA」，
    参与的几个 LoRA 各自的触发词都要带上（名字按传入顺序）。
    """
    trig = norm_triggers(spec)
    parts = []
    for fx in norm_fixed(spec):
        t = trig.get(fx['name'])
        if t and t not in parts:
            parts.append(t)
    names = ([lora] if lora else []) + list(also or [])
    for nm in names:
        t = trig.get(nm)
        if t and t not in parts:
            parts.append(t)
    body = ' '.join(str(prompt_text or '').split()).strip()
    if body:
        parts.append(body)
    return ', '.join(parts)


def build_workflow(cfg, spec, lora, strength, prompt_text, prefix, seed, extra=None):
    """extra：V0.5.1 的 LoRA 组合。给了就用它当「变量 LoRA 链」
    （2 个以上，各自带权重），替代单个 lora+strength。"""
    wf = {
        '1': {'class_type': 'UNETLoader',
              'inputs': {'unet_name': spec.get('unet') or cfg['unet'],
                         'weight_dtype': 'default'}},
        '2': {'class_type': 'CLIPLoader',
              'inputs': {'clip_name': cfg['clip'],
                         'type': cfg.get('clip_type', 'krea2'),
                         'device': 'default'}},
        '3': {'class_type': 'VAELoader', 'inputs': {'vae_name': cfg['vae']}},
        '4': {'class_type': 'CLIPTextEncode',
              'inputs': {'text': prompt_text, 'clip': ['2', 0]}},
        '5': {'class_type': 'CLIPTextEncode',
              'inputs': {'text': '', 'clip': ['2', 0]}},
        '6': {'class_type': 'ConditioningZeroOut',
              'inputs': {'conditioning': ['5', 0]}},
        '7': {'class_type': 'EmptyLatentImage',
              'inputs': {'width': int(spec['width']), 'height': int(spec['height']),
                         'batch_size': 1}},
        '9': {'class_type': 'VAEDecode',
              'inputs': {'samples': ['8', 0], 'vae': ['3', 0]}},
        '10': {'class_type': 'SaveImage',
               'inputs': {'images': ['9', 0], 'filename_prefix': prefix}},
    }
    model_src = ['1', 0]
    lora_node = cfg.get('lora_node', 'LoraLoaderModelOnly')

    # 固定 LoRA 链：UNETLoader -> fixed[0] -> fixed[1] -> …（每张图都会带上）
    nid = 20
    for fx in norm_fixed(spec):
        wf[str(nid)] = {'class_type': lora_node,
                        'inputs': {'lora_name': fx['name'],
                                   'strength_model': float(fx['strength']),
                                   'model': model_src}}
        model_src = [str(nid), 0]
        nid += 1

    # 变量 LoRA 接在固定链末尾：只有它参与扫档。
    # V0.5.1：extra 非空时它是「一组组合」，链上有几个就串几层。
    # id 从 40 起，避开固定链的 20~27 和骨架的 1~10，组合层数再多也不撞。
    var = [dict(x) for x in extra] if extra else (
        [{'name': lora, 'strength': strength}] if (lora and strength != 0) else [])
    nid = 40
    for v in var:
        wf[str(nid)] = {'class_type': lora_node,
                        'inputs': {'lora_name': v['name'],
                                   'strength_model': float(v['strength']),
                                   'model': model_src}}
        model_src = [str(nid), 0]
        nid += 1
    wf['8'] = {'class_type': 'KSampler', 'inputs': {
        'seed': int(seed), 'steps': int(spec['steps']), 'cfg': float(spec['cfg']),
        'sampler_name': spec['sampler'], 'scheduler': spec['scheduler'],
        'denoise': 1.0, 'model': model_src,
        'positive': ['4', 0], 'negative': ['6', 0], 'latent_image': ['7', 0]}}
    return {'prompt': wf, 'client_id': CLIENT_ID}


# ================================================= 测试工作流：内置 / 本地
# V0.5 新增。用户的原话：
#   「测试界面继续沿用网页版，可选择用户本地的工作流进行测试工作，若用户
#     没有相应的测试工作流，桌面版应绑定加入测试用途的工作流，供用户选择」
#
# 于是这里管两件事：
#   1) 内置测试工作流 —— 随包发出去的 workflows/*.json（里面的
#      __UNET__ / __CLIP__ / __VAE__ 占位符在运行时按本机 config 填上），
#      外加一个「引擎生成」的虚拟项（原来的做法，永远可用）；
#   2) 用户本地工作流 —— ComfyUI 的 user/default/workflows/ 里导出的 json。
#
# 两种格式都认：
#   · API 格式  {"1": {"class_type": …, "inputs": …}}  —— 直接改
#   · 界面格式  {"nodes": [...], "links": [...]}        —— 先转成 API 格式
# 界面格式里每个节点的控件顺序是 ComfyUI 前端定的，这里不可能复刻全部节点，
# 所以内置了一张常见节点的顺序表；表里没有的节点只保留连线、不带控件值，
# 并在 warnings 里点名，提示用户「这个工作流建议先在 ComfyUI 里导出 API 格式」。
WF_PLACEHOLDERS = ('__UNET__', '__CLIP__', '__CLIP_TYPE__', '__VAE__')

_WIDGET_ORDER = {
    'KSampler': ['seed', 'control_after_generate', 'steps', 'cfg',
                 'sampler_name', 'scheduler', 'denoise'],
    'KSamplerAdvanced': ['add_noise', 'noise_seed', 'control_after_generate',
                         'steps', 'cfg', 'sampler_name', 'scheduler',
                         'start_at_step', 'end_at_step',
                         'return_with_leftover_noise'],
    'SamplerCustom': ['add_noise', 'noise_seed', 'control_after_generate'],
    'CLIPTextEncode': ['text'],
    'CLIPTextEncodeSDXL': ['width', 'height', 'crop_w', 'crop_h', 'target_width',
                           'target_height', 'text_g', 'text_l'],
    'EmptyLatentImage': ['width', 'height', 'batch_size'],
    'EmptySD3LatentImage': ['width', 'height', 'batch_size'],
    'LoraLoader': ['lora_name', 'strength_model', 'strength_clip'],
    'LoraLoaderModelOnly': ['lora_name', 'strength_model'],
    'CheckpointLoaderSimple': ['ckpt_name'],
    'UNETLoader': ['unet_name', 'weight_dtype'],
    'CLIPLoader': ['clip_name', 'type', 'device'],
    'VAELoader': ['vae_name'],
    'SaveImage': ['filename_prefix'],
    'ImageScale': ['upscale_method', 'width', 'height', 'crop'],
    'Note': ['text'],
    'PrimitiveNode': [],
}


def _wf_nodes(wf):
    """遍历真正的节点，跳过 _ 开头的说明键。

    工作流 json 是我们自己发出去的，里面写了 _hx_note 之类的注释；
    ComfyUI 自己的导出里也可能混进非节点字段，所以遍历一律走这里。
    """
    for nid, n in (wf or {}).items():
        if isinstance(nid, str) and nid.startswith('_'):
            continue
        if isinstance(n, dict) and n.get('class_type'):
            yield str(nid), n


def _wf_strip_comments(wf):
    for k in [k for k in (wf or {}) if isinstance(k, str) and k.startswith('_')]:
        wf.pop(k, None)
    return wf


# ------------------------------------------------- ComfyUI 节点表（转换的依据）
# 光靠内置表转界面格式的工作流是不够的：实测用户目录里 71 个工作流，
# 里面混着 MarkdownNote / Reroute / rgthree 的界面件（后端根本不认，
# 提交上去整张图都会被拒），还有 rgthree 的「Lora Loader Stack」这类
# 第三方节点 —— 每个节点的控件名和顺序都不该由我们猜。
# ComfyUI 自己的 /object_info 里就有权威答案，所以：
#   · 拉一次 /object_info（16 MB / 几秒），只保留「每个类型的控件顺序」
#     和「有哪些类型」，派生结果落盘缓存；
#   · 缓存里缺哪个类型（用户刚装了新节点）就重拉一次；
#   · ComfyUI 没开的时候退回内置表 + 只做纯连线转换。
_OBJECT_INFO = {'key': None, 'order': None, 'missing': set()}
_OBJECT_INFO_LOCK = threading.Lock()
OBJECT_INFO_CACHE = os.path.join(HERE, '_object_info_cache.json')


def _schema_widget_order(node):
    """按 ComfyUI 的 INPUT_TYPES 推出控件顺序（和前端建节点的规则一致）。

    规则：required + optional 按声明顺序扫，类型是 COMBO（列表）或
    INT/FLOAT/STRING/BOOLEAN 的算控件；带 forceInput 的只当连线口；
    INT/FLOAT 带 control_after_generate 的后面会多一个控件
    （固定 / 递增 / 递减 / 随机），所以顺序里要补一个占位符。
    """
    out = []
    inp = (node or {}).get('input') or {}
    for group in ('required', 'optional'):
        for name, spec in (inp.get(group) or {}).items():
            if not isinstance(spec, (list, tuple)) or not spec:
                continue
            typ = spec[0]
            cfg = spec[1] if len(spec) > 1 and isinstance(spec[1], dict) else {}
            if cfg.get('forceInput'):
                continue
            if isinstance(typ, list) or typ in ('INT', 'FLOAT', 'STRING', 'BOOLEAN'):
                out.append(name)
                if cfg.get('control_after_generate'):
                    out.append('control_after_generate')
    return out


def _fetch_object_info(cfg):
    t0 = time.time()
    url = cfg['comfy_url'].rstrip('/') + '/object_info'
    with urllib.request.urlopen(urllib.request.Request(url), timeout=300) as r:
        raw = r.read()
    data = json.loads(raw.decode('utf-8'))
    order = {}
    for name, node in data.items():
        if isinstance(node, dict):
            order[name] = _schema_widget_order(node)
    print('  [节点表] %d 个类型 / %.1f MB / %.1fs'
          % (len(order), len(raw) / 1048576.0, time.time() - t0))
    sys.stdout.flush()
    return order


def _ui_node_types(ui):
    out = []
    for n in ((ui or {}).get('nodes') or []):
        t = n.get('type')
        if t and t not in out:
            out.append(t)
    return out


def comfy_schema(cfg, need=None):
    """拿到 {节点类型: [控件名…]}；拿不到返回 None（调用方走内置表兜底）。"""
    key = (cfg.get('comfy_url') or '').rstrip('/')
    with _OBJECT_INFO_LOCK:
        order = _OBJECT_INFO['order'] if _OBJECT_INFO['key'] == key else None
        missing = set(_OBJECT_INFO['missing'])
    if order is None:
        c = load_json(OBJECT_INFO_CACHE, None)
        if (isinstance(c, dict) and c.get('url') == key
                and isinstance(c.get('order'), dict)):
            order = c['order']
            missing = set(c.get('missing') or [])
            with _OBJECT_INFO_LOCK:
                _OBJECT_INFO.update(key=key, order=order, missing=missing)
    # 缓存里没有的类型：可能是新装的节点，重拉一次；拉完还是缺就记住，
    # 免得每张图都去下 16 MB
    if order is not None and need:
        want = set(need) - set(order.keys()) - missing
        if want:
            order = None
    if order is None:
        if not key:
            return None
        try:
            alive, _v = comfy_alive(cfg)
        except Exception:
            alive = False
        if not alive:
            return None
        try:
            order = _fetch_object_info(cfg)
        except Exception:
            bug('拉取 ComfyUI 节点表')
            return None
        still = sorted(set(need or []) - set(order.keys()))
        with _OBJECT_INFO_LOCK:
            _OBJECT_INFO.update(key=key, order=order, missing=set(still))
        try:
            save_json(OBJECT_INFO_CACHE, {'url': key, 'order': order,
                                          'missing': still})
        except OSError:
            pass
    return order


def _wf_is_api(d):
    """是不是 API 格式（每个值都是带 class_type 的节点）。"""
    if not isinstance(d, dict) or not d:
        return False
    nodes = [v for k, v in d.items()
             if not (isinstance(k, str) and k.startswith('_'))]
    if not nodes:
        return False
    for v in nodes:
        if not isinstance(v, dict) or not v.get('class_type'):
            return False
    return True


def _ui_to_api(ui, order_map=None):
    """界面格式 → API 格式。

    order_map 是 ComfyUI 的 {类型: [控件名…]}；给了它就用权威顺序，
    没有（ComfyUI 没开）就退回内置的 _WIDGET_ORDER。

    返回 (wf, warns, unresolved)：
      warns      认不出控件顺序的节点类型（这些节点的值会丢，可能跑不起来）
      unresolved 转完之后仍然指向不存在节点的连线 —— 有它就没法提交，
                 必须让用户去 ComfyUI 里「导出（API）」
    """
    nodes = ui.get('nodes') or []
    link_map = {}
    for l in (ui.get('links') or []):
        if isinstance(l, (list, tuple)) and len(l) >= 5:
            # 数组格式：[link_id, 来源节点, 来源槽, 目标节点, 目标槽, 类型]
            link_map[l[0]] = (str(l[1]), int(l[2] or 0))
        elif isinstance(l, dict):
            link_map[l.get('id')] = (str(l.get('origin_id')),
                                     int(l.get('origin_slot') or 0))

    lower_map = {}
    for k in (order_map or {}):
        lower_map.setdefault(k.lower(), k)

    def order_of(ctype):
        if order_map is not None:
            if ctype in order_map:
                return list(order_map[ctype])
            hit = lower_map.get(str(ctype).lower())
            if hit is not None:
                return list(order_map[hit])
        if ctype in _WIDGET_ORDER:
            return list(_WIDGET_ORDER[ctype])
        return None

    raw, warns = {}, []
    for n in nodes:
        nid = str(n.get('id'))
        ctype = n.get('type') or ''
        inputs = {}
        for idx, slot in enumerate(n.get('inputs') or []):
            if not isinstance(slot, dict):
                continue
            w = slot.get('widget')
            name = (w.get('name') if isinstance(w, dict) else None) or slot.get('name')
            lid = slot.get('link')
            # Reroute 这类节点的输入槽是没有名字的，给它个内部占位名，
            # 后面「直通改写」要用它找出上游
            key = name or ('_in%d' % idx)
            if lid is not None and lid in link_map:
                src, src_slot = link_map[lid]
                inputs[key] = [src, src_slot]
        wv = n.get('widgets_values')
        if isinstance(wv, list) and wv:
            od = order_of(ctype)
            if od is None:
                if ctype and ctype not in warns:
                    warns.append(ctype)
            else:
                # 老版导出的 KSampler 少一项 control_after_generate，按长度判
                if len(wv) == len(od) - 1 and 'control_after_generate' in od:
                    od = [k for k in od if k != 'control_after_generate']
                for i, key in enumerate(od):
                    if i < len(wv) and key != 'control_after_generate':
                        inputs[key] = wv[i]
        raw[nid] = {'class_type': ctype, 'inputs': inputs}

    unresolved = []
    if order_map is not None:
        known = set(order_map.keys())
        dropped = [nid for nid, nd in raw.items()
                   if nd['class_type'] not in known]
        # 直通改写：像 Reroute 这种节点删掉以后，下游要接回它的上游。
        # 判定标准就是「它只有一个连线输入」—— 出线数量不限（可以一进多出）。
        thru = {}
        for nid in dropped:
            ins = [v for v in raw[nid]['inputs'].values()
                   if isinstance(v, (list, tuple)) and v]
            if len(ins) == 1 and str(ins[0][0]) != nid:
                thru[nid] = [str(ins[0][0]), int(ins[0][1] or 0)]

        def resolve(ref, depth=0):
            """顺着直通链往上找，直到一个没被删掉的节点。"""
            if depth > 32 or ref is None:
                return None
            nid = str(ref[0])
            if nid not in dropped:
                return ref
            nxt = thru.get(nid)
            if nxt is None:
                return None
            return resolve(nxt, depth + 1)

        for nid, node in raw.items():
            if nid in dropped:
                continue
            for k, v in list(node['inputs'].items()):
                if not (isinstance(v, (list, tuple)) and v):
                    continue
                if str(v[0]) not in dropped:
                    continue
                nv = resolve(v)
                if nv is None:
                    unresolved.append('%s 的 %s 输入来自界面专用节点 %s'
                                      % (node['class_type'], k, v[0]))
                    node['inputs'].pop(k, None)
                else:
                    node['inputs'][k] = nv
        for nid in dropped:
            raw.pop(nid, None)
    return raw, warns, unresolved


def _wf_free_id(wf):
    n = 1
    for k in wf.keys():
        try:
            n = max(n, int(k) + 1)
        except (TypeError, ValueError):
            pass
    return n


def _wf_find(wf, *names):
    """按 class_type 找节点。先精确匹配，一个都没有再做子串匹配。"""
    exact, fuzzy = [], []
    for nid, n in _wf_nodes(wf):
        ct = str(n.get('class_type') or '')
        if ct in names:
            exact.append((nid, n))
        elif any(k.lower() in ct.lower() for k in names):
            fuzzy.append((nid, n))
    return exact or fuzzy


def _wf_clip_source(wf):
    """找一个 CLIP 输出，给 LoraLoader 的 clip 输入用。"""
    for _nid, n in _wf_find(wf, 'CLIPTextEncode', 'CLIPTextEncodeSDXL'):
        c = (n.get('inputs') or {}).get('clip')
        if isinstance(c, (list, tuple)) and c:
            return [str(c[0]), int(c[1] or 0)]
    for nid, n in _wf_find(wf, 'CLIPLoader'):
        return [str(nid), 0]
    for nid, n in _wf_find(wf, 'CheckpointLoaderSimple'):
        return [str(nid), 1]          # CLIP 是第二个输出
    return None


# 提示词在不同节点里可能叫这些名字。CLIPTextEncode 用 text，
# Qwen 系的 TextEncodeQwenImageEditPlus 用 prompt，SDXL 用 text_g/text_l。
TEXT_KEYS = ('text', 'prompt', 'text_g', 'text_l')


def _wf_text_in(wf, nid):
    """这个节点里有没有可写的文本控件？返回 (nid, node, key)。"""
    node = wf.get(str(nid))
    if not isinstance(node, dict):
        return None
    inp = node.get('inputs') or {}
    for k in TEXT_KEYS:
        if isinstance(inp.get(k), str):
            return str(nid), node, k
    return None


def _wf_text_target(wf, smps):
    """找提示词该写进哪个节点。

    顺序：采样器的正向 conditioning 直连的节点 → 顺着正向那条链往上找
    （工作流常套 ConditioningSetArea / Combine 之类）→ 退而求其次挑第一个
    带文本控件的节点（这时会报一条告警，因为可能是反向了）。
    """
    seeds = []
    for _nid, n in smps:
        inp = n.get('inputs') or {}
        for k in ('positive', 'guider', 'cond'):
            v = inp.get(k)
            if isinstance(v, (list, tuple)) and v and str(v[0]) in wf:
                seeds.append(str(v[0]))
    # SamplerCustomAdvanced 的正向藏在 guider（CFGGuider）里，多展开一层
    for sid in list(seeds):
        node = wf.get(sid) or {}
        for k in ('positive', 'conditioning'):
            v = (node.get('inputs') or {}).get(k)
            if isinstance(v, (list, tuple)) and v and str(v[0]) in wf:
                seeds.append(str(v[0]))

    for sid in seeds:
        hit = _wf_text_in(wf, sid)
        if hit:
            return hit, True

    seen, cur = set(), list(seeds)
    for _depth in range(8):
        nxt = []
        for c in cur:
            if c in seen:
                continue
            seen.add(c)
            hit = _wf_text_in(wf, c)
            if hit:
                return hit, True
            node = wf.get(c) or {}
            for k, v in (node.get('inputs') or {}).items():
                if k in ('model', 'clip', 'vae', 'latent_image', 'samples',
                         'negative', 'image', 'mask'):
                    continue
                if isinstance(v, (list, tuple)) and v and str(v[0]) in wf:
                    nxt.append(str(v[0]))
        cur = nxt

    for nid, n in _wf_nodes(wf):
        for k in TEXT_KEYS:
            if isinstance((n.get('inputs') or {}).get(k), str):
                return (nid, n, k), False
    return None, False


def _is_lora_node(ctype, inputs=None):
    """认 LoRA 加载节点。除了官方的 LoraLoader / LoraLoaderModelOnly，
    还有 rgthree 的「Lora Loader Stack」这类第三方节点 —— 它们的控件名
    是 lora_01/strength_01…，靠「输入里有 lora 字样」也能认出来。"""
    c = str(ctype or '').lower().replace(' ', '')
    if 'loraloader' in c or 'lorastack' in c or 'loraloaderstack' in c:
        return True
    if 'lora' in c:
        for k in (inputs or {}):
            if 'lora' in str(k).lower():
                return True
    return False


def _lora_slot(inputs):
    """返回 (LoRA 名控件名, 强度控件名)；找不到返回 (None, None)。"""
    inputs = inputs or {}
    if 'lora_name' in inputs:
        for sk in ('strength_model', 'strength_clip', 'strength'):
            if sk in inputs:
                return 'lora_name', sk
        return 'lora_name', None
    cands = [k for k in inputs if str(k).lower().startswith('lora_')]
    if not cands:
        return None, None
    name_key = sorted(cands)[0]
    tail = str(name_key).split('_')[-1]
    for sk in ('strength_' + tail, 'strength'):
        if sk in inputs:
            return name_key, sk
    return name_key, None


def _wf_apply_placeholders(wf, cfg):
    """把内置工作流里的 __UNET__ / __CLIP__ / __VAE__ 换成这台机器的配置。"""
    sub = {'__UNET__': cfg.get('unet', ''),
           '__CLIP__': cfg.get('clip', ''),
           '__CLIP_TYPE__': cfg.get('clip_type', 'krea2'),
           '__VAE__': cfg.get('vae', '')}
    n = 0
    for _nid, node in _wf_nodes(wf):
        for k, v in list((node.get('inputs') or {}).items()):
            if isinstance(v, str) and v in sub:
                node['inputs'][k] = sub[v]
                n += 1
    return n


def patch_workflow(wf, cfg, spec, lora, strength, prompt_text, prefix, seed,
                   extra=None):
    """把「这次要测什么」写进一张已有的工作流。返回 (notes, warns)。

    原则和引擎生成的那张保持一致：同一张图里，固定 LoRA 先串、被扫的 LoRA
    串在最后，正面提示词只写进正向 conditioning —— 这样格子之间的差异
    才只来自被扫的 LoRA 强度。

    extra：V0.5.1 的 LoRA 组合（2 个以上、各自带权重），替代单个 lora+strength。
    """
    notes, warns = [], []

    # ---------------- 提示词：优先写进正向 conditioning 那条链上的编码节点
    smps_all = _wf_find(wf, 'KSampler', 'KSamplerAdvanced', 'SamplerCustom',
                        'SamplerCustomAdvanced')
    tgt, direct = _wf_text_target(wf, smps_all)
    if tgt:
        nid, node, key = tgt
        node['inputs'][key] = prompt_text
        notes.append('提示词→节点 %s(%s)' % (nid, key))
        if not direct:
            warns.append('没从采样器顺出正向 conditioning，提示词写进了第一个带文本的'
                         '节点（%s），可能不是你想要的那一路' % nid)
    else:
        warns.append('工作流里找不到可写的文本节点，提示词没写进去')

    # ---------------- 采样参数
    smps = _wf_find(wf, 'KSampler', 'KSamplerAdvanced')
    if smps:
        for nid, n in smps:
            inp = n.setdefault('inputs', {})
            sk = 'noise_seed' if 'noise_seed' in inp else 'seed'
            if sk in inp or not inp:
                try:
                    inp[sk] = int(seed)
                except (TypeError, ValueError):
                    pass
            for k, v in (('steps', int(spec['steps'])), ('cfg', float(spec['cfg'])),
                         ('sampler_name', spec['sampler']),
                         ('scheduler', spec['scheduler'])):
                if k in inp:
                    inp[k] = v
        notes.append('采样参数→节点 %s' % '、'.join(i for i, _ in smps))
    else:
        warns.append('工作流里找不到 KSampler，seed / 步数 / CFG 没能写进去')

    # ---------------- 尺寸
    lat = [(i, n) for i, n in _wf_find(wf, 'EmptyLatentImage', 'EmptySD3LatentImage')
           if 'width' in (n.get('inputs') or {})]
    if not lat:
        lat = [(i, n) for i, n in _wf_nodes(wf)
               if 'width' in (n.get('inputs') or {})
               and 'height' in (n.get('inputs') or {})]
    if lat:
        lat[0][1]['inputs']['width'] = int(spec['width'])
        lat[0][1]['inputs']['height'] = int(spec['height'])
        notes.append('尺寸→节点 %s' % lat[0][0])
    else:
        warns.append('工作流里找不到宽高节点（EmptyLatentImage 等），尺寸沿用工作流自己的')

    # ---------------- LoRA 链
    chain = [dict(f) for f in norm_fixed(spec)]
    var = [dict(x) for x in extra] if extra else (
        [{'name': lora, 'strength': strength}]
        if (lora and float(strength) != 0) else [])
    chain.extend(var)
    lora_node = str(cfg.get('lora_node') or 'LoraLoaderModelOnly')
    smp = smps[0] if smps else None
    if smp is None:
        if chain:
            warns.append('找不到采样器，LoRA 没能插进去')
    else:
        smp_in = smp[1].setdefault('inputs', {})
        src = smp_in.get('model')
        if not (isinstance(src, (list, tuple)) and src and str(src[0]) in wf):
            if chain:
                warns.append('采样器的 model 输入不是本工作流内部的节点，'
                             'LoRA 没敢插进去（请手动加一个 LoraLoader）')
        else:
            head_id = str(src[0])
            head = wf.get(head_id) or {}
            head_ct = str(head.get('class_type') or '')
            # 采样器上游正好挂着一个 LoRA 节点（官方 LoraLoader、rgthree 的
            # 堆叠器都算），复用它比另插一个更贴合这份工作流的结构。
            reuse = head_id if _is_lora_node(head_ct, head.get('inputs')) else None
            cur = [head_id, 0]
            nid = _wf_free_id(wf)
            touched = []
            if not chain:
                # 基线格子：工作流里那个 preset LoRA 必须先关掉，
                # 否则「基线」还带着它，和被扫的档位就没有可比性了
                if reuse:
                    _nkey, _skey = _lora_slot(wf[reuse].get('inputs'))
                    inp = wf[reuse].setdefault('inputs', {})
                    closed = []
                    for k in ('strength_model', 'strength_clip'):
                        if k in inp:
                            inp[k] = 0.0
                            closed.append(k)
                    if not closed and _skey:
                        inp[_skey] = 0.0
                        closed.append(_skey)
                    if closed:
                        touched.append(reuse)
                        notes.append('基线：节点 %s 的 LoRA 强度已置 0' % reuse)
            for k, fx in enumerate(chain):
                if k == 0 and reuse:
                    inp = wf[reuse].setdefault('inputs', {})
                    nkey, skey = _lora_slot(inp)
                    inp[nkey or 'lora_name'] = fx['name']
                    if skey:
                        inp[skey] = float(fx['strength'])
                    cur = [reuse, 0]
                    touched.append(reuse)
                    continue
                new_id = str(nid)
                nid += 1
                node = {'class_type': lora_node,
                        'inputs': {'lora_name': fx['name'],
                                   'strength_model': float(fx['strength']),
                                   'model': cur}}
                if lora_node == 'LoraLoader':
                    cs = _wf_clip_source(wf)
                    if cs:
                        node['inputs']['clip'] = cs
                        node['inputs']['strength_clip'] = float(fx['strength'])
                    else:
                        node['class_type'] = 'LoraLoaderModelOnly'
                wf[new_id] = node
                cur = [new_id, 0]
                touched.append(new_id)
            if chain:
                smp_in['model'] = cur
            if touched:
                notes.append('LoRA→节点 %s' % '、'.join(touched))

    # ---------------- 出图落点：一律写进本次运行的产物目录
    saves = [(i, n) for i, n in _wf_find(wf, 'SaveImage')
             if str(n.get('class_type')) == 'SaveImage']
    if saves:
        for _nid, n in saves:
            n.setdefault('inputs', {})['filename_prefix'] = prefix
        notes.append('出图目录→节点 %s（文件名前缀 %s）'
                     % ('、'.join(i for i, _ in saves), prefix))
    else:
        warns.append('工作流里没有 SaveImage 节点，出的图取不回来，报告会是空的')
    return notes, warns


def load_workflow_file(path, cfg=None):
    """读一个工作流 json，返回 (api_wf, fmt, warns)。fmt 为 'api' | 'ui'。"""
    raw = load_json(path, None)
    if not isinstance(raw, dict):
        raise RuntimeError('读不出这个工作流（不是合法 JSON 对象）')
    if _wf_is_api(raw):
        wf = _wf_strip_comments(raw)
        _check_wf_struct(wf)
        return wf, 'api', []
    # 有些导出会把两种格式一起包一层：{"prompt": <api>, "workflow": <ui>}
    if _wf_is_api(raw.get('prompt')):
        wf = _wf_strip_comments(raw['prompt'])
        _check_wf_struct(wf)
        return wf, 'api', []
    if isinstance(raw.get('workflow'), dict) and raw['workflow'].get('nodes'):
        raw = raw['workflow']
    if not (raw.get('nodes')):
        raise RuntimeError('这个文件既不是 API 格式也不是界面格式的工作流')

    cfg = cfg or load_config()
    names = _ui_node_types(raw)
    order_map = comfy_schema(cfg, need=names)
    wf, warns, unresolved = _ui_to_api(raw, order_map)
    if not wf:
        raise RuntimeError('工作流里没有可用的节点')
    if unresolved:
        raise RuntimeError(
            '这个工作流转不成可提交的形式：里面用了 ComfyUI 界面专用的中转节点，'
            '而它们的上游接不回来。\n请在 ComfyUI 里打开它，用「工作流 → 导出（API）」'
            '另存一份，再选那一份。\n（%s）' % '；'.join(unresolved[:4]))
    if order_map is None:
        warns.append('连不上 ComfyUI，没法核对节点表，只能按内置的常见节点表转；'
                     '失败的话就先启动 ComfyUI，或用「导出（API）」')
    msg = '界面格式工作流，已自动转成 API 格式'
    if warns:
        msg += ('（这些节点没查到控件顺序，值会丢，可能跑不起来：%s —— '
                '建议在 ComfyUI 里「工作流 → 导出（API）」再导一次）'
                % '、'.join(warns[:6]))
    _check_wf_struct(wf)
    return wf, 'ui', [msg]


def _check_wf_struct(wf):
    """提交前先自己看一眼结构，把「必错」的情况说清楚。

    与其让 ComfyUI 回一句难懂的报错，不如这里就告诉用户是工作流的问题。
    """
    ids = set(str(k) for k in wf.keys())
    broken = []
    for nid, node in _wf_nodes(wf):
        for k, v in (node.get('inputs') or {}).items():
            if isinstance(v, (list, tuple)) and v and str(v[0]) not in ids:
                broken.append('%s.%s → 不存在的节点 %s' % (nid, k, v[0]))
    if broken:
        raise RuntimeError('工作流里有断掉的连线（指向已被删除的节点）：\n'
                           + '\n'.join(broken[:6]))
    if not _wf_find(wf, 'KSampler', 'KSamplerAdvanced', 'SamplerCustom',
                    'SamplerCustomAdvanced'):
        raise RuntimeError('这个工作流里找不到 KSampler —— 它多半不是文生图工作流'
                           '（可能是放大、打标、视频或 TTS 流），没法用来做 LoRA 扫档。')
    if not [1 for _i, n in _wf_find(wf, 'SaveImage')
            if str(n.get('class_type')) == 'SaveImage']:
        raise RuntimeError('这个工作流里没有 SaveImage 节点，出了图也取不回来，'
                           '没法做扫档对比。请在 ComfyUI 里加一个「保存图像」再导出。')


def build_workflow_for(cfg, spec, lora, strength, prompt_text, prefix, seed,
                       job=None, extra=None):
    """出提交体。按用户选的工作流走：引擎生成 或 某个 json 文件。

    extra：V0.5.1 的 LoRA 组合链（2 个以上、各自带权重）。
    """
    sel = None
    if job is not None and getattr(job, 'workflow', None):
        sel = job.workflow
    if not sel:
        sel = (spec or {}).get('workflow') or None
    kind = str((sel or {}).get('kind') or 'engine')

    if kind != 'file':
        wf = build_workflow(cfg, spec, lora, strength, prompt_text, prefix, seed,
                            extra=extra)
        if job is not None:
            _wf_note(job, [])
        return wf

    path = resolve_workflow_path((sel or {}).get('path') or '')
    # 转换很贵（要核对 16 MB 的节点表、把界面格式翻一遍），所以只在第一格做，
    # 之后每格从模板深拷贝一份再打补丁 —— 不然 100 个格子就转 100 遍。
    tpl = getattr(job, 'wf_template', None) if job is not None else None
    warns = list(getattr(job, 'wf_loaded', [])) if job is not None else []
    if tpl is None:
        wf, _fmt, load_warns = load_workflow_file(path, cfg)
        _wf_apply_placeholders(wf, cfg)
        warns = list(load_warns)
        if job is not None:
            job.wf_template = copy.deepcopy(wf)
            job.wf_loaded = list(warns)
    else:
        wf = copy.deepcopy(tpl)
    notes, pw = patch_workflow(wf, cfg, spec, lora, strength, prompt_text,
                               prefix, seed, extra=extra)
    if job is not None:
        _wf_note(job, warns + pw, notes)
    return {'prompt': wf, 'client_id': CLIENT_ID}


def _wf_note(job, warns, notes=None):
    """把工作流的告警攒在 job 上，跑完一次性展示（每个格子重复报没意义）。"""
    try:
        for w in warns or []:
            if w not in job.wf_warnings:
                job.wf_warnings.append(w)
        if notes:
            job.wf_notes = list(notes)
    except Exception:
        pass


def resolve_workflow_path(path):
    """只允许用内置 workflows/ 或 ComfyUI 工作流目录里的文件。"""
    if not path:
        raise RuntimeError('没有指定工作流文件')
    full = os.path.normpath(os.path.abspath(str(path)))
    allowed = [os.path.normpath(os.path.abspath(WORKFLOWS_DIR))]
    allowed += [os.path.normpath(os.path.abspath(d)) for d in workflow_dirs()]
    for root in allowed:
        try:
            if os.path.commonpath([full, root]) == root:
                break
        except ValueError:
            continue
    else:
        raise RuntimeError('这个路径不在允许的工作流目录里：%s' % path)
    if not os.path.isfile(full):
        raise RuntimeError('找不到工作流文件：%s' % path)
    return full


def workflow_dirs(cfg=None):
    """用户本地的工作流目录（ComfyUI 的 user/default/workflows）。"""
    cfg = cfg or load_config()
    root = (cfg.get('comfy_root') or '').strip()
    out = []
    if not root:
        return out
    for rel in (('ComfyUI', 'user', 'default', 'workflows'),      # 便携版根目录
                ('user', 'default', 'workflows'),                  # 源码版 ComfyUI 目录
                ('user', 'workflows')):
        d = os.path.normpath(os.path.join(root, *rel))
        if os.path.isdir(d) and d not in out:
            out.append(d)
    return out


def list_workflows(cfg=None):
    """内置 + 本地的工作流清单，给界面上那个下拉框用。"""
    cfg = cfg or load_config()
    bundled = [{
        'id': 'engine', 'kind': 'engine', 'format': 'api',
        'name': '引擎生成',
        'note': '不用挑，本工具按当前模型设置现场搭一张 Krea2 扫档图 —— 最稳，推荐',
        'path': '', 'where': 'builtin',
    }]
    if os.path.isdir(WORKFLOWS_DIR):
        for fn in sorted(os.listdir(WORKFLOWS_DIR)):
            if not fn.lower().endswith('.json'):
                continue
            bundled.append({
                'id': 'bf:' + fn, 'kind': 'file', 'format': 'api',
                'name': os.path.splitext(fn)[0],
                'note': '随工具自带（模型名按本机设置自动填）',
                'path': os.path.join(WORKFLOWS_DIR, fn), 'where': 'builtin',
            })

    local, dirs, n = [], workflow_dirs(cfg), 0
    for d in dirs:
        for root, _subs, files in os.walk(d):
            for fn in sorted(files):
                if not fn.lower().endswith('.json'):
                    continue
                n += 1
                if n > 400:
                    break
                p = os.path.join(root, fn)
                try:
                    if os.path.getsize(p) > 12 * 1024 * 1024:
                        n += 1
                        continue          # 工作流不可能这么大，别为了判格式去读它
                except OSError:
                    continue
                raw = load_json(p, None)
                fmt = 'api' if _wf_is_api(raw) else 'ui'
                rel = os.path.relpath(p, d).replace('\\', '/')
                local.append({
                    'id': 'lf:' + p, 'kind': 'file', 'format': fmt,
                    'name': rel, 'note': '你导出的工作流',
                    'path': p, 'where': 'local',
                    'bad': fmt == 'ui',
                })
    return {'bundled': bundled, 'local': local, 'dirs': dirs,
            'workflows_dir': WORKFLOWS_DIR}


def _safe_workflows(cfg=None):
    """扫工作流永远不能把启动数据带崩 —— 用户本地那个目录里可能有
    半截文件、超大文件、根本不是 json 的东西。"""
    try:
        return list_workflows(cfg)
    except Exception as e:
        bug('扫描工作流目录')
        return {'bundled': [], 'local': [], 'dirs': [], 'error': str(e),
                'workflows_dir': WORKFLOWS_DIR}


def describe_workflow(sel):
    """给报告/日志用的一句话描述。"""
    sel = sel or {}
    if str(sel.get('kind') or 'engine') != 'file':
        return '引擎生成（内置 Krea2 扫档图）'
    p = str(sel.get('path') or '')
    return '工作流文件：%s' % (os.path.basename(p) or p)


class JobStopped(Exception):
    pass


def _queue_ids(cfg):
    try:
        q = cget(cfg['comfy_url'] + '/queue', timeout=10)
    except Exception:
        return None
    ids = set()
    for x in q.get('queue_running', []) or []:
        ids.add(x[1])
    for x in q.get('queue_pending', []) or []:
        ids.add(x[1])
    return ids


def wait_job(cfg, pid, job, hard_timeout=1200):
    """等待任务完成。返回 (entry, error)。会检测任务丢失，避免无限死等。"""
    t0 = time.time()
    last_probe = 0.0
    missing_since = None
    while True:
        if job.stop_requested:
            raise JobStopped()
        elapsed = time.time() - t0
        if elapsed > hard_timeout:
            return None, '超时 %.0f 秒未完成' % elapsed
        time.sleep(1.0)
        try:
            h = cget(cfg['comfy_url'] + '/history/' + pid, timeout=15)
        except Exception:
            h = {}
        if pid in h:
            return h[pid], None
        # 每 5 秒探一次队列：既不在历史也不在队列 => 任务已丢失
        if time.time() - last_probe > 5:
            last_probe = time.time()
            ids = _queue_ids(cfg)
            if ids is not None and pid not in ids:
                if missing_since is None:
                    missing_since = time.time()
                elif time.time() - missing_since > 20:
                    return None, '任务从队列中丢失（ComfyUI 可能被重启或中断）'
            else:
                missing_since = None


def outputs_of(entry):
    res = []
    for _nid, out in (entry.get('outputs') or {}).items():
        for img in (out.get('images') or []):
            if img.get('type') == 'output':
                res.append(img)
    return res


def local_output_path(cfg, img):
    sub = img.get('subfolder') or ''
    return os.path.join(cfg['comfy_output'], sub, img['filename'])


def _take_over(src, dest):
    """把 ComfyUI 刚写出的那张图「认领」成我们自己的格子文件。

    V0.5 起 dest 与 src 在同一个目录里（都指向 output/HX验丹炉/<run_id>/），
    所以优先 os.replace **改名**：一个格子只留一个文件，不会出现
    「ComfyUI 那份 + 工具那份」两张同画面的图。
    跨盘、被占用等改名失败的情况退回复制（最坏也就是回到老行为，不会丢图）。
    """
    try:
        if os.path.exists(dest):
            os.remove(src)
        else:
            os.replace(src, dest)
        return dest, None
    except OSError:
        pass
    try:
        if not os.path.exists(dest):
            shutil.copy2(src, dest)
        return dest, None
    except OSError as e:
        return None, '取图失败：无法写入 %s（%s）' % (dest, e)


def fetch_image(cfg, img, dest):
    """把 ComfyUI 产出的图取到本地 dest。

    两条路，按顺序试：
      1) 本地 output 目录直读（最快，但要求 comfy_output 配对了、且工具与 ComfyUI 同机）
      2) ComfyUI 的 /view 接口（只要 comfy_url 通就行，不依赖任何本地路径）
    所以就算 comfy_output 是空的或填错了，只要 ComfyUI 在跑，图一样能拿到。
    """
    out_root = (cfg.get('comfy_output') or '').strip()
    if out_root:
        src = local_output_path(cfg, img)
        if os.path.exists(src):
            return _take_over(src, dest)
        local_err = '本地未找到 %s' % src
    else:
        local_err = 'comfy_output 未配置'

    try:
        q = urlencode({'filename': img['filename'],
                       'subfolder': img.get('subfolder') or '',
                       'type': img.get('type') or 'output'})
        req = urllib.request.Request(cfg['comfy_url'] + '/view?' + q)
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read()
        if not data:
            return None, '取图失败：ComfyUI /view 返回空数据（%s）' % local_err
        with open(dest, 'wb') as f:
            f.write(data)
        return dest, None
    except Exception as e:
        return None, '取图失败：%s；本地路径也不可用（%s）' % (e, local_err)


# ================================================================== 扫描任务
class ScanJob:
    def __init__(self, spec):
        self.spec = spec
        self.run_id = spec['run_id']
        self.status = 'running'      # running | done | stopped | error
        self.cells = []
        self.total = 0
        self.done = 0
        self.phase = ''
        self.message = '准备中…'
        self.started = time.time()
        self.finished = None
        self.stop_requested = False
        self.best = {}
        self.summaries = {}
        self.note = ''
        self.sheet = None
        self.report = None
        # V0.5：产物目录（run_dir / images）在 execute_scan 里定下来，
        # 因为要先问过 config 才知道该放 ComfyUI output 下还是退回 runs/。
        self.out_root = RUNS_DIR
        self.img_dir = ''
        # 用户选的工作流（见 list_workflows）；None / engine = 用引擎现搭的那张
        self.workflow = (spec.get('workflow') or None)
        # 自定义工作流时攒下来的告警 / 改动摘要，跑完一次性展示
        self.wf_warnings = []
        self.wf_notes = []
        self.wf_template = None     # 转好格式、还没打补丁的那份工作流
        self.wf_loaded = []
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            return {
                'run_id': self.run_id,
                'status': self.status,
                'total': self.total,
                'done': self.done,
                'phase': self.phase,
                'message': self.message,
                'elapsed': round(time.time() - self.started, 1),
                'stop_requested': self.stop_requested,
                'best': self.best,
                'summaries': self.summaries,
                'note': self.note,
                'workflow': describe_workflow(self.workflow),
                'wf_notes': list(self.wf_notes),
                'wf_warnings': list(self.wf_warnings),
                'cells': [dict(c) for c in self.cells],
                'spec': self.spec,
                # V0.5.1：把分组和它们的 key 一并给出。前端要靠 key 去
                # best / summaries 里取结论 —— 让前端自己重算一遍 key
                # （slug 规则、组合编号）迟早会两边不一致，那是很难查的 bug。
                'groups': [{'i': g['i'], 'key': g['key'], 'lora': g['lora'],
                            'short': g['short'], 'is_combo': g['is_combo'],
                            'ci': g['ci'], 'strengths': g['strengths'],
                            'detail': g.get('detail') or ''}
                           for g in scan_groups(self.spec)],
            }

    def touch(self, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(self, k, v)


JOB = None
JOB_LOCK = threading.Lock()


def slug(s, n=40):
    s = os.path.splitext(os.path.basename(s))[0]
    s = re.sub(r'[\\/:*?"<>|\s]+', '_', s).strip('_')
    return (s[:n] or 'lora')


def short_name(name, n=44):
    """给人看的 LoRA 短名。超长时保留头尾 —— 同一系列的 LoRA 常只有结尾不同
    （rank128 / rank32 / v2 / fp8），单纯截断会让它们长得一模一样。"""
    s = slug(name, 9999)
    if len(s) <= n:
        return s
    return s[:n - 14] + '…' + s[-12:]


def interrupted(cfg):
    """中断 ComfyUI 当前任务并清空队列。"""
    try:
        cpost(cfg['comfy_url'] + '/interrupt', {}, timeout=10)
    except Exception:
        pass
    try:
        cpost(cfg['comfy_url'] + '/queue', {'clear': True}, timeout=10)
    except Exception:
        pass


def execute_scan(job):
    cfg = load_config()
    spec = job.spec
    # V0.5：产物统一落在 ComfyUI output/HX验丹炉/<run_id>/（见 runs_root 的说明）。
    # 出图的 filename_prefix 也指向这里，于是 ComfyUI 写出来的那张图
    # 就是最终要留下的那张，不需要再抄一份。
    out_root = runs_root(cfg)
    run_dir = os.path.join(out_root, job.run_id)
    img_dir = os.path.join(run_dir, 'images')
    os.makedirs(img_dir, exist_ok=True)
    job.out_root = out_root
    job.img_dir = img_dir

    specs_groups = scan_groups(spec)         # V0.5.1：普通组 + LoRA 组合组
    strengths = [float(s) for s in spec['strengths']]
    prompts = spec['prompts']
    loras = spec['loras']
    fixed = norm_fixed(spec)
    seed = int(spec['seed'])
    ts = datetime.now().strftime('%Y%m%d-%H%M%S')

    # ---- 构建格子清单（行=prompt，列=强度；每行第一列是无 LoRA 基线）
    # V0.5.1：分组由 scan_groups 统一给出，普通组 = 一个对比 LoRA 扫各档，
    # 组合组 = 一次 LoRA 组合（只有一列，没有强度轴）。
    plan = []          # [(组, prompt_i, strength, dest, label, extra)]
    for g in specs_groups:
        for pi, pr in enumerate(prompts):
            plan.append((g, pi, 0.0,
                         os.path.join(img_dir, scan_cell_file(g, pi, 0.0)),
                         '基线', None))
            for s in (g['strengths'] if not g['is_combo'] else [COMBO_STRENGTH]):
                if s == 0:
                    continue
                plan.append((g, pi, s,
                             os.path.join(img_dir, scan_cell_file(g, pi, s)),
                             '组合' if g['is_combo'] else '%g' % s,
                             g['items']))

    job.touch(total=len(plan),
              message='共 %d 张待生成%s' % (
                  len(plan),
                  '（固定：%s）' % fixed_label(fixed) if fixed else '（无固定 LoRA）'))

    # 基线对所有 LoRA 相同 -> 只算一次，后面复用
    baseline_cache = {}

    for idx, (g, pi, s, dest, label, extra) in enumerate(plan):
        li, lora = g['i'], g['lora']
        if job.stop_requested:
            job.status = 'stopped'
            break
        # 触发词：基线格子（s=0）不带被对比 LoRA 的触发词，否则差异不干净
        text = compose_prompt(spec, None if s == 0 else lora,
                              prompts[pi]['text'],
                              also=(extra_names(g) if s != 0 else None))
        cell = {
            'lora': lora or '',
            'lora_short': (slug(lora) if lora else '基线'),
            'lora_i': li,
            'prompt_i': pi,
            'prompt_label': prompts[pi]['label'],
            'strength': s,
            'label': label,
            'fixed': fixed,
            'trigger': (norm_triggers(spec).get(lora) if (lora and s != 0) else '') or '',
            'prompt_used': text,
            'state': 'pending',
            'url': None,
            'metrics': None,
            'error': '',
        }
        if g['is_combo']:
            cell['combo'] = g['items']
            cell['combo_i'] = g['ci']
            cell['combo_detail'] = g.get('detail') or ''
        job.cells.append(cell)
        job.touch(phase='%d / %d' % (idx + 1, len(plan)),
                  message='%s · %s · %s' % (
                      slug(lora) if lora else '基线',
                      prompts[pi]['label'].replace('\n', ' '), label))

        try:
            ok, err = run_cell(job, cfg, cell, None if extra else lora, s, text,
                               dest, img_dir, ts, seed, extra=extra)
            if err:
                cell['state'] = 'failed'
                cell['error'] = err
            else:
                cell['state'] = 'done'
                base = baseline_cache.get(pi)
                if s == 0:
                    baseline_cache[pi] = dest
                    base = dest
                cell['metrics'] = analyze(dest, base)
                cell['url'] = '/files/%s/images/%s' % (
                    quote(job.run_id), quote(os.path.basename(dest)))
        except JobStopped:
            cell['state'] = 'failed'
            cell['error'] = '已停止'
            job.status = 'stopped'
            job.touch(done=idx + 1)
            break
        job.touch(done=idx + 1)

    if job.status == 'running':
        job.status = 'done'
    job.finished = time.time()

    # 自定义工作流时攒下来的告警：跑完一次性说出来，比每张图重复报有用
    if job.wf_warnings:
        job.note = (job.note + ' · ' if job.note else '') + \
            '工作流提示：' + '；'.join(job.wf_warnings[:3])

    # ---- 评分与推荐
    job.touch(message='计算评分与推荐档位…')
    for g in specs_groups:
        group = [c for c in job.cells
                 if c['lora_i'] == g['i'] and c['state'] == 'done']
        best = score_run(group, g['strengths'])
        if best is not None:
            job.best[g['key']] = best
        summ = summarize_lora(group, is_combo=g['is_combo'])
        if summ:
            if g['is_combo']:
                summ['detail'] = g.get('detail') or ''
            job.summaries[g['key']] = summ

    # ---- 出图
    try:
        build_outputs(cfg, job, run_dir, img_dir, strengths, prompts, loras)
    except Exception as e:
        bug('生成对比图/报告')
        job.note = '生成对比图/报告时出错: %s' % e

    # ---- 存历史
    try:
        save_json(os.path.join(run_dir, 'run.json'), {
            'run_id': job.run_id,
            'created': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'status': job.status,
            'spec': spec,
            'best': job.best,
            'summaries': job.summaries,
            'cells': job.cells,
            'duration': round((job.finished or time.time()) - job.started, 1),
            # V0.5：记下产物目录。默认就是本条记录自己所在的那个目录，
            # 但重建报告时要能一眼看出图到底放在哪（尤其是老记录）。
            'img_dir': os.path.relpath(img_dir, run_dir).replace('\\', '/'),
            'out_root': out_root,
        })
    except Exception:
        bug('保存本次结果到历史')

    # 开了「跑完自动关闭」的话，关掉本次由本工具启动的那个 ComfyUI
    try:
        if load_config().get('comfy_autostop'):
            with COMFY_LOCK:
                mine = bool(COMFY['by_us'] and COMFY['pid'])
            if mine:
                comfy_stop()
                job.note = (job.note + ' · ' if job.note else '') + \
                    '已自动关闭本次启动的 ComfyUI'
    except Exception:
        bug('自动关闭 ComfyUI')

    job.touch(message='完成' if job.status == 'done' else job.message)


def run_cell(job, cfg, cell, lora, strength, prompt_text, dest, img_dir, ts, seed,
             extra=None):
    if os.path.exists(dest):
        cell['state'] = 'done'
        return dest, None
    cell['state'] = 'running'
    # V0.5：让 ComfyUI 直接把图写进这次运行的产物目录（output/HX验丹炉/<run_id>/），
    # 文件名前缀带上格子名，取回来时只是「同目录改名」，全程只有一份文件。
    prefix = '%s/%s/%s' % (RUNS_SUBDIR, job.run_id,
                           os.path.splitext(os.path.basename(dest))[0])
    try:
        wf = build_workflow_for(cfg, job.spec, lora, strength, prompt_text,
                                prefix, seed, job=job, extra=extra)
    except Exception as e:
        return None, '工作流准备失败: %s' % e
    try:
        pid = cpost(cfg['comfy_url'] + '/prompt', wf)['prompt_id']
    except Exception as e:
        return None, '提交失败: %s' % e
    entry, err = wait_job(cfg, pid, job)
    if err:
        return None, err
    imgs = outputs_of(entry)
    if not imgs:
        st = (entry.get('status') or {}).get('status_str', '?')
        return None, '未产出图片 (%s)' % st
    return fetch_image(cfg, imgs[0], dest)


# ============================================================ 对比图 / 报告
def build_outputs(cfg, job, run_dir, img_dir, strengths, prompts, loras):
    """每个「组」一张 contact sheet + 一份 HTML 报告。

    V0.5.1：组由 scan_groups 统一给出（普通组 / 组合组），不再自己按 loras 循环。
    组合组的列是 [基线 | 组合]，只有一列变量 —— 它没有强度轴，画不出曲线，
    报告里也按「组合」而不是「档位」来称呼。
    """
    fixed = norm_fixed(job.spec)
    trig = norm_triggers(job.spec)
    # 有固定组时，基线 = 固定组（不含变量 LoRA），列名要说清楚
    base_col = '仅固定' if fixed else '基线'
    groups = scan_groups(job.spec)
    report_sections = []
    all_cols = [base_col]

    for g in groups:
        is_combo = g['is_combo']
        var_cols = [('组合' if is_combo else ('%g' % s), s)
                    for s in (g['strengths'] if is_combo else
                              [s for s in g['strengths'] if s != 0])]
        cols = [(base_col, 0.0)] + var_cols
        col_vals = [c[1] for c in cols]
        col_labels = [c[0] for c in cols]
        for lb in col_labels[1:]:
            if lb not in all_cols:
                all_cols.append(lb)

        matrix, rows = [], []
        for pi, pr in enumerate(prompts):
            row = []
            for _lab, s in cols:
                p = os.path.join(img_dir, scan_cell_file(g, pi, s))
                row.append(p if os.path.exists(p) else None)
            matrix.append(row)
            rows.append(pr['label'].replace('\n', ' '))

        if not any(any(r) for r in matrix):
            continue
        sheet = os.path.join(run_dir, 'sheet_G%d.png' % g['i'])
        sheet_sub = ('固定：%s · seed %s · %d×%d · %s steps · CFG %s'
                     % (fixed_label(fixed), job.spec['seed'], job.spec['width'],
                        job.spec['height'], job.spec['steps'], job.spec['cfg']))
        if is_combo:
            sheet_sub = '组合：%s · %s' % (g.get('detail') or '', sheet_sub)
        if trig.get(g['lora']):
            sheet_sub = '触发词：%s · %s' % (trig[g['lora']], sheet_sub)
        make_sheet(matrix, rows, col_labels, sheet,
                   ('Krea2 LoRA 组合 · %s' if is_combo else 'Krea2 强度扫描 · %s')
                   % short_name(g['lora']), sheet_sub)
        if not job.sheet:
            job.sheet = sheet
        report_sections.append({
            'lora': g['lora'],
            'short': g['short'],
            'fixed': fixed_label(fixed),
            'base_col': base_col,
            'sheet': os.path.basename(sheet),
            'cols': col_labels,
            # 每一列对应的数值（组合列写死 1.0）：报告里既要知道列名，
            # 也要能用数值把格子跟 cells 对上，两者不能混用一个数组
            'col_vals': col_vals,
            'is_combo': is_combo,
            'combo_detail': g.get('detail') or '',
            'rows': rows,
            # 相对路径：报告既能在工具里打开（/files/...），也能直接双击文件看
            'grid': [[('images/' + os.path.basename(p)) if p else None for p in r]
                     for r in matrix],
            'cells': [c for c in job.cells if c['lora_i'] == g['i']],
            'best': job.best.get(g['key']),
            'summary': job.summaries.get(g['key']),
            'trigger': trig.get(g['lora'], ''),
        })

    report_path = os.path.join(run_dir, 'report.html')
    make_report(job, report_path, report_sections, all_cols)
    job.report = report_path


def make_sheet(matrix, row_labels, col_labels, out_path, title, subtitle, cell=460):
    require_pillow()
    gap, left_w, top_h, head_h = 10, 190, 96, 44
    n_rows = len(matrix)
    n_cols = len(matrix[0]) if n_rows else 0
    W = left_w + n_cols * cell + (n_cols + 1) * gap
    H = top_h + head_h + n_rows * (cell + gap) + gap
    canvas = Image.new('RGB', (W, H), (247, 247, 249))
    d = ImageDraw.Draw(canvas)

    d.text((gap + 12, 14), title, font=load_font(32), fill=(22, 22, 28), anchor='lt')
    d.text((gap + 12, 58), subtitle, font=load_font(18), fill=(108, 108, 120), anchor='lt')
    for j, cl in enumerate(col_labels):
        x = left_w + gap + j * (cell + gap)
        d.text((x + cell // 2, top_h + head_h // 2), cl, font=load_font(26),
               fill=(38, 38, 46), anchor='mm')
    for i, row in enumerate(matrix):
        y = top_h + head_h + i * (cell + gap)
        d.text((gap + 12, y + cell // 2), row_labels[i], font=load_font(20),
               fill=(38, 38, 46), anchor='lm')
        for j, p in enumerate(row):
            x = left_w + gap + j * (cell + gap)
            if p and os.path.exists(p):
                canvas.paste(Image.open(p).convert('RGB').resize(
                    (cell, cell), Image.Resampling.LANCZOS), (x, y))
                d.rectangle([x + cell - 96, y + cell - 40, x + cell, y + cell],
                            fill=(0, 0, 0))
                d.text((x + cell - 12, y + cell - 9), col_labels[j],
                       font=load_font(24), fill=(255, 255, 255), anchor='rd')
            else:
                d.rectangle([x, y, x + cell, y + cell], fill=(233, 233, 237),
                            outline=(206, 206, 212))
                d.text((x + cell // 2, y + cell // 2), '未生成',
                       font=load_font(20), fill=(168, 78, 78), anchor='mm')
    canvas.save(out_path)


def _pct(v):
    """1.0 -> '+0%'，0.94 -> '-6%'。"""
    return '%+.0f%%' % ((float(v) - 1.0) * 100)


def _metric_html(c, is_base):
    """图片下面那行小字：这张图到底比基线好还是差。"""
    m = (c or {}).get('metrics') or {}
    if not m:
        return ''
    bits = []
    if is_base:
        bits.append('<span class="k">锐</span>%s' % m.get('sharpness', '—'))
        bits.append('<span class="k">熵</span>%s' % m.get('entropy', '—'))
    else:
        sr = (c or {}).get('sharp_ratio')
        d = ''
        if sr is not None:
            cls = 'up' if sr >= 1.02 else ('down' if sr < 0.95 else '')
            d = '<span class="d %s">%s</span>' % (cls, _pct(sr))
        bits.append('<span class="k">锐</span>%s%s' % (m.get('sharpness', '—'), d))
        bits.append('<span class="k">熵</span>%s' % m.get('entropy', '—'))
        if m.get('drift') is not None:
            bits.append('<span class="k">偏移</span>%s' % m['drift'])
        if (c or {}).get('score') is not None:
            bits.append('<span class="k">分</span><b>%s</b>' % c['score'])
    if (c or {}).get('recommended'):
        bits.append('<span class="star">★ 推荐</span>')
    if (c or {}).get('degraded'):
        bits.append('<span class="crash">崩坏</span>')
    return '<div class="mt">%s</div>' % ' '.join(bits)


def curve_svg(curve, w=250, h=112):
    """强度-分数曲线：实线=综合分，虚线=风格偏移（判断 LoRA 有没有真起作用）。"""
    if not curve:
        return ''
    pl, pr, pt, pb = 28, 10, 12, 20
    iw, ih = w - pl - pr, h - pt - pb
    xs = [c['strength'] for c in curve]
    lo, hi = min(xs), max(xs)
    span = (hi - lo) or 1.0

    def X(s):
        return pl + (s - lo) / span * iw

    def Y(v):
        v = max(0.0, min(1.0, float(v or 0.0)))
        return pt + (1 - v) * ih

    out = ['<svg class="curve" viewBox="0 0 %d %d" style="width:100%%;height:auto">'
           % (w, h)]
    for v in (0.0, 0.5, 1.0):
        y = Y(v)
        out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#ececf1" '
                   'stroke-width="1"/>' % (pl, y, pl + iw, y))
        out.append('<text x="%g" y="%g" font-size="9" fill="#a8a8b2" '
                   'text-anchor="end">%g</text>' % (pl - 4, y + 3, v))
    out.append('<polyline fill="none" stroke="#f0b429" stroke-width="1.6" '
               'stroke-dasharray="4 3" points="%s"/>'
               % ' '.join('%g,%g' % (X(c['strength']), Y(c['drift'])) for c in curve))
    pts = [(X(c['strength']), Y(c['score'])) for c in curve if c['score'] is not None]
    if len(pts) > 1:
        out.append('<polyline fill="none" stroke="#4f46e5" stroke-width="2.2" '
                   'points="%s"/>' % ' '.join('%g,%g' % p for p in pts))
    for c in curve:
        if c['score'] is None:
            continue
        x, y = X(c['strength']), Y(c['score'])
        if c.get('crashed'):
            out.append('<circle cx="%g" cy="%g" r="4" fill="#fff" stroke="#dc2626" '
                       'stroke-width="2"/>' % (x, y))
        else:
            out.append('<circle cx="%g" cy="%g" r="3.4" fill="#4f46e5"/>' % (x, y))
    for c in curve:
        out.append('<text x="%g" y="%g" font-size="10" fill="#6b6b76" '
                   'text-anchor="middle">%g</text>'
                   % (X(c['strength']), h - 5, c['strength']))
    out.append('</svg>')
    return ''.join(out)


REPORT_CSS = """
:root{color-scheme:light}
*{box-sizing:border-box}
body{margin:0;padding:26px 22px 64px;background:#f4f5f7;color:#1d1d22;
     font:14px/1.6 "Microsoft YaHei",-apple-system,"Segoe UI",sans-serif}
.wrapage{max-width:1600px;margin:0 auto}
h1{font-size:22px;margin:0 0 7px}
h2{font-size:16px;margin:0 0 12px}
.sub{color:#6b6b76;font-size:13px}
.sub2{color:#4f46e5;font-size:13px;margin-top:4px}
.sub3{color:#6b6b76;font-size:12.5px;margin-top:3px}
.hint{color:#8a8a94;font-size:12px;margin:12px 0 26px;line-height:1.75}
section{background:#fff;border:1px solid #e3e4e8;border-radius:12px;
        padding:16px 18px 20px;margin-bottom:20px}
ul.verdicts{list-style:none;margin:0;padding:0;display:grid;gap:10px}
li.vv{border-left:4px solid #cbd5e1;background:#fafbfc;border-radius:0 10px 10px 0;
      padding:11px 14px}
li.vv-ok{border-left-color:#16a34a;background:#f6fdf8}
li.vv-warn{border-left-color:#d97706;background:#fffcf5}
li.vv-bad{border-left-color:#dc2626;background:#fff7f7}
.vhead{display:flex;align-items:center;gap:9px;margin-bottom:4px}
.vhead b{font-size:14px;word-break:break-all}
li.vv p{margin:0;font-size:13px;color:#4a4a55;line-height:1.65}
.pill{font-size:11.5px;padding:2px 9px;border-radius:20px;font-weight:600;color:#fff;
      flex:0 0 auto}
.p-ok{background:#16a34a}.p-warn{background:#d97706}.p-bad{background:#dc2626}
.cdeck{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(440px,1fr))}
.card{display:flex;gap:14px;background:#fafbfc;border:1px solid #e8e9ee;
      border-radius:11px;padding:13px;margin:0}
.cthumb{flex:0 0 168px}
.cthumb img{width:168px;height:168px;object-fit:cover;border-radius:9px;display:block;
      background:#eee;box-shadow:0 1px 4px rgba(0,0,0,.1)}
.cthumb .noimg{width:168px;height:168px;border-radius:9px;background:#f0f0f4;
      display:flex;align-items:center;justify-content:center;color:#b0b0ba;font-size:12.5px}
.cbody{flex:1;min-width:0}
.cname{font-size:14px;font-weight:700;word-break:break-all;line-height:1.4}
.cbest{font-size:12.5px;color:#4f46e5;margin:4px 0 7px}
.cbest b{font-size:16px}
.ctags{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:8px}
.tg{font-size:11.5px;padding:2px 8px;border-radius:6px;background:#f0f1f5;color:#55555f}
.tg.ok{background:#e9f9ef;color:#15803d}
.tg.warn{background:#fdf5e3;color:#a16207}
.tg.bad{background:#fdecec;color:#b91c1c}
.tg.trig{background:#eef0fe;color:#3730a3}
/* V0.4 品质等级：彩色字 + 同色系浅底。颜色定义在 lora_grade.GRADES 里，
   这里只管形状，避免两处各写一套颜色改起来对不上。 */
.qgrade{display:inline-block;font-size:11.5px;font-weight:700;padding:2px 9px;
    border-radius:999px;letter-spacing:.5px;white-space:nowrap;cursor:help}
.qgrade.qbig{font-size:13px;padding:3px 12px}
.qlegend{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:0 0 14px;
    padding:10px 13px;background:#fafbfc;border:1px solid #e8e9ee;border-radius:10px}
.qlegend .t{font-size:12px;color:#6b6b76;margin-right:2px}
.qlegend .d{font-size:11.5px;color:#8b8b95;margin-left:2px}
.curve{display:block;margin-top:4px;width:100%;height:auto}
.cnote{font-size:12px;color:#6b6b76;margin-top:6px;line-height:1.6}
.wrap{overflow-x:auto;margin-bottom:14px}
table.grid{border-collapse:separate;border-spacing:8px}
table.grid th{font-weight:600;font-size:12.5px;color:#3a3a44}
table.grid th.rowh{width:110px;text-align:left;vertical-align:middle;font-weight:500}
table.grid td{position:relative;padding:0;vertical-align:top;width:236px}
table.grid td img{width:236px;height:236px;object-fit:cover;display:block;
      border-radius:8px;background:#eee;box-shadow:0 1px 3px rgba(0,0,0,.09);
      transition:transform .12s}
table.grid td a:hover img{transform:scale(1.03);box-shadow:0 6px 18px rgba(0,0,0,.16)}
table.grid td.rec img{outline:2px solid #d97706;outline-offset:1px}
table.grid td.miss .ph{width:236px;height:236px;border-radius:8px;background:#ececf0;
      display:flex;align-items:center;justify-content:center;color:#b05a5a;font-size:12.5px}
.mt{font-size:11.5px;color:#78787f;margin-top:5px;line-height:1.6;
      font-family:ui-monospace,Consolas,monospace}
.mt .k{color:#a8a8b2;margin-right:2px}
.mt b{color:#1d1d22}
.mt .d{margin-left:3px}
.mt .d.up{color:#15803d}
.mt .d.down{color:#b91c1c}
.mt .star{color:#d97706;font-weight:700}
.mt .crash{color:#fff;background:#dc2626;border-radius:4px;padding:0 5px;font-size:10.5px;
      font-family:"Microsoft YaHei",sans-serif}
.fixedline{font-size:12.5px;color:#6b6b76;margin-bottom:5px;line-height:1.75}
.fixedline code{background:#f0f1f5;padding:1px 6px;border-radius:5px;color:#3730a3;
      font-family:ui-monospace,Consolas,monospace}
.bestline{font-size:13px;color:#4f46e5;margin-bottom:12px}
.bestline b{font-size:15px}
.acts{display:flex;gap:9px;flex-wrap:wrap}
.btn{display:inline-block;text-decoration:none;font-size:12.5px;padding:7px 14px;
      border:1px solid #dcdde3;border-radius:8px;color:#33333b;background:#f7f8fa}
.btn:hover{background:#eef0fe;border-color:#c7cbf7;color:#3730a3}
ul.pl{list-style:none;padding:0;display:grid;gap:10px;margin:0}
ul.pl li{background:#fafbfc;border:1px solid #e8e9ee;border-radius:10px;
      padding:12px 16px;font-size:13px}
ul.pl p{margin:6px 0 0;color:#55555f;line-height:1.65}

/* ================================================================ 大图对比
   V0.5 新增。做法照 ComfyUI 的 Image Comparer 节点：两张图绝对定位叠在一起，
   上面那张用 clip-path 裁一半，中间的滑杆决定裁到哪儿 —— 同一个像素位置
   左右一比，强度带来的差别（发丝、布料纹理、背景细节）就藏不住了。
   刻意做成深色灯箱：不管报告页本身是亮色还是深色，它看起来都是同一个东西。 */
a.gimg{cursor:zoom-in;display:block;position:relative}
a.gimg::after{content:'对比';position:absolute;right:6px;bottom:6px;
      font:11px/1 "Microsoft YaHei",sans-serif;color:#fff;background:rgba(20,18,32,.72);
      border-radius:5px;padding:3px 7px;opacity:0;transition:opacity .12s}
a.gimg:hover::after{opacity:1}

.cmpmask{display:none;position:fixed;inset:0;z-index:90;background:rgba(8,8,12,.94);
      -webkit-backdrop-filter:blur(3px);backdrop-filter:blur(3px);
      flex-direction:column;padding:14px 16px 10px;color:#e9e9f1}
.cmpmask.on{display:flex}
.cmpbar{display:flex;align-items:center;gap:14px;flex-wrap:wrap;
      padding:0 2px 10px;flex:0 0 auto}
.cmptitle{font-size:14px;font-weight:700;letter-spacing:.3px}
.cmptitle span{font-weight:400;color:#9a9aac;font-size:12.5px;margin-left:8px}
.cmpsel{display:flex;align-items:center;gap:7px;flex-wrap:wrap}
.cmpsel label{font-size:12px;color:#9a9aac}
.cmpsel select{background:#17171e;color:#e9e9f1;border:1px solid #33333f;
      border-radius:7px;padding:5px 8px;font:12.5px/1.4 "Microsoft YaHei",sans-serif;
      max-width:290px}
.cmpacts{margin-left:auto;display:flex;gap:7px;align-items:center}
.cbtn{font:12.5px/1 "Microsoft YaHei",sans-serif;padding:7px 12px;border-radius:7px;
      border:1px solid #33333f;background:#1a1a21;color:#c9c9d6;cursor:pointer;
      text-decoration:none;display:inline-block}
.cbtn:hover{background:#23232c;color:#fff}
.cbtn.on{border-color:#7c6cf5;color:#fff;background:#2a2440}

/* 居中用 margin:auto 而不是 justify-content:center ——
   放大到 1:1 后图比容器宽，justify-content 会把左边一截顶到滚不到的地方。 */
.cmpstage{flex:1 1 auto;overflow:auto;min-height:0;display:flex}
.cmpwrap{margin:auto;position:relative;line-height:0;background:#111116;
      box-shadow:0 0 0 1px #2a2a34,0 18px 60px rgba(0,0,0,.6)}
.cmpwrap img{display:block;width:100%;height:auto;user-select:none;-webkit-user-drag:none}
.cmpwrap #cmpImgA{position:relative}
.cmpb{position:absolute;inset:0;overflow:hidden;
      clip-path:inset(0 0 0 50%)}
.cmpb img{position:absolute;inset:0;width:100%;height:100%}
.cmpline{position:absolute;top:0;bottom:0;left:50%;width:0;
      border-left:1.5px solid rgba(255,255,255,.92);
      box-shadow:0 0 8px rgba(0,0,0,.7);pointer-events:none}
.cmpline i{position:absolute;top:50%;left:50%;width:30px;height:30px;
      margin:-15px 0 0 -15px;border-radius:50%;
      background:rgba(255,255,255,.94);box-shadow:0 2px 10px rgba(0,0,0,.6)}
.cmpline i::before,.cmpline i::after{content:'';position:absolute;top:50%;width:0;height:0;
      border:5px solid transparent;margin-top:-5px}
.cmpline i::before{left:5px;border-right-color:#2b2b36}
.cmpline i::after{right:5px;border-left-color:#2b2b36}
#cmpRange{position:absolute;inset:0;width:100%;height:100%;margin:0;
      opacity:0;cursor:ew-resize;appearance:none;-webkit-appearance:none;background:transparent}
#cmpRange:focus-visible{opacity:.001;outline:none}
.cmplab{position:absolute;top:8px;font:12px/1 "Microsoft YaHei",sans-serif;
      color:#fff;background:rgba(20,18,32,.78);border-radius:6px;padding:5px 9px;
      pointer-events:none;max-width:46%;overflow:hidden;text-overflow:ellipsis;
      white-space:nowrap}
.cmplabA{left:8px}
.cmplabB{right:8px;background:rgba(58,42,120,.85)}
.cmpspeak{position:absolute;bottom:8px;left:50%;transform:translateX(-50%);
      font:11.5px/1.5 ui-monospace,Consolas,monospace;color:#cfcfdd;
      background:rgba(20,18,32,.78);border-radius:6px;padding:4px 10px;pointer-events:none}
.cmphint{flex:0 0 auto;text-align:center;color:#77778a;font-size:11.5px;padding:9px 0 2px}
.cmphint kbd{background:#20202a;border:1px solid #34343f;border-radius:4px;
      padding:1px 5px;font-family:ui-monospace,Consolas,monospace;color:#c9c9d6}
/* 页面自己带滚动条时，别再让底下的报告跟着滚 */
body.cmpopen{overflow:hidden}
"""

# 报告页的暗色表。默认用 media="not all" 挂着（解析期就不生效，不会闪一下白底），
# 只有 URL 带 ?theme=dark 时才由下面那行小脚本打开。
#
# 为什么要让报告页自己带暗色、而不是靠桌面版注入：鉴定台是把报告**嵌在 iframe**
# 里看的，iframe 是独立文档，主进程往主窗口注入的暗色表进不去 —— 结果就是
# 深色界面上嵌一块白纸。带参数这条路子，独立打开报告页（分享 / 打印）时
# 仍然是干净的亮色，两不耽误。
REPORT_DARK_CSS = """
/* 深色覆盖：主体交给逐条差异，下面只补滚动条与几处观感细节。 */
:root{color-scheme:dark}
body{background:#0b0b0f !important;color:#e9e9f1}
.sub{color:#8a8a99}
.sub2{color:#8a8a99}
.hint{color:#b6b6c4}
section{background:#141419;border:1px solid #26262e}
li.vv{border-left:4px solid #262c33;background:#171a1c}
li.vv-ok{background:#18281c}
li.vv-warn{background:#322b19}
li.vv-bad{background:#321919}
li.vv p{color:#d1d1d7}
.pill{color:#ffffff}
.card{background:#171a1c;border:1px solid #27282e}
.cthumb img{background:#161616;box-shadow:0 1px 4px rgba(0,0,0,0.42)}
.cthumb .noimg{background:#17171a}
.cbest{color:#928df2}
.tg{background:#17181b;color:#d2d2d7}
.tg.ok{background:#18261d;color:#91f0b4}
.tg.warn{background:#2f2817;color:#f1c483}
.tg.bad{background:#2e1717;color:#f18f8f}
.tg.trig{background:#181b30;color:#a9a5e6}
.qlegend{background:#171a1c;border:1px solid #27282e}
.qlegend .t{color:#d3d3d7}
.qlegend .d{color:#d3d3d7}
.cnote{color:#d3d3d7}
table.grid th{color:#d0d0d7}
table.grid td img{background:#161616;box-shadow:0 1px 3px rgba(0,0,0,0.40)}
table.grid td a:hover img{box-shadow:0 6px 18px rgba(0,0,0,0.56)}
table.grid td.miss .ph{background:#161619;color:#dfb8b8}
.mt{color:#d4d4d7}
.mt b{color:#d0d0d7}
.mt .d.up{color:#91f0b4}
.mt .d.down{color:#f18f8f}
.mt .star{color:#f1bd81}
.mt .crash{color:#ffffff}
.fixedline{color:#d3d3d7}
.fixedline code{background:#17181b;color:#a9a5e6}
.bestline{color:#928df2}
.btn{border:1px solid #27282c;color:#d1d1d7;background:#17191c}
.btn:hover{background:#181b30;border-color:#222649;color:#a9a5e6}
ul.pl li{background:#171a1c;border:1px solid #27282e}
ul.pl p{color:#d2d2d7}
html{background:#0b0b0f}
*::-webkit-scrollbar{width:10px;height:10px}
*::-webkit-scrollbar-track{background:transparent}
*::-webkit-scrollbar-thumb{background:#2e2e38;border:2px solid transparent;
  background-clip:content-box;border-radius:99px}
*::-webkit-scrollbar-thumb:hover{background:#3d3d4a;background-clip:content-box}
::selection{background:rgba(124,108,245,.35);color:#fff}
table.grid td img{border-radius:8px}
.cthumb img,.cthumb .noimg{border-radius:8px}
h1{letter-spacing:.2px}
"""

REPORT_TPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__ · LoRA 扫档报告</title>
<style>__CSS__</style>
<style id="hx-report-dark" media="not all">__DARKCSS__</style>
<script>
/* 只有带 ?theme=dark 才点亮暗色表 —— 桌面版鉴定台用 iframe 嵌这份报告时会带上。
   独立打开（分享给别人、打印）时是默认的亮色，不受影响。 */
(function(){try{
  if (/[?&]theme=dark/.test(location.search || '')){
    var s = document.getElementById('hx-report-dark');
    if (s) s.media = 'all';
  }
}catch(e){}})();
</script>
</head><body>
<div class="wrapage">
__HEAD__
__HINT__
<section><h2>品质等级说明</h2><div class="qlegend">__QLEGEND__</div></section>
<section><h2>结论 · 哪个档能用</h2><ul class="verdicts">__VERDICT__</ul></section>
<section><h2>总览 · 一眼看优劣</h2><div class="cdeck">__CARDS__</div></section>
__DETAIL__
<section><h2>使用的提示词（固定 seed，所有格子完全相同）</h2>
<ul class="pl">__PROMPTS__</ul></section>
</div>

<div class="cmpmask" id="cmpmask">
  <div class="cmpbar">
    <div class="cmptitle">大图对比<span id="cmpWho"></span></div>
    <div class="cmpsel">
      <label>A</label><select id="cmpA"></select>
      <label>B</label><select id="cmpB"></select>
    </div>
    <div class="cmpacts">
      <button class="cbtn on" id="cmpFit" type="button">适配窗口</button>
      <button class="cbtn" id="cmpOne" type="button">1:1</button>
      <a class="cbtn" id="cmpRaw" target="_blank" rel="noopener">看 B 原图</a>
      <button class="cbtn" id="cmpClose" type="button">关闭</button>
    </div>
  </div>
  <div class="cmpstage" id="cmpstage">
    <div class="cmpwrap" id="cmpwrap">
      <img id="cmpImgA" alt="">
      <div class="cmpb" id="cmpBwrap"><img id="cmpImgB" alt=""></div>
      <div class="cmpline" id="cmpline"><i></i></div>
      <input type="range" id="cmpRange" min="0" max="1000" value="500" step="1"
             aria-label="对比位置">
      <span class="cmplab cmplabA" id="cmpLabA">A</span>
      <span class="cmplab cmplabB" id="cmpLabB">B</span>
      <span class="cmpspeak" id="cmpPeak"></span>
    </div>
  </div>
  <div class="cmphint">
    在图上左右拖动 / 拖中间的滑杆 / 焦点在滑杆时按 <kbd>←</kbd><kbd>→</kbd> 微调 ·
    <kbd>Esc</kbd> 关闭
  </div>
</div>

<script>
/* ============================================================================
 * 大图对比（V0.5）
 * 参考 ComfyUI 的 Image Comparer 节点：B 图叠在 A 图上面、按滑杆位置裁一刀，
 * 于是同一个像素坐标上可以直接看出两档的差别。
 * A 默认为该行的基线，B 为点开的那一格；两个下拉都能换，换到别的提示词行也行。
 * ========================================================================== */
(function () {
  'use strict';
  var DATA = __CMPDATA__;
  var mask = document.getElementById('cmpmask');
  if (!mask || !DATA || !DATA.length) return;
  var wrap = document.getElementById('cmpwrap');
  var stage = document.getElementById('cmpstage');
  var imgA = document.getElementById('cmpImgA');
  var imgB = document.getElementById('cmpImgB');
  var bwrap = document.getElementById('cmpBwrap');
  var line = document.getElementById('cmpline');
  var range = document.getElementById('cmpRange');
  var selA = document.getElementById('cmpA');
  var selB = document.getElementById('cmpB');
  var labA = document.getElementById('cmpLabA');
  var labB = document.getElementById('cmpLabB');
  var peak = document.getElementById('cmpPeak');
  var who = document.getElementById('cmpWho');
  var rawLink = document.getElementById('cmpRaw');
  var btnFit = document.getElementById('cmpFit');
  var btnOne = document.getElementById('cmpOne');
  var cur = null, pos = 0.5;

  /* 格子的显示名：0 档叫「基线」，其余直接写强度数字 */
  function label(cell) {
    var v = parseFloat(cell.s);
    if (isNaN(v)) return String(cell.s || '');
    return v === 0 ? '基线' : '强度 ' + String(cell.s);
  }

  function paint() {
    var p = Math.max(0, Math.min(1, pos));
    bwrap.style.clipPath = 'inset(0 0 0 ' + (p * 100) + '%)';
    line.style.left = (p * 100) + '%';
    range.value = Math.round(p * 1000);
    peak.textContent = 'B 占比 ' + Math.round(p * 100) + '%';
  }

  function show() {
    if (!cur) return;
    who.textContent = '· ' + cur.sec.name + ' · ' + (cur.row.label || '');
    selA.value = String(cur.a.i);
    selB.value = String(cur.b.i);
    labA.textContent = 'A · ' + label(cur.a);
    labB.textContent = 'B · ' + label(cur.b);
    rawLink.href = cur.b.u;
    imgA.src = cur.a.u;
    imgB.src = cur.b.u;
    if (imgA.complete) zoom(wrap.style.width !== '');
    paint();
  }

  function findCell(sec, key) {
    for (var r = 0; r < sec.rows.length; r++) {
      for (var c = 0; c < sec.rows[r].cells.length; c++) {
        if (sec.rows[r].cells[c].i === key) return { row: sec.rows[r], cell: sec.rows[r].cells[c] };
      }
    }
    return null;
  }

  function open(secI, rowI, cellI) {
    var sec = DATA[secI];
    if (!sec) return;
    var row = sec.rows[rowI];
    if (!row || !row.cells.length) return;

    var b = row.cells[0];
    for (var i = 0; i < row.cells.length; i++) {
      if (row.cells[i].i === cellI) b = row.cells[i];
    }
    /* A 默认是这一行的基线（0 档）；点开的正是基线时，B 换成这一行的第一档 ——
       「基线 vs 自己」看不出任何东西，换成最小强度才是有用的默认。 */
    var a = null;
    for (var j = 0; j < row.cells.length; j++) {
      if (parseFloat(row.cells[j].s) === 0) { a = row.cells[j]; break; }
    }
    if (!a) a = row.cells[0];
    if (a.i === b.i) {
      for (var k = 0; k < row.cells.length; k++) {
        if (row.cells[k].i !== a.i) { b = row.cells[k]; break; }
      }
    }

    cur = { sec: sec, secI: secI, row: row, a: a, b: b };
    fillSelects();
    show();
    mask.classList.add('on');
    document.body.classList.add('cmpopen');
    try { range.focus(); } catch (e) { /* ignore */ }
  }

  /* 两个下拉都列满这一节的所有格子，按提示词行分组。
     跨行比较也是允许的（比如「同强度、不同提示词」），所以不做限制。 */
  function fillSelects() {
    if (!cur) return;
    selA.innerHTML = ''; selB.innerHTML = '';
    for (var r = 0; r < cur.sec.rows.length; r++) {
      var row = cur.sec.rows[r];
      if (!row.cells.length) continue;
      var ga = document.createElement('optgroup');
      var gb = document.createElement('optgroup');
      ga.label = gb.label = row.label || ('提示词 ' + (r + 1));
      for (var c = 0; c < row.cells.length; c++) {
        var cell = row.cells[c];
        var oa = document.createElement('option');
        oa.value = String(cell.i); oa.textContent = label(cell);
        var ob = oa.cloneNode(true);
        ga.appendChild(oa); gb.appendChild(ob);
      }
      selA.appendChild(ga); selB.appendChild(gb);
    }
  }

  function pick(which, key) {
    if (!cur) return;
    var found = findCell(cur.sec, parseInt(key, 10));
    if (!found) return;
    cur.row = found.row;
    cur[which] = found.cell;
    show();
  }

  function close() {
    mask.classList.remove('on');
    document.body.classList.remove('cmpopen');
    imgA.removeAttribute('src'); imgB.removeAttribute('src');
    cur = null;
  }

  function zoom(oneToOne) {
    if (oneToOne) {
      var w = imgA.naturalWidth || imgA.clientWidth || 0;
      if (w) { wrap.style.width = w + 'px'; wrap.style.maxWidth = 'none'; }
      btnOne.classList.add('on'); btnFit.classList.remove('on');
    } else {
      wrap.style.width = ''; wrap.style.maxWidth = '';
      btnFit.classList.add('on'); btnOne.classList.remove('on');
    }
    requestAnimationFrame(paint);
  }

  // 点格子图 → 打开对比
  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a.gimg') : null;
    if (!a) return;
    var parts = (a.getAttribute('data-ci') || '').split(':');
    if (parts.length !== 3) return;
    e.preventDefault();
    open(parseInt(parts[0], 10), parseInt(parts[1], 10), parseInt(parts[2], 10));
  });

  range.addEventListener('input', function () {
    pos = parseInt(range.value, 10) / 1000; paint();
  });
  selA.addEventListener('change', function () { pick('a', selA.value); });
  selB.addEventListener('change', function () { pick('b', selB.value); });
  document.getElementById('cmpClose').addEventListener('click', close);
  btnFit.addEventListener('click', function () { zoom(false); });
  btnOne.addEventListener('click', function () { zoom(true); });
  mask.addEventListener('mousedown', function (e) {
    if (e.target === mask || e.target === stage) close();
  });
  document.addEventListener('keydown', function (e) {
    if (!cur) return;
    if (e.key === 'Escape') { close(); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') {
      pos = Math.max(0, Math.min(1, pos + (e.key === 'ArrowLeft' ? -0.005 : 0.005)));
      paint(); e.preventDefault();
    }
  });
})();
</script>
</body></html>"""


def make_report(job, path, sections, cols):
    """cols 参数保留只为签名兼容 —— 真正用的是每节自己的 cols / col_vals。"""
    def esc(s):
        return (str(s).replace('&', '&amp;').replace('<', '&lt;')
                .replace('>', '&gt;'))

    def colvals(sec):
        """一节的「列 → 数值」。组合组的列名叫「组合」，直接 float() 会炸，
        所以数值统一从这里取（老记录没有 col_vals，退回按列名解析）。"""
        return sec.get('col_vals') or sec['cols']

    fixed_txt = fixed_label(norm_fixed(job.spec))
    trig = norm_triggers(job.spec)

    # ---------------- 结论栏：谁好谁坏，一句话说完
    verdicts = []
    for sec in sections:
        sm = sec.get('summary')
        if not sm:
            continue
        verdicts.append(
            '<li class="vv vv-%s"><div class="vhead"><b>%s</b>'
            '<span class="pill p-%s">%s</span>%s</div><p>%s</p></li>'
            % (sm['verdict'], esc(sec['short']), sm['verdict'],
               esc(sm['verdict_txt']),
               lora_grade.badge_html(sm.get('grade'), title=sm.get('grade_why') or ''),
               esc(sm['text'])))
    if not verdicts:
        verdicts.append('<li class="vv"><p>没有成功出图，无法给出结论。</p></li>')

    # ---------------- 总览卡片：缩略图 + 推荐档 + 指标 + 曲线
    cards = []
    for sec in sections:
        sm = sec.get('summary') or {}
        best = sec.get('best')
        thumb = None
        cv = colvals(sec)
        if best is not None and cv:
            key = '%g' % best
            if key in cv:
                thumb = sec['grid'][0][cv.index(key)]
        tags = []
        if sm:
            # 品质等级放最前：用户扫一眼卡片先看这个
            if sm.get('grade'):
                tags.append(lora_grade.badge_html(
                    sm['grade'], 'qbig', title=sm.get('grade_why') or ''))
            if sm.get('sharp_ratio') is not None:
                r = sm['sharp_ratio']
                tags.append('<span class="tg %s">画质 %s</span>'
                            % ('ok' if r >= 1.0 else
                               ('warn' if r >= 0.9 else 'bad'), _pct(r)))
            tags.append('<span class="tg">风格偏移 %.2f</span>' % (sm.get('drift') or 0))
            tags.append('<span class="tg">%s</span>' % esc(sm.get('effect_txt') or ''))
            for cs in (sm.get('crashed') or []):
                tags.append('<span class="tg bad">%g 档崩坏</span>' % cs)
        if trig.get(sec['lora']):
            tags.append('<span class="tg trig">触发词 %s</span>' % esc(trig[sec['lora']]))
        img = ('<a href="%s" target="_blank"><img src="%s" alt=""></a>'
               % (esc(thumb), esc(thumb))) if thumb else '<div class="noimg">无图</div>'
        if sec.get('is_combo'):
            bestline = ('<span title="各层 LoRA 按设定权重同时上场">组合：%s</span>'
                        % esc(sec['combo_detail'])) if sec.get('combo_detail') \
                else '组合成立'
        elif best is not None:
            bestline = '推荐强度 <b>%g</b>' % best
        else:
            bestline = '未给出推荐'
        cards.append(
            '<article class="card"><div class="cthumb">%s</div>'
            '<div class="cbody"><div class="cname" title="%s">%s</div>'
            '<div class="cbest">%s</div><div class="ctags">%s</div>%s</div></article>'
            % (img, esc(sec['lora']), esc(sec['short']), bestline,
               ''.join(tags),
               # 组合只有一列、没有强度轴，画不出曲线 —— 画了也是条空白
               '' if sec.get('is_combo') else curve_svg(sm.get('curve') or [])))

    # ---------------- 详图：每档一张，图下直接带指标
    blocks = []
    cmp_data = []          # 给「大图对比」用的数据（见 REPORT_TPL 里那段脚本）
    ci = 0                 # 格子编号，**全报告唯一**（跨节也不重号）
    for si, sec in enumerate(sections):
        sm = sec.get('summary') or {}
        cv = colvals(sec)
        cmap = {}
        for c in sec['cells']:
            cmap[(int(c['prompt_i']), float(c['strength']))] = c

        # 先把这一节的格子编号（全报告唯一），顺手把对比数据攒出来。
        # 基线单独记一份：点开某一档时 A 默认就取它。
        ci_of, cmp_rows = {}, []
        for ri in range(len(sec['rows'])):
            rcells, base_i = [], None
            for cj, url in enumerate(sec['grid'][ri]):
                if not url:
                    continue
                sval = 0.0 if cj == 0 else float(cv[cj])
                rcells.append({'i': ci, 'u': url, 's': ('%g' % sval)})
                if cj == 0:
                    base_i = ci
                ci_of[(ri, cj)] = ci
                ci += 1
            cmp_rows.append({'label': sec['rows'][ri], 'base': base_i,
                             'cells': rcells})
        cmp_data.append({'name': sec['short'], 'rows': cmp_rows})

        grid_html = []
        for ri, rowlab in enumerate(sec['rows']):
            tds = ['<th class="rowh">%s</th>' % esc(rowlab)]
            for cj, url in enumerate(sec['grid'][ri]):
                s_val = 0.0 if cj == 0 else float(cv[cj])
                cell = cmap.get((ri, s_val))
                if url:
                    # data-ci = 节号:行号:格子号 —— 点图就按它开对比层
                    # （href 留着，新标签打开原图这条路还在，只是默认不走它）
                    tds.append('<td class="gc%s">'
                               '<a class="gimg" data-ci="%d:%d:%d" href="%s"'
                               ' title="点开大图对比">'
                               '<img src="%s" alt="" loading="lazy"></a>'
                               '%s</td>'
                               % (' rec' if (cell or {}).get('recommended') else '',
                                  si, ri, ci_of.get((ri, cj), 0),
                                  esc(url), esc(url), _metric_html(cell, s_val == 0)))
                else:
                    tds.append('<td class="miss"><div class="ph">未生成</div></td>')
            grid_html.append('<tr>%s</tr>' % ''.join(tds))

        if sec.get('is_combo'):
            best_txt = ('组合：%s' % esc(sec['combo_detail'])) \
                if sec.get('combo_detail') else '组合成立'
            sec_head = '%s · 组合' % esc(sec['short'])
        else:
            best_txt = ('建议强度 <b>%g</b>' % sec['best']) \
                if sec['best'] is not None else '未给出推荐（数据不足或全部崩坏）'
            sec_head = '%s · 逐档对比' % esc(sec['short'])
        note = ('<div class="cnote">%s</div>' % esc(sm.get('text'))) if sm.get('text') else ''
        fixedline = '固定 LoRA（所有格子共用，不参与扫档）：%s' % esc(sec.get('fixed') or '无')
        if trig.get(sec['lora']):
            fixedline += '<br>触发词：<code>%s</code>' % esc(trig[sec['lora']])

        blocks.append(
            '<section><h2>%s</h2>'
            '<div class="fixedline">%s</div><div class="bestline">%s</div>%s'
            '<div class="wrap"><table class="grid"><tr><th></th>%s</tr>%s</table></div>'
            '<div class="acts"><a class="btn" href="%s" target="_blank">查看对比大图</a>'
            '<a class="btn" href="%s" download>下载对比大图</a></div></section>'
            % (sec_head, fixedline, best_txt, note,
               ''.join('<th>%s</th>' % esc(c) for c in sec['cols']),
               ''.join(grid_html), esc(sec['sheet']), esc(sec['sheet'])))

    prompt_html = ''.join(
        '<li><b>%s</b><p>%s</p></li>' % (esc(p['label'].replace('\n', ' · ')), esc(p['text']))
        for p in job.spec['prompts'])

    # 标题这里直接拼 APP_TITLE，**不要**再写 __TITLE__：
    # REPORT_TPL 里那处 __TITLE__ 是在拼 html 的第一步就替换掉的，
    # 而 head 是后来才插进模板的 —— 再放占位符会原样漏到用户眼前
    # （实测报告页上真的印着「_TITLE__ · LoRA 扫档报告」）。
    head = ('<h1>' + APP_TITLE + ' · LoRA 扫档报告</h1>'
            '<div class="sub">%s · seed %s · %d×%d · %s steps · CFG %s · 共 %d 张 · 耗时 %ss</div>'
            '<div class="sub2">固定 LoRA（所有图共用）：%s</div>'
            '<div class="sub3">工作流：%s</div>'
            % (esc(job.run_id), esc(job.spec['seed']), job.spec['width'],
               job.spec['height'], esc(job.spec['steps']), esc(job.spec['cfg']),
               sum(len(s['cells']) for s in sections),
               round((job.finished or time.time()) - job.started, 1),
               esc(fixed_txt),
               esc(describe_workflow((job.spec or {}).get('workflow')))))
    hint = ('<div class="hint">综合分 = 0.6×画质分 + 0.4×风格偏移度；画质分由锐度、细节熵、'
            '饱和度决定。曲线里<b>实线是综合分</b>（按本批最高分归一到 100%），'
            '<b>虚线是风格偏移</b>（与基线图的真实像素差异，0～1，越大说明 LoRA 改动越多），'
            '红圈 = 该档崩坏。<br>'
            '第一列永远是基线 —— 有固定 LoRA 时，基线 = 只加载固定 LoRA、不加载被对比的那个，'
            '差异才只来自被对比的 LoRA。分数只用于横向排序，最终仍以你的眼睛为准。<br>'
            '<b>点任意一张图</b>会打开「大图对比」：两张图叠在一起、中间一根滑杆裁开，'
            '左右拖动就能在同一处像素上对照基线与该档的差别（用法同 ComfyUI 的 '
            'Image Comparer 节点）。A / B 两个下拉可以换成任意两格，'
            '觉得不够看清就切「1:1」按原始像素比。</div>')

    # 品质等级图例：先说清每档什么意思，再让人看标签
    legend = ['<span class="t">品质等级</span>']
    for k in lora_grade.GRADE_ORDER:
        g = lora_grade.GRADE_BY_KEY[k]
        legend.append('<span class="qgrade" style="color:%s;background:%s" '
                      'title="%s">%s</span><span class="d">%s</span>'
                      % (g['fg'], g['bg'], g['desc'], g['label'], g['desc']))
    qlegend = ''.join(legend)

    # 嵌进 <script> 的 JSON 必须把 </script> 之类的序列破掉，
    # 否则 LoRA 名字里带个 </script> 就能把报告页拆了
    cmp_json = (json.dumps(cmp_data, ensure_ascii=False)
                .replace('<', '\\u003c').replace('>', '\\u003e')
                .replace('&', '\\u0026'))

    html = REPORT_TPL.replace('__TITLE__', APP_TITLE)
    for token, val in (('__CSS__', REPORT_CSS), ('__DARKCSS__', REPORT_DARK_CSS),
                       ('__HEAD__', head), ('__HINT__', hint),
                       ('__QLEGEND__', qlegend),
                       ('__CMPDATA__', cmp_json),
                       ('__VERDICT__', ''.join(verdicts)),
                       ('__CARDS__', ''.join(cards)),
                       ('__DETAIL__', ''.join(blocks)),
                       ('__PROMPTS__', prompt_html)):
        html = html.replace(token, val)

    with open(path, 'w', encoding='utf-8') as f:
        f.write(html)


# ======================================================== 离线：重算 / 导入
# 这一整块完全不需要 ComfyUI —— 指标是本地 Pillow 算的，图片已经在 run 目录里。
def cell_filename(lora_i, prompt_i, strength, combo_i=None):
    """格子对应的文件名。命名是确定的，所以能从 cells 反推出图片在哪。

    combo_i：V0.5.1 的 LoRA 组合格子。组合的列没有强度轴，文件名里
    也不带强度（C0_p1.png），所以必须由调用方点明「这是个组合格子」——
    光看 strength 猜不出来。
    """
    if float(strength) == 0:
        return 'base_p%d.png' % int(prompt_i)
    if combo_i is not None:
        return 'C%d_p%d.png' % (int(combo_i), int(prompt_i))
    return 'L%d_p%d_s%g.png' % (int(lora_i), int(prompt_i), float(strength))


def _baseline_map(img_dir, n_prompts):
    """每个提示词一张基线图（对所有 LoRA 共用）。"""
    out = {}
    for pi in range(n_prompts):
        p = os.path.join(img_dir, 'base_p%d.png' % pi)
        if os.path.exists(p):
            out[pi] = p
    return out


def rebuild_run(run_id):
    """离线重算一条历史：只用 run 目录里已有的图，重算指标 / 评分 / 结论 / 报告。

    用途：改了评分口径想回溯、当时出报告失败、或手动补了几张图想合进同一张对比表。
    """
    d = _runs_child(run_id)
    if not d:
        return None, '找不到这条记录'
    rj_path = os.path.join(d, 'run.json')
    rj = load_json(rj_path, {})
    if not rj:
        return None, '这条记录里没有 run.json'
    spec = rj.get('spec') or {}
    cells = rj.get('cells') or []
    loras = spec.get('loras') or []
    prompts = spec.get('prompts') or []
    strengths = [float(s) for s in (spec.get('strengths') or [])]
    if (not loras and not norm_combos(spec)) or not prompts or not cells:
        return None, '这条记录缺少 LoRA / 提示词 / 格子信息，无法重算'
    # V0.5：图默认就在记录目录下的 images/，但 run.json 里记着实际位置，
    # 以它为准（升级前的老记录没有这个字段，就按默认值走）。
    img_dir = os.path.join(d, (rj.get('img_dir') or 'images').replace('/', os.sep))
    if not os.path.isdir(img_dir):
        img_dir = os.path.join(d, 'images')
    if not os.path.isdir(img_dir):
        return None, '这条记录里没有 images 目录'

    baselines = _baseline_map(img_dir, len(prompts))

    for c in cells:
        name = cell_filename(c.get('lora_i', 0), c.get('prompt_i', 0),
                             c.get('strength', 0), combo_i=c.get('combo_i'))
        fp = os.path.join(img_dir, name)
        c['url'] = '/files/%s/images/%s' % (quote(run_id), quote(name))
        if not os.path.exists(fp):
            c['state'] = 'failed'
            c['error'] = c.get('error') or '图片文件不存在'
            c['metrics'] = None
            c['score'] = None
            c['recommended'] = False
            c['degraded'] = False
            continue
        c['state'] = 'done'
        c['error'] = ''
        base = None if float(c.get('strength') or 0) == 0 \
            else baselines.get(int(c.get('prompt_i') or 0))
        c['metrics'] = analyze(fp, base)

    best, summaries = {}, {}
    for g in scan_groups(spec):
        group = [c for c in cells
                 if int(c.get('lora_i') or 0) == g['i'] and c.get('state') == 'done']
        b = score_run(group, g['strengths'])
        if b is not None:
            best[g['key']] = b
        s = summarize_lora(group, is_combo=g['is_combo'])
        if s:
            if g['is_combo']:
                s['detail'] = g.get('detail') or ''
            summaries[g['key']] = s

    dur = float(rj.get('duration') or 0)
    job = SimpleNamespace(run_id=run_id, spec=spec, cells=cells, best=best,
                          summaries=summaries, sheet=None, report=None,
                          started=time.time() - dur if dur else time.time(),
                          finished=time.time())
    build_outputs(None, job, d, img_dir, strengths, prompts, loras)

    rj['cells'] = cells
    rj['best'] = best
    rj['summaries'] = summaries
    rj['rescored'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    save_json(rj_path, rj)
    return {'ok': True, 'best': best, 'summaries': summaries}, None


IMG_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.bmp')


def list_dir_images(path, limit=120):
    """列出一个目录（含子目录）里最近的图片，给「导入图片」用。"""
    if not path or not os.path.isdir(path):
        return None
    out = []
    for root, _dirs, files in os.walk(path):
        for fn in files:
            if os.path.splitext(fn)[1].lower() in IMG_EXT:
                fp = os.path.join(root, fn)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                out.append({'path': fp, 'name': fn,
                            'rel': os.path.relpath(fp, path),
                            'size': st.st_size, 'mtime': st.st_mtime})
        if len(out) > 5000:
            break
    out.sort(key=lambda x: -x['mtime'])
    return out[:limit]


def import_image(run_id, lora_i, prompt_i, strength, src):
    """把一张外部图片当成某个格子（LoRA × 提示词 × 强度）导入进去，然后重算。"""
    d = _runs_child(run_id)
    if not d:
        return {'error': '找不到这条记录'}
    if not src or not os.path.isfile(src):
        return {'error': '来源图片不存在'}
    if os.path.splitext(src)[1].lower() not in IMG_EXT:
        return {'error': '只支持图片文件'}
    rj_path = os.path.join(d, 'run.json')
    rj = load_json(rj_path, {})
    spec = rj.get('spec') or {}
    prompts = spec.get('prompts') or []
    # V0.5.1：目标格子的编号按 scan_groups 的组序号解释 ——
    # 普通组和组合组共用同一套序号，跟格子里的 lora_i 是同一个口径。
    groups = scan_groups(spec)
    try:
        li, pi, st = int(lora_i), int(prompt_i), float(strength)
    except (TypeError, ValueError):
        return {'error': '目标格子参数不合法'}
    if not (0 <= li < len(groups)) or not (0 <= pi < len(prompts)):
        return {'error': '目标 LoRA / 组合 / 提示词超出范围'}
    g = groups[li]
    c_i = g['ci'] if g['is_combo'] else None
    if g['is_combo']:
        st = COMBO_STRENGTH          # 组合没有强度轴，落盘时统一按 1.0 记

    img_dir = os.path.join(d, 'images')
    os.makedirs(img_dir, exist_ok=True)
    name = cell_filename(li, pi, st, combo_i=c_i)
    shutil.copy2(src, os.path.join(img_dir, name))

    cells = rj.get('cells') or []
    hit = None
    for c in cells:
        if int(c.get('lora_i') or 0) == li and int(c.get('prompt_i') or 0) == pi \
                and abs(float(c.get('strength') or 0) - st) < 1e-9:
            hit = c
            break
    if hit is None:
        hit = {
            'lora': g['lora'], 'lora_short': slug(g['lora']), 'lora_i': li,
            'prompt_i': pi, 'prompt_label': prompts[pi].get('label', ''),
            'strength': st, 'label': ('组合' if g['is_combo'] else
                                      ('外部导入' if st else '基线')),
            'fixed': norm_fixed(spec),
            'trigger': '', 'prompt_used': '', 'state': 'done',
            'url': None, 'metrics': None, 'error': '',
        }
        if g['is_combo']:
            hit['combo'] = g['items']
            hit['combo_i'] = c_i
        cells.append(hit)
    hit['state'] = 'done'
    hit['error'] = ''
    if st != 0 and not g['is_combo']:
        hit['label'] = hit.get('label') or '%g' % st
        if hit['label'] in ('基线', ''):
            hit['label'] = '%g' % st
    if not g['is_combo'] and st not in [float(x) for x in
                                        (spec.get('strengths') or [])]:
        spec['strengths'] = sorted(set([float(x) for x in
                                        (spec.get('strengths') or [0])] + [st]))
    rj['spec'] = spec
    rj['cells'] = cells
    save_json(rj_path, rj)

    res, err = rebuild_run(run_id)
    if err:
        return {'error': '图片已导入，但重算失败：%s' % err}
    return {'ok': True, 'file': name, 'best': res['best'], 'runs': list_runs()}


# ==================================================== LoRA 库 / 下载收件箱
#
# 两块功能共用同一套文件操作：
#   收件箱 —— 从下载目录把新 LoRA 收进 ComfyUI
#   库整理 —— 直接整理 loras 目录里的文件
#
# 设计前提（实测确认，不是猜的）：
#   ComfyUI 的 folder_paths.py 是拿「每个子目录的 mtime」判断要不要重扫的，
#   所以往 models/loras 下任意子目录增删改文件，ComfyUI 下一次请求就会自动
#   感知到，不需要重启、也没有额外的刷新接口要调。
#   （ComfyUI 自己的网页下拉框要 F5 才更新，但本工具每次实时请求，没问题。）

MODEL_EXT = ('.safetensors', '.ckpt', '.pt', '.bin', '.pth')
# 浏览器下载中的临时后缀，扫到就跳过
PART_EXT = ('.crdownload', '.part', '.!ut', '.aria2', '.tmp', '.download')
SIDE_EXT = ('.json', '.txt', '.md', '.png', '.jpg', '.jpeg', '.webp')

# safetensors 头部 __metadata__ 里出现这些键 → 基本可以确定是 LoRA
LORA_META_KEYS = ('ss_network_dim', 'ss_network_alpha', 'ss_base_model_version',
                  'ss_output_name', 'ss_tag_frequency', 'lora_rank',
                  'lora_base_model', 'ss_learning_rate', 'ss_num_batches')
# 出现这些 → 是完整模型（底模 / VAE / 文本编码器），不是 LoRA
BASE_META_KEYS = ('diffusers_version',)
# 旧格式（.ckpt/.pt）读不出元数据，只能扫字节串，命中也只是「疑似」
LORA_BLOB_MAGIC = (b'lora_unet_', b'lora_te_')


# ---------------------------------------------------------- 模型文件探测
def _read_st_header(path, max_bytes=8 * 1024 * 1024):
    """safetensors 头部：前 8 字节是小端 u64 = 后面 JSON 的长度。"""
    with open(path, 'rb') as f:
        raw = f.read(8)
        if len(raw) < 8:
            return None, '文件太短，不是完整的 safetensors'
        n = struct.unpack('<Q', raw)[0]
        if n > max_bytes:
            return None, '头部异常（%d 字节）' % n
        try:
            return json.loads(f.read(n).decode('utf-8', 'replace')), None
        except Exception as e:
            return None, '头部解析失败：%s' % e


def _st_is_lora(header):
    meta = header.get('__metadata__') or {}
    hits = [k for k in LORA_META_KEYS if k in meta]
    if hits:
        return True, hits
    # 有些 LoRA 的 __metadata__ 是空的，但张量名里带 lora
    for t in header:
        if t == '__metadata__':
            continue
        low = t.lower()
        if low.startswith('lora_') or '.lora_' in low:
            return True, ['权重名含 lora_']
    return False, []


def _st_meta(header):
    meta = header.get('__metadata__') or {}

    rank = meta.get('lora_rank') or meta.get('ss_network_dim')
    try:
        rank = int(rank) if rank is not None else None
    except (TypeError, ValueError):
        rank = None

    base = (meta.get('lora_base_model') or meta.get('base_model')
            or meta.get('ss_base_model_version') or '')

    # ss_tag_frequency 是一段 JSON 字符串，里面是训练时用的 tag 与出现次数。
    # 取高频的几个，可以直接当触发词的候选。
    tags = []
    raw = meta.get('ss_tag_frequency')
    if isinstance(raw, str) and len(raw) < 400000:
        try:
            d = json.loads(raw)
            if isinstance(d, dict):
                pairs = sorted(d.items(), key=lambda kv: -kv[1])[:12]
                tags = [str(t) for t, _n in pairs]
            elif isinstance(d, list):
                tags = [str(t) for t in d[:12]]
        except Exception:
            pass

    # ss_output_name 是作者训练时填的输出名，经常就被当成触发词用
    out_name = str(meta.get('ss_output_name') or '')[:60]
    return rank, str(base)[:60], tags, out_name


# ---------------------------------------------------- 触发词找回（纯本地）
# 忘了触发词是最常见的情况。能找的地方都在本地：下载器留下的伴随文件、
# 训练时写进 safetensors 头部的 metadata、以及文件名本身。
# 不联网 —— Civitai 在多数网络下访问不了，联网方案必然变成「点了没反应」。

def _clean_trigger(s):
    """把候选触发词洗成能直接粘进提示词的短文本。"""
    if not isinstance(s, str):
        return ''
    t = s.strip().strip('"').strip("'").strip('，,、')
    t = re.sub(r'\s+', ' ', t)
    if not t or len(t) > 160:
        return ''
    if t.lower() in ('none', 'null', 'n/a', 'unknown', 'undefined', 'true', 'false'):
        return ''
    return t


def _json_triggers(obj, depth=0):
    """从 dict 里挖触发词，兼容 Civitai 伴随文件的几种写法。"""
    out = []
    if not isinstance(obj, dict) or depth > 3:
        return out
    for k, v in obj.items():
        # 去空格/下划线/横线再比：trainedWords / trained_words / trigger-words 都能命中
        kl = str(k).lower().replace('_', '').replace('-', '').replace(' ', '')
        if any(t in kl for t in ('trainedword', 'trigger', 'activation')):
            if isinstance(v, str):
                t = _clean_trigger(v)
                if t:
                    out.append(t)
            elif isinstance(v, list):
                for x in v:
                    if isinstance(x, str):
                        t = _clean_trigger(x)
                    elif isinstance(x, dict):
                        t = _clean_trigger(x.get('text') or x.get('name') or '')
                    else:
                        t = ''
                    if t:
                        out.append(t)
        elif isinstance(v, dict):
            out += _json_triggers(v, depth + 1)
    return out


def _sidecar_triggers(path):
    """从同名伴随文件里找触发词，返回 [(文本, 来源)]。"""
    out = []
    stem = os.path.splitext(path)[0]
    d = os.path.dirname(path) or '.'
    # Civitai 下载器（含 ComfyUI 的 Civitai 扩展）会留下 <name>.civitai.info
    for p, src in ((stem + '.civitai.info', 'civitai'),
                   (stem + '.civitai.json', 'civitai'),
                   (stem + '.json', 'json'),
                   (stem + '.txt', 'txt')):
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                raw = f.read(400000)
        except OSError:
            continue
        got = []
        if src in ('civitai', 'json'):
            try:
                got = _json_triggers(json.loads(raw))
            except Exception:
                pass
            if not got and src == 'json':
                t = _clean_trigger(raw)
                got = [t] if t else []
        else:
            t = _clean_trigger(raw)
            got = [t] if t else []
        for t in got:
            out.append((t, src))

    # 目录级「触发词.txt」：行内带本模型文件名才算，避免张冠李戴。
    # 必须用「带扩展名的全名」去匹配 —— 只用主干（如 d）会在 safetensors
    # 这类长词里命中一堆无关行。全名匹配不上时，才退而求其次用主干，
    # 且要求主干够长，避免再踩同一个坑。
    base = os.path.basename(path).lower()
    stem_base = os.path.splitext(base)[0]
    for fn in ('触发词.txt', 'trigger.txt', 'triggers.txt'):
        p = os.path.join(d, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                lines = f.read(100000).splitlines()
        except OSError:
            continue
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith('#'):
                continue
            low = s.lower()
            if base and base in low:
                hit = base
            elif len(stem_base) >= 5 and stem_base in low:
                hit = stem_base
            else:
                continue
            rest = re.sub(re.escape(hit), '', s, flags=re.I)
            t = _clean_trigger(rest.strip(' \t：-—=,，'))
            if t:
                out.append((t, 'dir-txt'))
    return out


def _name_trigger(name):
    """从文件名猜一个候选。置信度最低，只是给个线索。

    保持原样的分隔符（不把 - 和 _ 换成空格）——换了以后 `krea2-影视柔光-优化`
    会变成 `krea2 影视柔光 优化`，反而更像一个正经触发词，误导性更强。
    """
    stem = os.path.splitext(name)[0]
    s = re.sub(r'[-_.]?(v\d+|final|epoch\d+|e\d+|step\d+|s\d+|\d{3,})$',
               '', stem, flags=re.I)
    s = re.sub(r'[-_]\d{5,}$', '', s).strip(' -_.')
    if 2 <= len(s) <= 40 and s.lower() not in (
            'lora', 'model', 'checkpoint', 'style', 'concept', 'test', 'new'):
        return s
    return ''


def _plausible_name(s):
    """ss_output_name 经常是训练任务名（training_2197446-20260912…），
    那种东西拿来当触发词只会污染提示词，先筛掉。"""
    if not s:
        return False
    if re.search(r'\d{5,}', s):
        return False
    if re.match(r'^(training|output|lora|model|test|untitled|sd|flux)',
                s.lower()):
        return False
    return True


_SRC_LABEL = {'civitai': '随文件下载信息',
              'json': '同名 json',
              'txt': '同名 txt',
              'dir-txt': '目录触发词表',
              'st-meta': '训练标注',
              'st-name': '训练输出名',
              'filename': '文件名推测'}


def collect_triggers(path, name, st_tags=None, st_out_name=''):
    """按可信度从高到低排，返回 [{'text','src','label'}]。"""
    cands = []

    def add(text, src):
        t = _clean_trigger(text)
        if not t:
            return
        if any(t.lower() == x['text'].lower() for x in cands):
            return
        cands.append({'text': t, 'src': src, 'label': _SRC_LABEL.get(src, src)})

    for t, s in _sidecar_triggers(path):
        add(t, s)
    for t in (st_tags or [])[:8]:
        add(t, 'st-meta')
    if _plausible_name(st_out_name):
        add(st_out_name, 'st-name')
    add(_name_trigger(name), 'filename')
    return cands[:6]


_PROBE_CACHE = {}
_PROBE_LOCK = threading.Lock()


def probe_model_file(path):
    """判定一个模型文件是不是 LoRA，顺带读出 rank / 底模 / 训练 tag。

    kind: 'lora' 确定是 LoRA
          'model_base' 是完整模型（底模/VAE/文本编码器），不是 LoRA
          'unknown'  读不出来或没有特征，需要人来判断
    """
    try:
        st = os.stat(path)
    except OSError as e:
        return {'path': path, 'name': os.path.basename(path),
                'kind': 'unknown', 'reason': '读不到：%s' % e}

    key = (path, int(st.st_mtime), st.st_size)
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
    if hit:
        return dict(hit, path=path)

    name = os.path.basename(path)
    ext = os.path.splitext(name)[1].lower()
    out = {'path': path, 'name': name, 'size': st.st_size, 'mtime': st.st_mtime,
           'ext': ext, 'rank': None, 'base': '', 'tags': [], 'st_out_name': '',
           'trigger': '', 'trigger_src': '', 'cands': [],
           'kind': 'unknown', 'reason': ''}

    if ext == '.safetensors':
        header, err = _read_st_header(path)
        if err:
            out['reason'] = err
        else:
            is_lora, hits = _st_is_lora(header)
            rank, base, tags, out_name = _st_meta(header)
            out.update({'rank': rank, 'base': base, 'tags': tags,
                        'st_out_name': out_name})
            if is_lora:
                out['kind'] = 'lora'
                out['reason'] = '头部含 ' + ', '.join(hits[:2])
            elif any(k in (header.get('__metadata__') or {})
                     for k in BASE_META_KEYS):
                out['kind'] = 'model_base'
                out['reason'] = '是完整模型，不是 LoRA'
            else:
                out['reason'] = '头部没有 LoRA 特征'
    elif ext in ('.ckpt', '.pt', '.bin', '.pth'):
        # pickle 格式不能安全地解析出元数据，只能扫字节串
        try:
            with open(path, 'rb') as f:
                blob = f.read(4 * 1024 * 1024)
            if any(m in blob for m in LORA_BLOB_MAGIC):
                out['kind'] = 'lora'
                out['reason'] = '文件里找到 lora_ 权重名（疑似）'
            else:
                out['reason'] = '旧格式，无法确认，需要你自己判断'
        except OSError as e:
            out['reason'] = '读不到：%s' % e
    else:
        out['reason'] = '不是模型文件'

    # 触发词：不管上面判定成什么，都试着找一次（ckpt 也可能带伴随文件）
    cands = collect_triggers(path, name, out.get('tags') or [],
                             out.get('st_out_name') or '')
    out['cands'] = cands
    out['trigger'] = cands[0]['text'] if cands else ''
    out['trigger_src'] = cands[0]['src'] if cands else ''
    out['trigger_label'] = cands[0]['label'] if cands else ''

    with _PROBE_LOCK:
        if len(_PROBE_CACHE) > 4000:
            _PROBE_CACHE.clear()
        _PROBE_CACHE[key] = out
    return dict(out, path=path)


# --------------------------------------------------- loras 物理目录定位
# ComfyUI 的 /object_info 只给相对文件名，不给绝对路径，所以得自己找。

def _parse_extra_model_paths(root):
    """零依赖解析 extra_model_paths.yaml，返回里面 loras 的候选绝对路径。"""
    out = []
    for fn in ('extra_model_paths.yaml', 'extra_model_paths.yaml.example'):
        p = os.path.join(root, fn)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        base = None
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith('#') or ':' not in s:
                continue
            k, _, v = s.partition(':')
            k = k.strip().lower()
            v = v.strip().strip('"').strip("'")
            if k == 'base_path':
                base = v
            elif k in ('loras', 'lora') and base:
                out.append(os.path.normpath(os.path.join(base, v)))
    return out


def _has_model_file(d):
    """这个目录（含一层子目录）里有没有模型文件。"""
    try:
        for fn in os.listdir(d):
            fp = os.path.join(d, fn)
            if os.path.isfile(fp) and \
                    os.path.splitext(fn)[1].lower() in MODEL_EXT:
                return True
        for fn in os.listdir(d):
            sub = os.path.join(d, fn)
            if os.path.isdir(sub):
                for fn2 in os.listdir(sub)[:30]:
                    if os.path.splitext(fn2)[1].lower() in MODEL_EXT:
                        return True
    except OSError:
        pass
    return False


_LORA_ROOT = {'root': None, 'how': ''}


def lora_root(cfg=None, refresh=False):
    """找到 ComfyUI 的 models/loras 物理目录。

    返回 (路径 or None, 是怎么找到的, 试过的路径列表)。
    """
    cfg = cfg or load_config()
    if not refresh and _LORA_ROOT['root'] and os.path.isdir(_LORA_ROOT['root']):
        return _LORA_ROOT['root'], _LORA_ROOT['how'], []

    cands = []
    explicit = (cfg.get('lora_dir') or '').strip()
    if explicit:
        cands.append((explicit, 'config 里指定的 lora_dir'))
    root = (cfg.get('comfy_root') or '').strip()
    if root:
        for rel in ('ComfyUI/models/loras', 'models/loras'):
            cands.append((os.path.join(root, rel), 'comfy_root + ' + rel))
        for d in (root, os.path.join(root, 'ComfyUI')):
            for p in _parse_extra_model_paths(d):
                cands.append((p, 'extra_model_paths.yaml'))

    tried = []
    for p, how in cands:
        tried.append(p)
        if os.path.isdir(p) and _has_model_file(p):
            _LORA_ROOT.update({'root': os.path.normpath(p), 'how': how})
            return _LORA_ROOT['root'], how, tried

    # 最后在 comfy_root 下找叫 loras 的目录（最多走 4 层）
    if root and os.path.isdir(root):
        base = os.path.normpath(root)
        for dp, dns, _fns in os.walk(base):
            if os.path.relpath(dp, base).count(os.sep) > 4:
                dns[:] = []
                continue
            if os.path.basename(dp).lower() == 'loras' and _has_model_file(dp):
                _LORA_ROOT.update({'root': os.path.normpath(dp),
                                   'how': '在 comfy_root 下搜到'})
                return _LORA_ROOT['root'], _LORA_ROOT['how'], tried

    _LORA_ROOT.update({'root': None, 'how': ''})
    return None, '', tried


# ------------------------------------------------------------ 路径安全
def norm_rel(rel):
    """把相对路径统一成**正斜杠**形式。

    V0.4.1 修的坑：os.path.relpath() 在 Windows 上返回反斜杠，
    所以 /api/lib/list 给前端的是 "子目录\\文件.safetensors"，
    而 tag_overrides.json 与 /api/lib/tags 里存的一直是正斜杠。
    前端拿 list 的 rel 去查 tags 的表，一个都匹配不上 —— 表现就是
    「定级明明存盘了，文件名却不变色、等级筛选也筛不出东西」。

    这里的值是**展示与查表用的键**，不是磁盘路径；
    真要访问磁盘必须走 safe_join()（它两种斜杠都能接）。
    """
    return (rel or '').replace('\\', '/').strip('/')


def safe_join(root, rel):
    """把相对路径限制在 root 内。越界返回 None。"""
    root = os.path.normpath(os.path.abspath(root))
    rel = norm_rel(rel).replace('/', os.sep)
    if '..' in rel.split(os.sep):
        return None
    p = os.path.normpath(os.path.join(root, rel)) if rel else root
    try:
        if os.path.commonpath([root, p]) != root:
            return None
    except ValueError:
        return None
    return p


def uniq_dest(dest):
    """目标已存在就自动改名，绝不覆盖。"""
    if not os.path.exists(dest):
        return dest
    base, ext = os.path.splitext(dest)
    i = 1
    while os.path.exists('%s_%d%s' % (base, i, ext)):
        i += 1
    return '%s_%d%s' % (base, i, ext)


def _relocate_override(ov, old_rel, new_rel):
    """把一条覆盖记录的键从旧相对路径挪到新相对路径。返回有没有挪到东西。

    **为什么必须做**：tag_overrides.json 是拿**相对路径**当键的，
    所以文件一搬位置，键就指向一个不存在的地方。症状是
    「明明定过上品，挪个文件夹就变回未定级」—— 而且不是报错，
    是静默地掉出品质丹房，人只会觉得工具把等级弄丢了。

    这是 V0.5.2 做收纳功能时才暴露出来的：V0.2 那套文件移动能力
    和 V0.4 的定级能力是各做各的，谁也没管对方。移动/重命名这些
    **改变相对路径**的动作，都得从这里过一道。

    目标键已经存在时**不覆盖而是合并**（目标已有的值优先，缺的字段
    从旧的补过来）—— 免得搬一次文件把目标位置原有的记录冲掉。
    """
    old_rel = norm_rel(old_rel)
    new_rel = norm_rel(new_rel)
    if not old_rel or not new_rel or old_rel == new_rel:
        return False
    src = ov.pop(old_rel, None)
    if not isinstance(src, dict) or not src:
        return False
    dst = ov.get(new_rel)
    if not isinstance(dst, dict):
        ov[new_rel] = src
    else:
        for k, v in src.items():
            dst.setdefault(k, v)
    return True


def _relocate_prefix(ov, old_prefix, new_prefix):
    """整盒搬家：把 old_prefix 底下**所有**记录的键平移到 new_prefix 底下。

    和 _relocate_override 是一个道理，只是粒度从「一个文件」变成
    「一整棵子树」。搬文件夹时必须用这个 —— 只挪文件夹本身那条记录
    是不够的，里面每个文件的等级也得跟着走。
    """
    op = norm_rel(old_prefix)
    np_ = norm_rel(new_prefix)
    moved = 0
    for k in [k for k in ov.keys()
              if k == op or k.startswith(op + '/')]:
        tail = k[len(op):].lstrip('/')
        nk = (np_ + '/' + tail) if np_ else tail
        if _relocate_override(ov, k, nk):
            moved += 1
    return moved


# ---------------------------------------------------------- 收件箱扫描
def default_watch_dirs():
    home = os.path.expanduser('~')
    out = []
    for p in (os.path.join(home, 'Downloads'), os.path.join(home, 'Desktop')):
        if os.path.isdir(p):
            out.append(p)
    return out


def scan_inbox(cfg, dirs=None, max_depth=2, only_lora=False):
    dirs = dirs or (cfg.get('watch_dirs') or default_watch_dirs())
    if isinstance(dirs, str):
        dirs = [d.strip() for d in dirs.split(';') if d.strip()]
    items, errs, seen = [], [], set()
    for d in dirs:
        d = (d or '').strip()
        if not d:
            continue
        if not os.path.isdir(d):
            errs.append('目录不存在：%s' % d)
            continue
        base = d.rstrip(os.sep)
        for dp, dns, fns in os.walk(d):
            if dp[len(base):].count(os.sep) >= max_depth:
                dns[:] = []
            for fn in fns:
                ext = os.path.splitext(fn)[1].lower()
                if ext in PART_EXT or ext not in MODEL_EXT:
                    continue
                fp = os.path.join(dp, fn)
                if fp in seen:
                    continue
                seen.add(fp)
                try:
                    st = os.stat(fp)
                except OSError:
                    continue
                info = probe_model_file(fp)
                if only_lora and info['kind'] != 'lora':
                    continue
                info['rel'] = os.path.relpath(fp, d)
                info['dir'] = d
                # 刚动过 = 很可能还在下载，别急着移
                info['busy'] = (time.time() - st.st_mtime) < 120
                items.append(info)
    items.sort(key=lambda x: -x['mtime'])
    return {'dirs': dirs, 'items': items, 'errors': errs}


# ------------------------------------------------------------ 移动作业
MOVE = {'busy': False, 'done': 0, 'total': 0, 'message': '', 'results': []}
MOVE_LOCK = threading.Lock()


def move_state():
    with MOVE_LOCK:
        return dict(MOVE)


def start_move(paths, target_dir):
    paths = [p for p in (paths or []) if p]
    if not paths:
        return {'error': '没有选中任何文件'}
    if not target_dir:
        return {'error': '没有指定目标目录'}
    with MOVE_LOCK:
        if MOVE['busy']:
            return {'error': '还有一批正在移动，等它跑完'}
        MOVE.update({'busy': True, 'done': 0, 'total': len(paths),
                     'message': '开始…', 'results': []})
    threading.Thread(target=_move_worker, args=(paths, target_dir),
                     daemon=True).start()
    return {'ok': True, 'total': len(paths)}


def _move_worker(paths, target_dir):
    for fp in paths:
        name = os.path.basename(fp)
        try:
            if not os.path.isfile(fp):
                raise OSError('文件已不在原处')
            src_dev = os.stat(fp).st_dev
            os.makedirs(target_dir, exist_ok=True)
            dst_dev = os.stat(target_dir).st_dev
            want = os.path.join(target_dir, name)
            dst = uniq_dest(want)
            with MOVE_LOCK:
                MOVE['message'] = '正在移动 %s' % name
            shutil.move(fp, dst)
            rec = {'name': name, 'ok': True, 'dest': dst,
                   'renamed': dst != want, 'copied': src_dev != dst_dev}
        except Exception as e:
            rec = {'name': name, 'ok': False,
                   'error': str(e) or e.__class__.__name__}
        with MOVE_LOCK:
            MOVE['results'].append(rec)
            MOVE['done'] += 1
    with MOVE_LOCK:
        ok = sum(1 for r in MOVE['results'] if r['ok'])
        MOVE['busy'] = False
        MOVE['message'] = '完成 %d / %d' % (ok, len(paths))


# ---------------------------------------------------------- 库：浏览
def count_models(d, limit=4000):
    n = 0
    for _dp, _dns, fns in os.walk(d):
        n += sum(1 for f in fns
                 if os.path.splitext(f)[1].lower() in MODEL_EXT)
        if n > limit:
            break
    return n


def lib_tree(root):
    """所有子目录的扁平列表（前端按 depth 画成树）。"""
    root = os.path.normpath(os.path.abspath(root))
    out = [{'rel': '', 'name': '（根目录）', 'depth': 0,
            'count': count_models(root)}]
    for dp, dns, _fns in os.walk(root):
        dns.sort(key=str.lower)
        raw = os.path.relpath(dp, root)
        if raw == '.':
            continue
        # V0.4.1：对外一律正斜杠，depth 也按正斜杠算层数
        rel = norm_rel(raw)
        out.append({'rel': rel, 'name': os.path.basename(dp),
                    'depth': rel.count('/') + 1,
                    'count': count_models(dp)})
    return out


def lib_list(root, sub=''):
    d = safe_join(root, sub)
    if d is None or not os.path.isdir(d):
        return {'error': '目录不存在或越界'}
    sub = norm_rel(sub)
    root = os.path.normpath(os.path.abspath(root))
    dirs, files = [], []
    try:
        names = sorted(os.listdir(d), key=str.lower)
    except OSError as e:
        return {'error': '读不了这个目录：%s' % e}
    for fn in names:
        fp = os.path.join(d, fn)
        # V0.4.1：rel 统一正斜杠，否则前端查标签表必然对不上
        rel = norm_rel(os.path.relpath(fp, root))
        if os.path.isdir(fp):
            dirs.append({'name': fn, 'rel': rel, 'count': count_models(fp)})
            continue
        ext = os.path.splitext(fn)[1].lower()
        if ext in MODEL_EXT:
            item = dict(probe_model_file(fp), rel=rel)
        elif ext in SIDE_EXT:
            try:
                sz = os.path.getsize(fp)
            except OSError:
                sz = 0
            item = {'name': fn, 'rel': rel, 'path': fp, 'size': sz,
                    'ext': ext, 'kind': 'other', 'rank': None,
                    'base': '', 'tags': [], 'reason': '伴随文件',
                    'trigger': '', 'trigger_src': '', 'cands': []}
        else:
            continue
        files.append(item)
    return {'root': root, 'sub': sub, 'dirs': dirs, 'files': files}


# ---------------------------------------------------------- 库：整理
_BAD_CHARS = set('<>:"|?*')


def lib_mkdir(root, sub, name):
    name = (name or '').strip().strip('/\\')
    if not name or name in ('.', '..'):
        return {'error': '文件夹名不合法'}
    if any(c in _BAD_CHARS for c in name):
        return {'error': '文件夹名不能含 < > : " | ? *'}
    rel = os.path.join(sub, name) if sub else name
    p = safe_join(root, rel)
    if p is None:
        return {'error': '路径越界'}
    if os.path.exists(p):
        return {'error': '已经存在了：%s' % name}
    try:
        os.makedirs(p)
    except OSError as e:
        return {'error': '建不了：%s' % e}
    return {'ok': True, 'rel': norm_rel(os.path.relpath(p, os.path.abspath(root)))}


def lib_rename(root, rel, newname):
    """给文件或**文件夹**改名。

    V0.5.2 起支持给文件夹改名（收纳盒改名）。改名同样会改变相对路径，
    所以标签记录必须一起搬（文件搬自己那条，文件夹搬整棵子树）。
    """
    src = safe_join(root, rel)
    if not src or not os.path.exists(src):
        return {'error': '文件不存在'}
    newname = (newname or '').strip()
    if not newname or newname in ('.', '..'):
        return {'error': '名字不能为空'}
    if any(c in _BAD_CHARS or c in '/\\' for c in newname):
        return {'error': '名字不能含 / \\ < > : " | ? *'}
    root_abs = os.path.abspath(root)
    is_dir = os.path.isdir(src)
    if is_dir and os.path.normpath(src) == os.path.normpath(root_abs):
        return {'error': '不能给库根目录改名'}
    dst = os.path.join(os.path.dirname(src), newname)
    if os.path.exists(dst):
        return {'error': '目标位置已经有同名文件'}
    new_rel = norm_rel(os.path.relpath(dst, root_abs))
    try:
        os.rename(src, dst)
    except OSError as e:
        return {'error': '改不了：%s（可能被 ComfyUI 占用）' % e}
    ov = _tag_overrides()
    if is_dir:
        dirty = _relocate_prefix(ov, rel, new_rel)
    else:
        dirty = _relocate_override(ov, rel, new_rel)
    if dirty:
        save_json(TAGS_PATH, ov)
    return {'ok': True, 'rel': new_rel, 'is_dir': is_dir, 'grade_moved': dirty}


def lib_move(root, rels, target_sub, dirs=None):
    """把文件（或整个文件夹）移到目标目录下。

    V0.5.2 起多了一个 `dirs` 参数 —— 支持**整盒搬家**。收纳时经常要把
    一整个分类文件夹挪到另一个分类底下，一次挪一个文件太笨。

    **任何改变相对路径的动作都必须在这里把标签记录一起搬走**，
    否则已定级的 LoRA 一挪位置就静默掉出品质丹房（详见 _relocate_override）。
    """
    d = safe_join(root, target_sub or '')
    if d is None or not os.path.isdir(d):
        return {'error': '目标目录不存在'}
    d = os.path.normpath(d)
    root_abs = os.path.abspath(root)
    ov = _tag_overrides()
    ov_dirty = False
    ok, bad, box_ok, box_bad = [], [], [], []

    # ---- 整盒（文件夹）搬家 ----
    for rel in (dirs or []):
        src = safe_join(root, rel)
        if not src or not os.path.isdir(src):
            box_bad.append({'rel': rel, 'error': '文件夹不存在'})
            continue
        src = os.path.normpath(src)
        if src == root_abs:
            box_bad.append({'rel': rel, 'error': '不能搬库根目录'})
            continue
        # 不许把盒子塞进它自己或它自己的子孙里 —— 那样目录树会被搅烂
        # （Windows 上实测直接 WinError 87，而且半路失败会留下残骸）
        if d == src or d.startswith(src + os.sep):
            box_bad.append({'rel': rel, 'error': '不能把文件夹移进它自己里面'})
            continue
        if os.path.dirname(src) == d:
            box_bad.append({'rel': rel, 'error': '已经在这个位置了'})
            continue
        dst = uniq_dest(os.path.join(d, os.path.basename(src)))
        new_rel = norm_rel(os.path.relpath(dst, root_abs))
        try:
            shutil.move(src, dst)
        except OSError as e:
            box_bad.append({'rel': rel, 'error': '%s（可能被 ComfyUI 占用）' % e})
            continue
        if _relocate_prefix(ov, rel, new_rel):
            ov_dirty = True
        box_ok.append({'rel': norm_rel(rel), 'dest': new_rel,
                       'renamed': os.path.basename(dst) != os.path.basename(src)})

    # ---- 单个文件 ----
    for rel in (rels or []):
        src = safe_join(root, rel)
        if not src or not os.path.exists(src):
            bad.append({'rel': rel, 'error': '文件不存在'})
            continue
        if os.path.isdir(src):
            bad.append({'rel': rel, 'error': '这是文件夹，请用整盒搬家'})
            continue
        dst = uniq_dest(os.path.join(d, os.path.basename(src)))
        new_rel = norm_rel(os.path.relpath(dst, root_abs))
        try:
            shutil.move(src, dst)
        except OSError as e:
            bad.append({'rel': rel,
                        'error': '%s（可能被 ComfyUI 占用）' % e})
            continue
        if _relocate_override(ov, rel, new_rel):
            ov_dirty = True
        ok.append({'rel': norm_rel(rel), 'dest': new_rel,
                   'renamed': os.path.basename(dst) != os.path.basename(src)})

    if ov_dirty:
        # 只落一次盘：中途失败不该留下写了一半的 JSON
        save_json(TAGS_PATH, ov)
    return {'ok': True, 'moved': ok, 'failed': bad,
            'boxes': box_ok, 'box_failed': box_bad,
            'grade_moved': ov_dirty}


TRASH_DIR = os.path.join(HERE, 'trash')


def _to_recycle_bin(paths):
    """走 Windows 系统回收站。

    返回 (真的删掉了的, 还在原地的, 原因)。
    判断成功与否**只看文件有没有消失，不看返回码** —— 这是实测出来的：
    SHFileOperationW 带 FOF_ALLOWUNDO 把文件送进回收站后，返回码经常是 2
    （ERROR_FILE_NOT_FOUND），但文件确实已经躺在 $Recycle.Bin 里了。
    反过来，不带 FOF_ALLOWUNDO 时它返回 0 却是**永久删除**。
    所以：FOF_ALLOWUNDO 必须带上，且以「文件是否消失」为准。
    """
    try:
        import ctypes
        from ctypes import wintypes

        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [('hwnd', wintypes.HWND),
                        ('wFunc', wintypes.UINT),
                        ('pFrom', wintypes.LPCWSTR),
                        ('pTo', wintypes.LPCWSTR),
                        ('fFlags', ctypes.c_uint16),
                        ('fAnyOperationsAborted', wintypes.BOOL),
                        ('hNameMappings', ctypes.c_void_p),
                        ('lpszProgressTitle', wintypes.LPCWSTR)]

        buf = '\0'.join(paths) + '\0\0'
        op = SHFILEOPSTRUCTW()
        op.hwnd = None
        op.wFunc = 3                       # FO_DELETE
        op.pFrom = buf
        op.pTo = None
        # FOF_ALLOWUNDO 绝不能少，少了就是永久删除
        op.fFlags = 0x0040 | 0x0010 | 0x0400 | 0x0004
        res = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    except Exception as e:
        return [], list(paths), '调用失败：%s' % e

    gone = [p for p in paths if not os.path.exists(p)]
    left = [p for p in paths if os.path.exists(p)]
    if not left:
        return gone, left, ''
    return gone, left, '还有 %d 个没删掉（返回码 %d）' % (len(left), res)


def lib_delete(root, rels):
    """删除 —— 优先系统回收站；回收站用不了就挪到工具自己的回收目录。
    永远不做不可恢复的删除。"""
    paths, bad = [], []
    for rel in rels or []:
        p = safe_join(root, rel)
        if not p:
            bad.append({'rel': rel, 'error': '路径越界，已拒绝'})
        elif not os.path.exists(p):
            bad.append({'rel': rel, 'error': '文件不存在'})
        elif os.path.isdir(p):
            bad.append({'rel': rel, 'error': '不支持删整个文件夹'})
        else:
            paths.append(p)
    if not paths:
        return {'ok': True, 'deleted': [], 'failed': bad, 'how': ''}

    gone, left, why = _to_recycle_bin(paths)
    if not left:
        return {'ok': True, 'deleted': [os.path.basename(p) for p in gone],
                'failed': bad, 'how': '已放进系统回收站'}

    # 回收站搞不定的那几个 —— 也绝不直接删，挪到工具自己的回收目录
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
    d = os.path.join(TRASH_DIR, stamp)
    moved, fail = [], []
    for p in left:
        try:
            os.makedirs(d, exist_ok=True)
            shutil.move(p, uniq_dest(os.path.join(d, os.path.basename(p))))
            moved.append(os.path.basename(p))
        except OSError as e:
            fail.append({'rel': os.path.basename(p),
                         'error': '%s（可能被 ComfyUI 占用）' % e})
    how = '已放进系统回收站' if gone else ''
    if moved:
        how = (how + '；' if how else '') + '其余挪到 %s' % d
    if fail:
        how = (how + '；' if how else '') + why
    return {'ok': True,
            'deleted': [os.path.basename(p) for p in gone] + moved,
            'failed': bad + fail, 'how': how or why}


def _fingerprint(path):
    """头尾各 1MB + 文件大小。全文件哈希对 71G 太慢，这个够用。"""
    h = hashlib.sha1()
    h.update(str(os.path.getsize(path)).encode())
    with open(path, 'rb') as f:
        h.update(f.read(1 << 20))
        try:
            f.seek(-(1 << 20), os.SEEK_END)
            h.update(f.read(1 << 20))
        except OSError:
            pass
    return h.hexdigest()


def _full_hash(path, block=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        while True:
            b = f.read(block)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# 目录或文件名里出现这些词，通常说明这份是后来复制/备份出来的
_DUP_DIR_HINT = ('backup', '备份', 'dup', '重复', 'temp', 'tmp', '新建文件夹',
                 'old', '旧', '副本', 'copy', '整理', '未分类', 'downloads', '下载')
_DUP_NAME_HINT = ('(1)', '(2)', '(3)', ' - copy', ' copy', '副本', '-副本',
                  '_copy', 'backup', 'bak')


def _pick_keep(root, paths):
    """一组内容相同的文件里，挑最像「正主」的那个。"""
    def key(p):
        n = os.path.basename(p).lower()
        try:
            rel = os.path.relpath(p, root).lower()
        except ValueError:
            rel = n
        d = os.path.dirname(rel)
        bad_dir = sum(1 for w in _DUP_DIR_HINT if w in d)
        bad_name = sum(1 for w in _DUP_NAME_HINT if w in n)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            mt = 0.0
        # 先看「像不像备份」，再看目录深浅，最后看谁更老
        return (bad_dir + bad_name, d.count(os.sep) + (1 if d in ('.', '') else 0) * 0,
                d.count(os.sep), mt)
    return sorted(paths, key=key)[0]


def find_lora_path(cfg, name):
    """按 ComfyUI 里的 LoRA 名（可能是 sub/xxx.safetensors）找绝对路径。"""
    if not name:
        return None
    root, _how, _tried = lora_root(cfg)
    if not root:
        return None
    p = safe_join(root, name)
    if p and os.path.isfile(p):
        return p
    fn = os.path.basename(str(name).replace('/', os.sep).replace('\\', os.sep))
    for dp, _dns, fns in os.walk(root):
        if fn in fns:
            return os.path.join(dp, fn)
    return None


def _dupe_group(root, sz, paths):
    paths = sorted(paths)
    keep = _pick_keep(root, paths)
    names = {os.path.basename(p) for p in paths}
    return {'size': sz, 'count': len(paths),
            'same_name': len(names) == 1,
            'waste': sz * (len(paths) - 1),
            'keep': norm_rel(os.path.relpath(keep, root)),
            'files': [{'rel': norm_rel(os.path.relpath(p, root)),
                       'name': os.path.basename(p),
                       'keep': p == keep} for p in paths]}


def lib_dupes(root, min_size=2 * 1024 * 1024, verify=True):
    """找内容完全相同的文件。

    三道关：大小相同 → 头尾指纹相同 → 全文件 sha256 复核。
    **文件名和路径完全不参与判断**，所以两种容易混淆的情况都能分清楚：
      同名不同路径 + 内容不同  → 不算重复（只是撞名）
      不同名不同路径 + 内容相同 → 算重复（真浪费空间）
    """
    root = os.path.normpath(os.path.abspath(root))
    by_size = {}
    for dp, _dns, fns in os.walk(root):
        for fn in fns:
            if os.path.splitext(fn)[1].lower() not in MODEL_EXT:
                continue
            fp = os.path.join(dp, fn)
            try:
                sz = os.path.getsize(fp)
            except OSError:
                continue
            if sz >= min_size:
                by_size.setdefault(sz, []).append(fp)

    out = []
    for sz, fps in by_size.items():
        if len(fps) < 2:
            continue
        by_fp = {}
        for fp in fps:
            try:
                by_fp.setdefault(_fingerprint(fp), []).append(fp)
            except OSError:
                continue
        for _h, same in by_fp.items():
            if len(same) < 2:
                continue
            groups = [same]
            if verify:
                # 头尾一致只说明大概率相同，整文件再核一遍才敢让用户删
                by_full = {}
                for p in same:
                    try:
                        by_full.setdefault(_full_hash(p), []).append(p)
                    except OSError:
                        continue
                groups = [g for g in by_full.values() if len(g) > 1]
            for g in groups:
                out.append(_dupe_group(root, sz, sorted(g)))
    out.sort(key=lambda x: -x['waste'])
    return out


# ============================================== V0.3：库自动打标签 + 一键整理
# 标签只存在 loras 目录里，靠三样本地信息推断（文件名 / 所在目录 /
# safetensors 头里的训练 tag），不联网。用户点错一次可以用「手动改」纠正，
# 纠正结果记在 tag_overrides.json，下次直接采用。
#
# tag_overrides.json 结构：
#   { "子目录/文件.safetensors": {"cat": "portrait", "at": "...",
#                                 "labels": ["常用", "待测"] } }
# labels 是用户自己加的中文标签，机器不会自动生成，也不会被自动覆盖。
#
# V0.4 新增 grade 字段：用户手动指定的品质等级（劣品/下品/中品/上品）。
# 它和 cat / labels 是三件独立的事 —— 撤销其中一件不能影响另外两件
# （之前撤销类别时把整条记录 pop 掉，连标签一起没了）。

def _tag_overrides():
    """读人工覆盖表，顺手把历史遗留的反斜杠键修正成正斜杠。

    之前 V0.4 有个接口直接写 Windows 原生路径做键（子目录\\文件.safetensors），
    而 tag_one 查表用的是正斜杠，于是同一条记录读不出来。
    这里在读的时候统一键名，旧数据不用用户手动改文件。
    """
    ov = load_json(TAGS_PATH, {})
    if not isinstance(ov, dict):
        return {}
    fixed = {}
    dirty = False
    for k, v in ov.items():
        nk = norm_rel(k)
        if nk != k:
            dirty = True
            # 同一个文件两种写法都存在时，正斜杠那条优先（新的写入路径用正斜杠）
            if nk in fixed:
                continue
        fixed[nk] = v
    if dirty:
        try:
            save_json(TAGS_PATH, fixed)
        except Exception:
            pass          # 修不了也不该让整个标签功能挂掉
    return fixed


MAX_LABEL_LEN = 12
MAX_LABELS_PER_FILE = 8


def _clean_label(s):
    # 这里是 docstring 而不是注释：写成文档字符串必须转义反斜杠，
    # 否则 Python 3.12 会在启动时打 SyntaxWarning，用户双击 start.bat
    # 就会在黑窗口里看到两行黄色警告（以为是报错）。
    r"""把用户输入的标签收拾干净：去空白、去换行、去路径非法字符。

    标签会变成文件夹名（按自定义标签整理时），所以必须挡住 \ / : * ? " < > |
    这些在 Windows 上不能用的字符。
    """
    s = (s or '').strip().replace('\n', ' ').replace('\r', ' ')
    s = re.sub(r'[\\/:*?"<>|]+', '', s)
    s = re.sub(r'\s+', ' ', s).strip(' .')
    return s[:MAX_LABEL_LEN]


def _labels_of(ov_rel):
    """从一个 override 条目里取出合法标签列表。"""
    raw = (ov_rel or {}).get('labels') or []
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for x in raw:
        c = _clean_label(str(x))
        if c and c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= MAX_LABELS_PER_FILE:
            break
    return out


def _prune_override(rel, ov):
    """把一个条目里没用的空字段清掉，整个空了就把条目删掉。

    判断「空了」只看实质内容（cat / labels / grade），时间戳 at 不算 ——
    否则撤销类别后会留下一个只有 at 的空壳条目。
    """
    e = ov.get(rel)
    if not isinstance(e, dict):
        ov.pop(rel, None)
        return
    if not e.get('cat'):
        e.pop('cat', None)
    if not e.get('labels'):
        e.pop('labels', None)
    # V0.4：手动品质等级。非法值当没设，否则一个手滑写错的等级会让条目永久留渣。
    g = lora_grade.clean_grade(e.get('grade'))
    if g:
        e['grade'] = g
    else:
        e.pop('grade', None)
    if not (e.get('cat') or e.get('labels') or e.get('grade')):
        ov.pop(rel, None)
        return
    if not e.get('at'):
        e.pop('at', None)


def _walk_models(root, max_files=6000):
    """递归列出所有模型文件的相对路径。"""
    out = []
    root = os.path.abspath(root)
    for dp, dns, fns in os.walk(root):
        dns.sort(key=str.lower)
        for fn in sorted(fns, key=str.lower):
            if os.path.splitext(fn)[1].lower() in MODEL_EXT:
                # V0.4.1：统一正斜杠，和 tag_overrides.json 的键一致
                out.append(norm_rel(os.path.relpath(os.path.join(dp, fn), root)))
                if len(out) >= max_files:
                    return out
    return out


def tag_one(root, rel):
    """给单个 LoRA 打标签（会读 safetensors 头，所以比纯文件名慢一点）。"""
    full = safe_join(root, rel)
    if not full or not os.path.isfile(full):
        return None
    probe = probe_model_file(full)
    d = os.path.dirname(rel)
    info = lora_tags.classify(
        os.path.basename(rel), d,
        tags=probe.get('tags') or [],
        out_name=probe.get('st_out_name') or '',
        base=probe.get('base') or '')

    ov = _tag_overrides().get(norm_rel(rel)) or {}
    if ov.get('cat'):
        cat = ov.get('cat') or info['cat']
        cat = cat if cat in lora_tags.CAT_BY_KEY or cat == 'unknown' else 'unknown'
        info.update({
            'cat': cat,
            'cat_label': lora_tags.CAT_BY_KEY.get(cat, {}).get('label', '未识别'),
            'confidence': 'manual',
            'why': '你手动指定的',
        })
    info['labels'] = _labels_of(ov)
    # V0.4：品质等级。库页面没有测试数据，所以库里显示的是**用户手动设的**等级；
    # 用户没设就是「未定级」。报告页那边是按测试指标自动定级，两边互不干扰。
    g = lora_grade.clean_grade(ov.get('grade'))
    info['grade'] = g
    info['grade_label'] = (lora_grade.GRADE_BY_KEY[g]['label'] if g else '')
    info['grade_manual'] = bool(g)
    info['rel'] = norm_rel(rel)
    info['name'] = os.path.basename(rel)
    info['size'] = probe.get('size', 0)
    info['kind'] = probe.get('kind', 'unknown')
    info['rank'] = probe.get('rank')
    # 触发词。probe_model_file 早就找出来了，这里一直漏搬 ——
    # 结果是 /api/lib/tags 的每一行都没有 trigger，普通丹房收纳盒里
    # 那个「点一下复制触发词」的 chip 拿不到数据，等于摆设。
    # （V0.5.1 的触发词走的是另一条路：lib_list → probe_model_file，
    #   那条路随普通丹房改版一起没了。）
    # **只带这三个短字段，不带 cands**：cands 是候选列表，
    # 271 行全塞进去会把响应白白撑大，前端也只用到第一名。
    info['trigger'] = probe.get('trigger') or ''
    info['trigger_src'] = probe.get('trigger_src') or ''
    info['trigger_label'] = probe.get('trigger_label') or ''
    return info


def lib_grade_index(root):
    """整库的「谁是什么品质」索引 —— 丹房（品质丹房 / 普通丹房）靠它分组。

    刻意**不读 safetensors 头**：那件事 lib_tag_scan 已经做了，代价是每个
    文件一次 IO；整库几百个文件就是一两秒。丹房只需要「名字 + 等级 + 大小」，
    读一次 tag_overrides.json 就够，所以这里快得多，可以随点随刷。

    等级是**虚拟归档**的依据，不是磁盘上的目录：改等级只动 tag_overrides.json，
    不搬文件。这样 ComfyUI 里引用了这些 LoRA 的工作流不会因为改个等级就失效，
    用户也可以随时改回来。
    """
    ov = _tag_overrides()
    grades = grades_payload()
    glabel = {g['key']: g.get('label', '') for g in grades}
    order = [g['key'] for g in grades]
    groups = {k: [] for k in order + ['none']}
    rows = []
    for rel in _walk_models(root):
        rel = norm_rel(rel)
        fp = safe_join(root, rel)
        try:
            sz = os.path.getsize(fp) if fp else 0
        except OSError:
            sz = 0
        g = lora_grade.clean_grade((ov.get(rel) or {}).get('grade'))
        sub = os.path.dirname(rel).replace('\\', '/')
        row = {'rel': rel, 'name': os.path.basename(rel), 'sub': sub,
               'size': sz, 'grade': g, 'grade_label': glabel.get(g, '')}
        rows.append(row)
        groups[g or 'none'].append(row)
    return {
        'root': root,
        'rows': rows,
        'groups': groups,
        'dirs': dir_groups(rows),
        'grades': grades,
        'order': order + ['none'],
        'total': len(rows),
        'graded': sum(1 for r in rows if r['grade']),
        'ungraded': len(groups['none']),
    }


def dir_groups(rows):
    """把整库的 LoRA 按**它所在的磁盘目录**分身 —— 普通丹房的收纳盒靠它分组。

    V0.5.2 加的。刻意和 `groups`（按品质分）并列放在同一个返回里：
    品质丹房组织维度是「品质」，普通丹房是「磁盘路径」，两者是**同一批
    数据、两种切法**，前端拿到一份就能画两块，不用打两次接口。

    排序：有名字的目录按名排（大小写不敏感），**根目录那盒永远放最后** ——
    「散落在根目录」在直觉上属于「还没收拾」的那一堆，垫底最顺手。

    盒子的 label：根目录叫「散落在根目录」，子目录直接用它自己那层名字
    （不是完整路径，太长了）；完整路径放在 sub 里给 title 用。
    """
    buckets = {}
    for r in rows:
        buckets.setdefault(r['sub'], []).append(r)
    named = sorted([s for s in buckets if s], key=str.lower)
    out = []
    for s in named:
        items = sorted(buckets[s], key=lambda x: x['name'].lower())
        out.append({'sub': s, 'name': s.split('/')[-1], 'label': s,
                    'depth': s.count('/') + 1, 'count': len(items),
                    'items': items, 'is_root': False})
    if '' in buckets:
        items = sorted(buckets[''], key=lambda x: x['name'].lower())
        out.append({'sub': '', 'name': '散落在根目录', 'label': '散落在根目录',
                    'depth': 0, 'count': len(items), 'items': items,
                    'is_root': True})
    return out


def lib_tag_scan(root, sub='', include_unknown=True):
    """给一个目录（默认整库）里的所有 LoRA 打标签。

    只在请求时算，不写盘 —— 标签随列表一起返回，用户刷新就重算一次。
    实测 250 个文件（要读每个 safetensors 的头）约 1~2 秒，可以接受。
    """
    rels = _walk_models(root) if not sub else [
        r for r in _walk_models(root)
        if norm_rel(r).lower().startswith(norm_rel(sub).lower() + '/')
    ] or ([sub] if safe_join(root, sub) and
           os.path.isfile(safe_join(root, sub)) else [])
    rows = []
    unknown = 0
    for rel in rels:
        info = tag_one(root, rel)
        if not info:
            continue
        if info['cat'] == 'unknown':
            unknown += 1
            if not include_unknown:
                continue
        rows.append(info)

    by_cat = {}
    for r in rows:
        by_cat.setdefault(r['cat_label'], 0)
        by_cat[r['cat_label']] += 1
    # 用户自定义标签的分布（前端统计条要用，可点击筛选）
    by_label = {}
    for r in rows:
        for lb in r.get('labels') or []:
            by_label.setdefault(lb, 0)
            by_label[lb] += 1
    # V0.4：品质等级分布。固定按「劣→上」排序，前端直接顺序渲染，
    # 不要用 dict 顺序 —— 那是随数据变的，会让等级条乱跳。
    by_grade = {k: 0 for k in lora_grade.GRADE_ORDER}
    for r in rows:
        g = r.get('grade')
        if g in by_grade:
            by_grade[g] += 1
    return {
        'rows': rows,
        'total': len(rows),
        'unknown': unknown,
        'by_cat': dict(sorted(by_cat.items(), key=lambda kv: -kv[1])),
        'by_label': dict(sorted(by_label.items(), key=lambda kv: -kv[1])),
        'by_grade': by_grade,
        # V0.4.2：走同一个 helper，保证和 /api/bootstrap 给的是同一份
        'grades': grades_payload(),
        'graded': sum(1 for r in rows if r.get('grade')),
        'labeled': sum(1 for r in rows if r.get('labels')),
        'categories': lora_tags.CATEGORIES,
    }


def lib_tag_set(root, rel, cat):
    """用户手动指定类别，记住。

    注意：选「不确定」只撤销**类别**这一个改动，用户自己加的中文标签要留着
    —— 之前这里直接 pop 整个条目，会把标签一起抹掉。
    """
    if cat not in lora_tags.CAT_BY_KEY and cat != 'unknown':
        return {'error': '不认识的类别：%s' % cat}
    rel = norm_rel(rel)
    if not rel:
        return {'error': '没指定文件'}
    ov = _tag_overrides()
    labels = _labels_of(ov.get(rel))
    if cat == 'unknown':
        # 撤销类别覆盖。只清 cat 字段，**保留用户自己加的中文标签**——
        # 之前这里是 ov.pop(rel)，一次「选不确定」就把标签全抹了。
        entry = ov.get(rel)
        if entry:
            entry.pop('cat', None)
        ov.setdefault(rel, {})['labels'] = labels
    else:
        entry = ov.setdefault(rel, {})
        entry['cat'] = cat
        entry['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        entry['labels'] = labels
    # 清掉空字段 + 空条目，避免 JSON 里堆 'cat': '' 之类的垃圾
    _prune_override(rel, ov)
    save_json(TAGS_PATH, ov)
    info = tag_one(root, rel)
    return {'ok': True, 'info': info, 'overrides': len(ov)}


def lib_grade_set(root, rel, grade):
    """用户手动指定品质等级，记住。传空字符串 = 撤销手动等级。

    只动 grade 这一个字段 —— 类别（cat）和自定义标签（labels）是另两件事，
    之前撤销类别时把整条记录 pop 掉，连带把用户加的标签弄丢了，
    这里不能再犯同样的错。
    """
    rel = norm_rel(rel)
    if not rel:
        return {'error': '没指定文件'}
    g = lora_grade.clean_grade(grade)
    if (grade or '').strip() and not g:
        return {'error': '不认识的品质等级：%s' % grade}
    ov = _tag_overrides()
    entry = ov.setdefault(rel, {})
    if g:
        entry['grade'] = g
        entry['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    else:
        entry.pop('grade', None)
    _prune_override(rel, ov)
    save_json(TAGS_PATH, ov)
    return {'ok': True, 'info': tag_one(root, rel), 'grade': g}


def lib_grade_set_bulk(root, rels, grade):
    """一次给多个文件定级 / 撤销定级（右键菜单批量用）。

    逐个文件独立处理并各自判断成败，最后只落一次盘 ——
    中间失败不会留下写了一半的 JSON。
    """
    g = lora_grade.clean_grade(grade)
    if (grade or '').strip() and not g:
        return {'error': '不认识的品质等级：%s' % grade}
    ov = _tag_overrides()
    ok, fail = 0, []
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    for rel in (rels or []):
        rel = norm_rel(rel)
        if not rel:
            continue
        entry = ov.setdefault(rel, {})
        if g:
            entry['grade'] = g
            entry['at'] = now
        elif entry.get('grade'):
            entry.pop('grade', None)
        else:
            fail.append((rel, '本来就没有定级'))
            continue
        _prune_override(rel, ov)
        ok += 1
    if ok:
        save_json(TAGS_PATH, ov)
    return {'ok': True, 'changed': ok, 'grade': g,
            'skipped': fail[:8], 'skipped_n': len(fail)}


def lib_tag_labels(root, rel, label, mode='add'):
    """给单个文件增删一个用户自定义标签（中文，本地存储）。"""
    rel = norm_rel(rel)
    if not rel:
        return {'error': '没指定文件'}
    name = _clean_label(label)
    if not name:
        return {'error': '标签不能是空的'}
    ov = _tag_overrides()
    entry = ov.setdefault(rel, {})
    labels = _labels_of(entry)
    if mode == 'remove':
        if name not in labels:
            return {'error': '本来就没有「%s」这个标签' % name}
        labels = [x for x in labels if x != name]
    else:
        if name in labels:
            return {'error': '已经有「%s」这个标签了' % name}
        if len(labels) >= MAX_LABELS_PER_FILE:
            return {'error': '每个文件最多 %d 个自定义标签' % MAX_LABELS_PER_FILE}
        labels = labels + [name]
    entry['labels'] = labels
    # 标签清空且没有类别覆盖时，删掉空条目，免得 JSON 里堆垃圾
    entry['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    _prune_override(rel, ov)
    save_json(TAGS_PATH, ov)
    return {'ok': True, 'info': tag_one(root, rel), 'labels': labels}


def lib_tag_labels_bulk(root, rels, label, mode='add'):
    """给多个文件批量加/删同一个自定义标签。"""
    name = _clean_label(label)
    if not name:
        return {'error': '标签不能是空的'}
    ov = _tag_overrides()
    ok, fail = 0, []
    for rel in (rels or []):
        rel = norm_rel(rel)
        entry = ov.setdefault(rel, {})
        labels = _labels_of(entry)
        if mode == 'remove':
            if name not in labels:
                fail.append((rel, '没有这个标签'))
                continue
            labels = [x for x in labels if x != name]
        else:
            if name in labels:
                fail.append((rel, '已经有这个标签'))
                continue
            if len(labels) >= MAX_LABELS_PER_FILE:
                fail.append((rel, '标签数量已满'))
                continue
            labels = labels + [name]
        entry['labels'] = labels
        entry['at'] = time.strftime('%Y-%m-%d %H:%M:%S')
        _prune_override(rel, ov)
        ok += 1
    if ok:
        save_json(TAGS_PATH, ov)
    return {'ok': True, 'changed': ok, 'skipped': fail[:8],
            'skipped_n': len(fail)}


def lib_organize_plan(root, mode='base_cat', sub=''):
    """先算清楚「谁搬到哪」，让用户看清楚再动手 —— 整理是不可逆的大动作。"""
    scan = lib_tag_scan(root, sub)
    out = lora_tags.organize_plan(scan['rows'], mode)
    # 按目标目录汇总，让用户一眼看到会建出哪些文件夹
    buckets = {}
    for p in out['plan']:
        buckets.setdefault(p['dest'], []).append(p)
    out['buckets'] = [{'dest': d, 'count': len(v),
                       'low_conf': sum(1 for x in v if x['conf'] != 'high')}
                      for d, v in sorted(buckets.items())]
    out['low_conf'] = sum(1 for p in out['plan'] if p['conf'] != 'high')
    out['unknown'] = scan['unknown']
    return out


def lib_organize_run(root, mode='base_cat', sub='', only_high=True):
    """真搬。复用 lib_move 的逐个移动 + uniq_dest 改名，绝不覆盖已有文件。"""
    plan = lib_organize_plan(root, mode, sub)['plan']
    if only_high:
        plan = [p for p in plan if p['conf'] == 'high']
    if not plan:
        return {'ok': True, 'moved': 0, 'skipped': 0,
                'note': '没有需要搬的文件' + ('（低置信的已跳过，'
                                              '可勾选「连待确认的一起搬」）'
                                              if only_high else '')}
    root_abs = os.path.abspath(root)
    moved, failed = [], []
    for p in plan:
        dest_dir = safe_join(root, p['dest'])
        if not dest_dir:
            failed.append({'name': p['name'], 'error': '目标路径越界'})
            continue
        src = safe_join(root, p['rel'])
        if not src or not os.path.isfile(src):
            failed.append({'name': p['name'], 'error': '文件已不在原处'})
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            dst = uniq_dest(os.path.join(dest_dir, os.path.basename(src)))
            shutil.move(src, dst)
            moved.append({'name': p['name'],
                          'dest': norm_rel(os.path.relpath(dst, root_abs))})
        except OSError as e:
            failed.append({'name': p['name'],
                           'error': '%s（可能正被 ComfyUI 占用）' % e})
    return {'ok': True, 'moved': len(moved), 'failed': failed,
            'results': moved, 'tree': lib_tree(root_abs)}


# ==================================================================== API
def grades_payload():
    """品质等级表（静态，4 档）。

    单独抽出来是因为它现在有两个消费方：/api/bootstrap（启动时一次性给）
    和 /api/lib/tags（打标签时顺带给）。两处必须是同一份 —— 前端菜单里的
    颜色、统计条里的等级、报告里的徽章全靠它对上。
    """
    return [lora_grade.GRADE_BY_KEY[k] for k in lora_grade.GRADE_ORDER]


def api_bootstrap():
    cfg = load_config()
    diag = preflight(cfg)
    alive = diag['comfy_alive']
    loras = _node_options(cfg, 'LoraLoaderModelOnly', 'lora_name') if alive else []
    groups = {}
    for l in loras:
        g = l.split('\\')[0] if '\\' in l else (l.split('/')[0] if '/' in l else '根目录')
        groups.setdefault(g, []).append(l)
    lr, how, tried = lora_root(cfg, refresh=True)
    return {
        'comfy_alive': alive,
        'comfy_version': diag['comfy_version'],
        'comfy_url': cfg.get('comfy_url', ''),
        'comfy_error': diag['comfy_error'],
        'comfy': comfy_state(),
        'diagnostics': diag,
        'loras': loras,
        'lora_groups': {k: v for k, v in sorted(groups.items())},
        'unets': diag['available']['unets'],
        'current_unet': cfg.get('unet', ''),
        'lib': {
            'root': lr or '',
            'how': how,
            'tried': tried[:8],
            'watch_dirs': (cfg.get('watch_dirs') or default_watch_dirs()),
        },
        'config': {k: cfg.get(k, '') for k in ('unet', 'clip', 'vae', 'clip_type',
                                               'comfy_url', 'comfy_output',
                                               'comfy_root', 'comfy_launch',
                                               'lora_dir', 'lora_node')},
        'defaults': cfg.get('defaults', {}),
        'flags': {'comfy_autostart': bool(cfg.get('comfy_autostart')),
                  'comfy_autostop': bool(cfg.get('comfy_autostop'))},
        'presets': load_json(PRESETS_PATH, []),
        'profiles': profiles_data.profiles_payload(),
        # V0.5：测试工作流清单（内置 + 用户本地）。
        # 跟 profiles 一样属于「开机就该有的东西」—— 下拉框不能等用户
        # 点开才去扫盘，否则第一次展开永远是空的。
        'workflows': _safe_workflows(),
        'tag_categories': lora_tags.CATEGORIES,
        # V0.4.2：品质等级表跟着启动数据一起下发。
        # 等级定义是**静态的**（就4 档，写死在 lora_grade.py 里），
        # 跟库里有多少文件、扫没扫过标签毫无关系 —— 所以它属于启动数据，
        # 不该只在 /api/lib/tags 里给。
        # V0.4.1 的 bug 就是：等级表只在 libTagScan() 里赋值，
        # 于是「刚接入库、还没打标」时 LIB.grades 是空数组，
        # 右键菜单里"品质等级"标题还在、下面一档都没有（实测渲染 0 项），
        # 而用户必须先点一次「自动打标签」才能用。
        'grades': grades_payload(),
        # 底模 key→中文说明，前端悬停时显示（FLUX 这类词翻译反而不认得，
        # 所以保留英文原名 + 一句中文解释）
        'base_desc': dict(lora_tags.BASE_DESC),
        'runs': list_runs(),
    }


def list_runs():
    out = []
    seen = set()
    # V0.5：新位置（ComfyUI output/HX验丹炉）在前，老的 runs/ 兜底；
    # 同名记录只算一次，免得升级后每条历史在列表里出现两遍。
    for root in runs_roots():
        for name in sorted(os.listdir(root), reverse=True):
            if name in seen:
                continue
            p = os.path.join(root, name, 'run.json')
            if os.path.exists(p):
                seen.add(name)
                d = load_json(p, {})
                cells = d.get('cells') or []
                rec = next((c for c in cells
                            if c.get('recommended') and c.get('url')), None)
                # 用当前目录名重建路径，这样历史目录被改过名也不会指向失效地址
                # V0.5.4：run_id 里常有中文（LoRA 名被拼进目录名），
                # 必须 quote 成 %XX，浏览器才会照原样发请求。
                # 漏这步的现场：列表里的小图全裂、点开的大图却正常 ——
                # 大图是 ComfyUI 自己写的相对路径，不经过这个 URL。
                thumb = ('/files/%s/images/%s'
                         % (quote(name), quote(os.path.basename(rec['url'].split('?')[0])))
                         ) if rec else None
                out.append({
                    'run_id': name,
                    'created': d.get('created', ''),
                    'status': d.get('status', ''),
                    # loras 是给人看的 slug，lora_rels 才是身份。
                    # V0.5.4：slug() 会把空格换成下划线（krea2 JUEHUOGE脸模
                    #→ krea2_JUEHUOGE脸模），拿它去库里反查 rel 必然落空，
                    # 表现就是报告最下方说「找不到该 LoRA」、点不动品质标签。
                    # 身份必须给原名（相对 loras 根目录的路径）。
                    'loras': [slug(x) for x in (d.get('spec') or {}).get('loras', [])],
                    'lora_rels': [norm_rel(x) for x in
                                  (d.get('spec') or {}).get('loras', [])],
                    # V0.5.1：组合也报出来。历史列表 / 投放丹房都要知道
                    # 这一批除了逐个 LoRA 还验过哪些组合。
                    'combos': [c['label'] for c in
                               norm_combos(d.get('spec') or {})],
                    'combo_items': [c['items'] for c in
                                    norm_combos(d.get('spec') or {})],
                    'fixed': [slug(f['name']) for f in
                              norm_fixed(d.get('spec') or {})],
                    'fixed_rels': [norm_rel(f['name']) for f in
                                   norm_fixed(d.get('spec') or {})],
                    'triggers': norm_triggers(d.get('spec') or {}),
                    'strengths': (d.get('spec') or {}).get('strengths', []),
                    'best': d.get('best', {}),
                    'summaries': d.get('summaries', {}),
                    'thumb': thumb,
                    'duration': d.get('duration', 0),
                    'has_report': os.path.exists(
                        os.path.join(root, name, 'report.html')),
                })
    return out


def api_history_detail(run_id):
    d = _runs_child(run_id)
    if not d:
        return {'error': 'not found'}
    p = os.path.join(d, 'run.json')
    if not os.path.exists(p):
        return {'error': 'not found'}
    return load_json(p, {})


def _runs_child(run_id):
    """把 run_id 解析成一个存在的产物目录；非法/不存在返回 None。

    V0.5 起产物可能落在 ComfyUI output 下（新的统一位置），也可能还在
    老的 runs/ 里，所以按顺序在几个根目录里找（见 resolve_run_dir）。
    """
    return resolve_run_dir(run_id)


def _count_files(d):
    n = 0
    for _root, _dirs, files in os.walk(d):
        n += len(files)
    return n


def delete_run(run_id):
    """删一条测试记录 —— 连同它名下的测试图一起。

    V0.5：图和报告现在就放在同一个目录里（ComfyUI output/HX验丹炉/<run_id>/），
    所以「鉴定台删除报告」和「删掉 ComfyUI 那边的测试图」是同一件事。
    为了兼容升级前的老记录，几个根目录里同名的都清一遍。
    """
    if JOB and JOB.status == 'running' and JOB.run_id == run_id:
        return {'error': '这条记录正在运行中，请先停止再删除'}
    name = str(run_id or '')
    if not name or os.path.basename(name) != name or name in ('.', '..'):
        return {'error': '找不到这条历史记录'}

    hit, files, dirs = 0, 0, []
    for root in runs_roots():
        full = os.path.normpath(os.path.join(root, name))
        if os.path.dirname(full) != os.path.normpath(root) \
                or not os.path.isdir(full):
            continue
        files += _count_files(full)
        dirs.append(full)
        shutil.rmtree(full, ignore_errors=True)
        hit += 1
    if not hit:
        return {'error': '找不到这条历史记录'}
    return {'ok': True, 'deleted_files': files, 'deleted_dirs': dirs,
            'runs': list_runs()}


def delete_all_runs():
    if JOB and JOB.status == 'running':
        return {'error': '有任务正在运行，请先停止再清空'}
    for root in runs_roots():
        for name in os.listdir(root):
            full = os.path.normpath(os.path.join(root, name))
            if os.path.dirname(full) == os.path.normpath(root) \
                    and os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
    return {'ok': True, 'runs': []}


def _prepare_workflow(job):
    """选了自定义工作流时的起跑前准备：转格式 + 结构自检 + 缓存模板。

    刻意放在点「开始测试」的那一刻做：有问题当场报，别等跑到第一张才报 ——
    用户那时已经等了几十秒，还以为是工具坏了。
    """
    sel = job.workflow or {}
    if str(sel.get('kind') or 'engine') != 'file':
        job.wf_template = None
        return
    cfg = load_config()
    spec = job.spec
    # 用第一格的真实参数试改一遍 —— 这一格能改出来，后面每一格就都能改
    lora = (spec.get('loras') or [''])[0]
    strength = next((float(s) for s in spec.get('strengths') or []
                     if float(s) != 0), 1.0)
    pr = (spec.get('prompts') or [{'text': ''}])[0]
    text = compose_prompt(spec, lora, pr.get('text') or '')
    build_workflow_for(cfg, spec, lora, strength, text,
                       '%s/_preflight' % RUNS_SUBDIR,
                       int(spec.get('seed') or 1), job=job)


def start_scan(spec):
    global JOB
    with JOB_LOCK:
        if JOB and JOB.status == 'running':
            return {'error': '已有任务在运行中'}
        spec['fixed'] = norm_fixed(spec)          # 归一化后再入库/落盘
        spec['triggers'] = norm_triggers(spec)    # 触发词同样只留有效项
        spec['combos'] = norm_combos(spec)        # V0.5.1：组合也归一化后落盘
        n_l, n_c = len(spec.get('loras') or []), len(spec['combos'])
        if n_l == 1 and not n_c:
            tag = slug(spec['loras'][0])
        elif n_l and n_c:
            # 既有逐档扫又有组合：批次名要把两件事都说清楚，
            # 不然历史列表里一条 batch3 根本看不出验的是什么
            tag = 'batch%d+combo%d' % (n_l, n_c)
        elif n_l:
            tag = 'batch%d' % n_l
        else:
            tag = 'combo%d' % n_c
        if spec['fixed']:
            tag += '_fix%d' % len(spec['fixed'])
        spec['run_id'] = '%s_%s' % (
            datetime.now().strftime('%Y%m%d-%H%M%S'), tag)
        job = ScanJob(spec)
        try:
            _prepare_workflow(job)
        except Exception as e:
            bug('准备测试工作流')
            return {'error': '这个工作流用不了：\n%s' % e}
        JOB = job
    t = threading.Thread(target=_scan_guard, args=(job,), daemon=True)
    t.start()
    return {'ok': True, 'run_id': job.run_id,
            'workflow': describe_workflow(spec.get('workflow')),
            'wf_notes': list(job.wf_notes),
            'wf_warnings': list(job.wf_warnings)}


def _scan_guard(job):
    try:
        execute_scan(job)
    except JobStopped:
        job.status = 'stopped'
    except Exception as e:
        bug('执行扫档任务')
        job.status = 'error'
        job.touch(message='出错: %s' % e)


def stop_scan():
    global JOB
    if not JOB or JOB.status != 'running':
        return {'ok': False, 'error': '没有正在运行的任务'}
    JOB.stop_requested = True
    cfg = load_config()
    interrupted(cfg)
    return {'ok': True}


# =============================================================== HTTP 服务
class Handler(BaseHTTPRequestHandler):
    server_version = 'Krea2LoRA-Lab'

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode('utf-8')
        elif isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        n = int(self.headers.get('Content-Length') or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode('utf-8'))
        except Exception:
            return {}

    def do_GET(self):
        p = urlparse(self.path).path
        try:
            if p in ('/', '/index.html', '/run', '/run/', '/hist', '/hist/',
                     '/lib', '/lib/'):
                # V0.5.1：三个板块各占一个地址，都是同一份 index.html。
                # 前端读 location.pathname 决定只渲染哪一块 ——
                # 「独立子界面」是在客户端做的视图隔离，不是三份 HTML：
                # 三份的话这套 3600 行的脚本要复制三遍，改一处漏两处。
                with open(os.path.join(STATIC_DIR, 'index.html'), encoding='utf-8') as f:
                    return self._send(200, f.read(), 'text/html; charset=utf-8')
            if p == '/api/bootstrap':
                return self._send(200, api_bootstrap())
            if p == '/api/ping':
                return self._send(200, {'lab': CLIENT_ID,
                                        'pid': os.getpid()})
            if p == '/api/report':
                # V0.4.3：报错反馈文档。返回纯文本（不是 JSON），
                # 因为用户拿到手就是直接复制粘贴发出去的东西，
                # 中间不该再经过一次"解析 JSON 才能变成人话"的步骤。
                if not lab_diag:
                    return self._send(
                        500, '这台上的 lab_diag.py 不见了，所以生成不了报告。\n'
                             '请重新解压一份完整的分享包。',
                        'text/plain; charset=utf-8')
                return self._send(200, lab_diag.build_report(
                    comfy_state, APP_TITLE, APP_VERSION),
                    'text/plain; charset=utf-8')
            if p == '/api/job':
                if not JOB:
                    return self._send(200, {'status': 'idle'})
                return self._send(200, JOB.snapshot())
            if p == '/api/history':
                return self._send(200, list_runs())
            if p == '/api/comfy/state':
                return self._send(200, comfy_state())
            if p == '/api/lora/trigger':
                # 忘了触发词时用：按 LoRA 名反查本地能找到的所有线索
                name = (parse_qs(urlparse(self.path).query).get('name')
                        or [''])[0]
                cfg = load_config()
                path = find_lora_path(cfg, name)
                if not path:
                    return self._send(200, {'name': name, 'cands': [],
                                            'error': '在 loras 目录里没找到这个文件'})
                probe = probe_model_file(path)
                return self._send(200, {'name': name,
                                        'path': path,
                                        'cands': probe.get('cands') or []})
            if p == '/api/lib/tree':
                cfg = load_config()
                root, how, tried = lora_root(cfg)
                if not root:
                    return self._send(200, {
                        'error': '找不到 ComfyUI 的 loras 目录。\n'
                                 '在「模型设置」里把 comfy_root 填成你的 ComfyUI '
                                 '根目录，或直接用 lora_dir 指定 loras 目录。',
                        'tried': tried})
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, {'root': root, 'how': how,
                                        'tree': lib_tree(root),
                                        'list': lib_list(
                                            root, (q.get('sub') or [''])[0])})
            if p == '/api/lib/list':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, lib_list(root, (q.get('sub') or [''])[0]))
            if p == '/api/profiles':
                # V0.3：分类型测试方案库
                return self._send(200, profiles_data.profiles_payload())
            if p == '/api/workflows':
                # V0.5：重新扫一遍工作流（刚在 ComfyUI 里导出一张，不想重启工具）
                return self._send(200, _safe_workflows())
            if p == '/api/comfy/scan':
                # V0.4.2：界面上那个「自动扫描本机 ComfyUI」按钮。
                # 返回**全部**候选而不是一个答案 —— 机器上装了多个 ComfyUI
                # 是常事，让人自己挑比程序替他猜靠谱。
                # 放在 do_GET 里：前端用的是 get()，挂到 do_POST 会被
                # 静默走空分支（症状是"按钮点了没反应"）。
                try:
                    cands = comfy_candidates()
                except Exception as e:
                    bug('扫描 ComfyUI 候选目录')
                    return self._send(200, {'error': '扫描失败：%s' % e,
                                            'candidates': []})
                cur = (load_config().get('comfy_root') or '').strip()
                return self._send(200, {'candidates': cands, 'current': cur})
            if p == '/api/lib/grades':
                # V0.4.2：单独取品质等级表。
                # 等级是静态的4 档，不依赖库、不依赖 ComfyUI，
                # 所以这个接口不查路径 —— 库还没接上时也能用
                # （那时右键菜单正好最需要它）。
                return self._send(200, {'grades': grades_payload()})
            if p == '/api/lib/index':
                # V0.5.1：整库品质索引（丹房的「品质丹房 / 普通丹房」靠它分组）。
                # 刻意跟 /api/lib/tags 分开：tags 要给每个文件读 safetensors 头
                # 来猜类别，250 个文件要 1~2 秒；丹房只要「谁是什么等级」，
                # 读一次 tag_overrides.json 就够，几百个文件毫秒级返回。
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                try:
                    return self._send(200, lib_grade_index(root))
                except Exception as e:
                    bug('读取品质索引')
                    return self._send(200, {'error': '读不了品质索引：%s' % e})
            if p == '/api/lib/tags':
                # V0.3：给库里的 LoRA 自动打标签
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                q = parse_qs(urlparse(self.path).query)
                sub = (q.get('sub') or [''])[0]
                inc = (q.get('all') or ['1'])[0] != '0'
                try:
                    return self._send(200, lib_tag_scan(root, sub, inc))
                except Exception as e:
                    bug('LoRA 自动打标签')
                    return self._send(200, {'error': '打标签失败：%s' % e})
            if p == '/api/lib/organize':
                # V0.3：按标签一键整理（只算计划，不动文件）
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                q = parse_qs(urlparse(self.path).query)
                return self._send(200, lib_organize_plan(
                    root, (q.get('mode') or ['base_cat'])[0],
                    (q.get('sub') or [''])[0]))
            if p == '/api/lib/dupes':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                try:
                    return self._send(200, {'groups': lib_dupes(root)})
                except Exception as e:
                    bug('LoRA 查重')
                    return self._send(200, {'error': '查重失败：%s' % e})
            if p == '/api/inbox/scan':
                cfg = load_config()
                q = parse_qs(urlparse(self.path).query)
                dirs = q.get('dir')
                try:
                    return self._send(200, scan_inbox(cfg, dirs=dirs))
                except Exception as e:
                    bug('扫描收件箱')
                    return self._send(200, {'error': '扫描失败：%s' % e})
            if p == '/api/inbox/state':
                return self._send(200, move_state())
            if p == '/api/import/list':
                q = parse_qs(urlparse(self.path).query)
                folder = (q.get('dir') or [''])[0]
                cfg = load_config()
                if not folder:
                    folder = (cfg.get('comfy_output') or '').strip()
                imgs = list_dir_images(folder, 96)
                if imgs is None:
                    return self._send(200, {'error': '目录不存在：%s' % folder,
                                            'dir': folder, 'images': []})
                return self._send(200, {'dir': folder, 'images': imgs,
                                        'count': len(imgs)})
            if p == '/api/import/thumb':
                # 仅供本工具（只监听 127.0.0.1）给导入选择器出缩略图
                q = parse_qs(urlparse(self.path).query)
                fp = (q.get('path') or [''])[0]
                if not fp or not os.path.isfile(fp) \
                        or os.path.splitext(fp)[1].lower() not in IMG_EXT:
                    return self._send(404, {'error': 'missing'})
                try:
                    im = Image.open(fp).convert('RGB')
                    im.thumbnail((220, 220), Image.Resampling.LANCZOS)
                    buf = io.BytesIO()
                    im.save(buf, 'JPEG', quality=82)
                    return self._send(200, buf.getvalue(), 'image/jpeg')
                except Exception as e:
                    return self._send(500, {'error': str(e)})
            if p.startswith('/api/history/'):
                return self._send(200, api_history_detail(p.rsplit('/', 1)[1]))
            if p.startswith('/files/'):
                return self._serve_file(unquote(p[7:]))
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            bug('处理 ' + verb + ' 请求：' + str(p))
            return self._send(500, {'error': str(e)})

    def _serve_file(self, rel):
        """把 /files/<run_id>/… 映射到磁盘上的产物目录。

        V0.5：产物可能在新位置（ComfyUI output/HX验丹炉）或老的 runs/，
        两边都要认。URL 形式没变，所以前端和报告页一个字都不用改。
        """
        rel = rel.replace('\\', '/').lstrip('/')
        for root in runs_roots():
            full = os.path.normpath(os.path.join(root, rel.replace('/', os.sep)))
            try:
                if os.path.commonpath([full, root]) != os.path.normpath(root):
                    continue          # 越界，换下一个根目录（不直接 403）
            except ValueError:
                continue
            if os.path.isfile(full):
                ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
                with open(full, 'rb') as f:
                    return self._send(200, f.read(), ctype)
        return self._send(404, {'error': 'missing'})

    def do_POST(self):
        p = urlparse(self.path).path
        body = self._body()
        try:
            if p == '/api/scan':
                spec = body.get('spec') or {}
                err = validate_spec(spec)
                if err:
                    return self._send(200, {'error': err})
                cfg = load_config()
                alive, info = comfy_alive(cfg)
                if not alive:
                    # 没连上就别开跑 —— 否则会一路跑出一堆「提交失败」的空格子。
                    # 开了自动启动就顺手把 ComfyUI 拉起来，前端轮询 /api/comfy/state。
                    if cfg.get('comfy_autostart'):
                        ok, msg = comfy_start(cfg)
                        return self._send(200, {
                            'launching': ok, 'comfy': comfy_state(),
                            'error': None if ok else ('%s\n（也可以手动启动 ComfyUI 后再点一次）' % msg),
                        })
                    return self._send(200, {
                        'error': '连不上 ComfyUI（%s），没有出图条件。\n'
                                 '请先启动 ComfyUI 再点「开始测试」；'
                                 '或在 config.json 里把 comfy_autostart 设为 true，'
                                 '让本工具自己拉起它。' % info})
                return self._send(200, start_scan(spec))
            if p == '/api/comfy/start':
                ok, msg = comfy_start(load_config())
                return self._send(200, {'ok': ok, 'message': msg,
                                        'comfy': comfy_state()})
            if p == '/api/comfy/stop':
                return self._send(200, comfy_stop())
            if p == '/api/rescore':
                run_id = body.get('run_id') or ''
                res, err = rebuild_run(run_id)
                if err:
                    return self._send(200, {'error': err})
                res['runs'] = list_runs()
                return self._send(200, res)
            if p == '/api/rescore_all':
                if JOB and JOB.status == 'running':
                    return self._send(200, {'error': '有任务正在运行，请先停止'})
                okd, bad = [], []
                for r in list_runs():
                    _res, err = rebuild_run(r['run_id'])
                    (bad if err else okd).append(r['run_id'])
                return self._send(200, {'ok': True, 'done': len(okd),
                                        'failed': bad, 'runs': list_runs()})
            if p == '/api/import':
                return self._send(200, import_image(
                    body.get('run_id'), body.get('lora_i'),
                    body.get('prompt_i'), body.get('strength'), body.get('path')))
            if p == '/api/stop':
                return self._send(200, stop_scan())
            if p == '/api/presets':
                presets = load_json(PRESETS_PATH, [])
                name = (body.get('name') or '').strip()
                text = (body.get('text') or '').strip()
                if not name or not text:
                    return self._send(200, {'error': '预设名称和内容都不能为空'})
                presets = [x for x in presets if x.get('name') != name]
                presets.append({'name': name, 'text': text})
                save_json(PRESETS_PATH, presets)
                return self._send(200, {'ok': True, 'presets': presets})
            if p == '/api/presets/delete':
                name = body.get('name')
                presets = [x for x in load_json(PRESETS_PATH, [])
                           if x.get('name') != name]
                save_json(PRESETS_PATH, presets)
                return self._send(200, {'ok': True, 'presets': presets})
            if p == '/api/history/delete':
                return self._send(200, delete_run(body.get('run_id')))
            if p == '/api/history/clear':
                return self._send(200, delete_all_runs())
            if p == '/api/strengths':
                # 记住用户增删后的档位列表，否则刷新页面又会回到 config 里的默认档
                vals = []
                for x in (body.get('strengths') or []):
                    if isinstance(x, bool):
                        continue          # True/False 会被 float() 当 1.0/0.0，直接排除
                    try:
                        v = float(x)
                    except (TypeError, ValueError):
                        continue
                    if v != 0 and v not in vals:
                        vals.append(v)
                vals.sort()
                cfg = load_config()
                d = cfg.get('defaults') or {}
                d['strengths'] = [0] + vals
                cfg['defaults'] = d
                save_json(CONFIG_PATH, cfg)
                return self._send(200, {'ok': True, 'strengths': vals})
            if p == '/api/config':
                cfg = load_config()
                for k in ('unet', 'comfy_url', 'comfy_output', 'comfy_root',
                          'comfy_launch', 'clip', 'clip_type', 'vae', 'lora_node',
                          'lora_dir'):
                    if body.get(k):
                        cfg[k] = body[k]
                if 'watch_dirs' in body:
                    w = body.get('watch_dirs')
                    cfg['watch_dirs'] = [x.strip() for x in w if x.strip()] \
                        if isinstance(w, list) else \
                        [x.strip() for x in str(w or '').split(';') if x.strip()]
                # 路径改了就得重新找一次 loras 目录
                lora_root(cfg, refresh=True)
                for k in ('comfy_autostart', 'comfy_autostop'):
                    if k in body:
                        cfg[k] = bool(body[k])
                save_json(CONFIG_PATH, cfg)
                return self._send(200, {
                    'ok': True, 'comfy': comfy_state(),
                    'config': {k: cfg.get(k, '') for k in
                               ('unet', 'clip', 'clip_type', 'vae',
                                'comfy_url', 'comfy_root', 'comfy_launch')}})
            if p == '/api/inbox/move':
                # 收件箱：把选中的文件移进 loras 的某个子目录
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                sub = (body.get('sub') or '').strip()
                target = safe_join(root, sub)
                if target is None:
                    return self._send(200, {'error': '目标目录越界'})
                paths = body.get('paths') or []
                bad = [p for p in paths
                       if not isinstance(p, str) or not os.path.isfile(p)]
                if bad:
                    return self._send(200, {
                        'error': '有 %d 个文件读不到（可能已经被移动或删除）'
                                 % len(bad)})
                return self._send(200, start_move(paths, target))
            if p == '/api/lib/tag_set':
                # V0.3：用户手动纠正类别
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                return self._send(200, lib_tag_set(root, body.get('rel'),
                                                    body.get('cat')))
            if p == '/api/lib/grade_set':
                # V0.4：用户手动指定品质等级（纯本地，存tag_overrides.json）
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                if body.get('rels'):
                    return self._send(200, lib_grade_set_bulk(
                        root, body.get('rels'), body.get('grade')))
                return self._send(200, lib_grade_set(root, body.get('rel'),
                                                    body.get('grade')))
            if p == '/api/lib/tag_labels':
                # V0.3：用户自定义标签（纯本地，存tag_overrides.json）
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                if body.get('rels'):
                    return self._send(200, lib_tag_labels_bulk(
                        root, body.get('rels'), body.get('label'),
                        body.get('mode') or 'add'))
                return self._send(200, lib_tag_labels(
                    root, body.get('rel'), body.get('label'),
                    body.get('mode') or 'add'))
            if p == '/api/lib/organize_run':
                # V0.3：按标签一键整理（真搬）
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                only_high = body.get('only_high', True)
                return self._send(200, lib_organize_run(
                    root, body.get('mode') or 'base_cat',
                    body.get('sub') or '', bool(only_high)))
            if p == '/api/lib/mkdir':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                r = lib_mkdir(root, body.get('sub') or '', body.get('name'))
                if r.get('ok'):
                    r['tree'] = lib_tree(root)
                return self._send(200, r)
            if p == '/api/lib/rename':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                return self._send(200, lib_rename(root, body.get('rel'),
                                                  body.get('name')))
            if p == '/api/lib/move':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                r = lib_move(root, body.get('rels') or [],
                             body.get('sub') or '',
                             dirs=body.get('dirs') or [])
                if r.get('ok'):
                    r['tree'] = lib_tree(root)
                return self._send(200, r)
            if p == '/api/lib/delete':
                cfg = load_config()
                root, _how, _t = lora_root(cfg)
                if not root:
                    return self._send(200, {'error': '找不到 loras 目录'})
                r = lib_delete(root, body.get('rels') or [])
                if r.get('ok'):
                    r['tree'] = lib_tree(root)
                return self._send(200, r)
            if p == '/api/open':
                target = body.get('path')
                run_dir = os.path.join(RUNS_DIR, body.get('run_id', ''))
                target = os.path.normpath(run_dir) if not target else \
                    os.path.normpath(os.path.join(run_dir, target))
                if os.path.exists(target):
                    os.startfile(target)  # noqa
                    return self._send(200, {'ok': True})
                return self._send(200, {'error': '路径不存在'})
            return self._send(404, {'error': 'not found'})
        except Exception as e:
            bug('处理 ' + verb + ' 请求：' + str(p))
            return self._send(500, {'error': str(e)})


def validate_spec(spec):
    loras = [str(x).strip() for x in (spec.get('loras') or []) if str(x).strip()]
    combos = norm_combos(spec)
    if not loras and not combos:
        return ('请至少选一个「对比 LoRA」，或在「LoRA 组合」里配一组'
                '（2 个以上 LoRA 各自带权重）')
    if not spec.get('prompts'):
        return '请至少填一条提示词'
    try:
        ss = [float(s) for s in (spec.get('strengths') or []) if float(s) != 0]
    except (TypeError, ValueError):
        return '强度档里有非法数值'
    # 强度档是给「对比 LoRA」扫档用的；只跑组合时它没有意义，不强制
    if loras and not ss:
        return '强度档不能只有基线，请至少给一个非 0 档位'
    try:
        steps = int(spec.get('steps', 0))
    except (TypeError, ValueError):
        return '步数必须是整数'
    if steps < 1:
        return '步数必须 ≥ 1'

    fixed = norm_fixed(spec)
    if len(fixed) > 8:
        return '固定 LoRA 最多 8 个，现在有 %d 个' % len(fixed)
    dup = [f['name'] for f in fixed if f['name'] in loras]
    if dup:
        return '同一个 LoRA 不能既固定又参与扫档：%s' % '、'.join(dup)

    # V0.5.1：组合最多 6 组、每组最多 6 层 —— 再多人也记不住哪组是哪组
    if len(combos) > 6:
        return 'LoRA 组合最多 6 组，现在有 %d 组' % len(combos)
    for i, cb in enumerate(combos, 1):
        if len(cb['items']) > 6:
            return '第 %d 组组合有 %d 个 LoRA，最多 6 个' % (i, len(cb['items']))

    # V0.5：选了自定义工作流就要先把文件认下来，别等跑到第一张才报错
    sel = spec.get('workflow') or None
    if sel and str(sel.get('kind') or 'engine') == 'file':
        try:
            resolve_workflow_path(sel.get('path') or '')
        except Exception as e:
            return str(e)

    n = len(spec.get('prompts') or []) * (len(loras) * (len(ss) + 1) + len(combos))
    if n > 400:
        return '任务量过大（%d 张），请减少 LoRA / 组合 / 提示词 / 强度档' % n
    return None


def port_listening(port, timeout=0.35):
    """端口上有没有人在听。用裸 socket，别用 urlopen ——
    某些环境下 urlopen 连不通的本机端口要等满超时才返回，会拖慢启动。"""
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect(('127.0.0.1', port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def running_lab(port):
    """这个端口上是不是已经跑着一个本工具？（而不是别的程序）"""
    if not port_listening(port):
        return False
    try:
        req = urllib.request.Request('http://127.0.0.1:%d/api/ping' % port)
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode('utf-8')).get(
                'lab') == CLIENT_ID
    except Exception:
        return False


def enable_utf8_console():
    """让控制台里的中文提示不乱码。

    Windows 控制台默认代码页是 936(GBK)，而 Python 3 按 UTF-8 写 stdout，
    结果就是下面那些中文提示全变成「娴嬭瘯宸ュ叿」这种乱码 —— 用户看到
    一屏乱码，第一反应是「工具坏了」。这里把控制台输出代码页切成 UTF-8，
    再把 stdout/stderr 的编码同步过去。
    （双击 start.bat 时 bat 也会切一次，这里是双保险：直接用 python 跑也正常。）
    """
    if os.name != 'nt':
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except Exception:
        pass
    for stream in ('stdout', 'stderr'):
        try:
            getattr(sys, stream).reconfigure(encoding='utf-8',
                                             errors='replace')
        except Exception:
            pass


def main():
    enable_utf8_console()
    os.makedirs(RUNS_DIR, exist_ok=True)
    os.makedirs(STATIC_DIR, exist_ok=True)

    # V0.4.3：报错收集。必须在任何可能出错的事之前挂上 ——
    # 挂在main() 开头才能把"启动阶段就崩了"这种最早期失败也记到。
    # 两件事：未捕获异常接到日志上；启动本身记一笔，
    # 这样日志里能看出"这台机器上跑起来过没有"（有的用户报"双击没反应"，
    # 那一例里其实日志文件根本不存在，靠这条就能立刻判断）。
    if lab_diag:
        try:
            lab_diag.install_excepthook()
            lab_diag.record_startup()
        except Exception:
            pass

    cfg = load_config()
    base_port = int(cfg.get('port', 8760))

    fz = boot_anim.Furnace()
    fz.start()
    fz.step('准备中', 10)

    # 已经开着一个就不要开第二个。Windows 允许两个进程绑同一端口，
    # 一旦发生，浏览器会随机连到其中一个 —— 表现就是「我明明改了配置却不生效」。
    # 只探前几个端口，别扫一大片：探不通的端口在有些环境下要等满超时才返回。
    for p in range(base_port, base_port + 3):
        if running_lab(p):
            url = 'http://127.0.0.1:%d/' % p
            print('=' * 60)
            print('  %s 已经在运行：%s' % (APP_TITLE, url))
            print('  直接给你打开浏览器，不重复启动第二个。')
            print('=' * 60)
            if not os.environ.get('LAB_NO_BROWSER'):
                webbrowser.open(url)
            return

    fz.step('检查运行环境', 25)
    # 刚从 GitHub 下载解开的副本没有 config.json —— 先自己把 ComfyUI 找出来，
    # 找到就写进 config.json，之后打开界面就能直接用。
    fz.step('寻找 ComfyUI', 55)
    cfg, found_how = ensure_config()
    if found_how:
        fz.note(found_how)
    maybe_relaunch_with_comfy_python(cfg)

    srv = None
    port = base_port
    for p in range(base_port, base_port + 20):
        try:
            srv = ThreadingHTTPServer(('127.0.0.1', p), Handler)
            port = p
            break
        except OSError:
            continue
    if srv is None:
        print('端口 %d-%d 都被占用，无法启动。' % (base_port, base_port + 19))
        sys.exit(1)

    url = 'http://127.0.0.1:%d/' % port
    fz.step('连接丹炉（大模型）', 80)
    diag = preflight(cfg)
    fz.step('点火开炉', 95)
    ok = '[OK]'
    bad = '[!!]'
    fz.done()
    print('=' * 60)
    print('  %s  已启动' % APP_TITLE)
    print('  界面地址 : %s' % url)
    print('-' * 60)
    print('  %s Python %s' % (ok, diag['python']))
    print('  %s Pillow %s' % (ok if diag['pillow'] else bad,
                              diag['pillow'] or '未安装 —— 拼图与评分会失败'))
    print('  %s ComfyUI %s  %s' % (ok if diag['comfy_alive'] else bad,
                                   cfg.get('comfy_url', ''),
                                   ('v%s' % diag['comfy_version']) if diag['comfy_alive']
                                   else '未连接'))
    if diag['comfy_alive']:
        for k in ('unet', 'clip', 'vae'):
            print('  %s %-5s %s' % (bad if k in diag['missing'] else ok,
                                    k, cfg.get(k) or '(未配置)'))
    for w in diag['warnings']:
        print('  %s %s' % (bad, w))
    if not diag['warnings']:
        print('  一切就绪，去浏览器里勾 LoRA 吧。')
    print('=' * 60)
    # 输出被重定向到文件/管道时 python 是块缓冲，不 flush 的话这段启动
    # 信息要等缓冲区满才出现（真控制台下是行缓冲，看不到差别）
    sys.stdout.flush()
    if not os.environ.get('LAB_NO_BROWSER'):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print('\n已退出。')


def cli_set_comfy_root(root):
    """`python server.py --set-comfy-root <路径>`：把 comfy_root 写进 config.json。

    V0.4.3 新增。start.bat 用 find_comfy.ps1 探到 ComfyUI 之后会调这一下，
    这样 server 启动时就已知根目录，不必再全盘扫一遍。

    两条硬规矩：
      * comfy_root 已经有值就**不动**。用户手填的路径优先于自动探测，
        哪怕探到的是另一个 ComfyUI。
      * 写盘失败只提示、不抛错。这一步是锦上添花，失败不该拦住启动。

    放在 server.py 而不是 bat 里内联 python -c，是因为只有这里知道
    config.json 的结构；两处各写一份，字段名迟早对不上。
    """
    root = (root or '').strip()
    if not root:
        return 1
    if not os.path.isdir(root):
        print('  [跳过] 探到的路径不存在：%s' % root)
        return 1
    try:
        cfg = load_config()
    except Exception as e:                       # 配置坏了不该拦住启动
        print('  [跳过] 读 config.json 失败：%s' % e)
        return 1
    if (cfg.get('comfy_root') or '').strip():
        return 0
    cfg['comfy_root'] = root
    try:
        on_disk = load_json(CONFIG_PATH, None)
        if on_disk is None:
            on_disk = load_json(EXAMPLE_PATH, {})
        if not isinstance(on_disk, dict):
            on_disk = {}
        on_disk.pop('_说明', None)
        on_disk['comfy_root'] = root
        save_json(CONFIG_PATH, on_disk)
    except OSError as e:
        print('  [跳过] 写 config.json 失败：%s' % e)
        return 1
    print('  已记下 ComfyUI 路径：%s' % root)
    return 0


if __name__ == '__main__':
    # V0.4.3：start.bat 探测到 ComfyUI 后调用的写盘子命令。
    # 放在 main() 之前、且只在带这个参数时执行 —— 正常双击启动
    # 完全不经过这里，行为跟以前一模一样。
    if len(sys.argv) >= 3 and sys.argv[1] == '--set-comfy-root':
        sys.exit(cli_set_comfy_root(sys.argv[2]))
    main()

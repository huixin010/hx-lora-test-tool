# -*- coding: utf-8 -*-
"""
LoRA 库自动打标签（V0.3 新增）

解决什么问题：库里有几百个 LoRA，用户点开只看到一个文件名，根本不知道它是
人像还是画风还是加物件的，于是既不敢用也不敢删、更没法整理。

判定方式：**纯本地关键词权重打分**。不联网、不调模型、不猜。
三个信息源一起看，权重从高到低：
  1. 文件名本身（作者最常把类型写在名字里）
  2. safetensors 头里的训练 tag（ss_tag_frequency，作者喂进去的图注）
  3. 所在目录名（很多人已经手工分过一轮了）

输出是「类别 + 依据 + 置信度」。置信度低的不硬猜，标成待确认让用户点一下
选类别 —— 用户选过一次就记在本地（tag_overrides.json），下次不再问。

**界面上所有文字都是中文**（V0.3 优化）：从 Civitai 下载的文件名几乎全是
英文，判定依据如果原样显示 "portrait / skin / detail"，用户根本不知道结论
从哪来的。WORD_CN 把命中的英文词翻成中文，底模名保留英文原名（FLUX / SDXL
这类是行业通用词，翻译反而不认得）但配中文说明。
"""

import re

# ------------------------------------------------------- 英文关键词 → 中文
# 依据、提示、悬停文案里出现的英文一律走这里翻译。译名要让人一眼看懂，
# 不是逐字直译 —— 比如 detail boost 要译成「细节增强」而不是「细节提升」。
WORD_CN = {
    # 人像 / 角色
    'portrait': '人像', 'face': '人脸', 'headshot': '头像', 'facial': '五官',
    'skin': '皮肤', 'beauty': '美颜', 'girl': '女孩', 'woman': '女性',
    'boy': '男孩', 'glamour': '写真风格', 'body': '人体',
    'eye': '眼睛', 'smile': '笑容', 'persona': '人物设定',
    'character': '角色', 'oc': '原创角色', 'anime': '动漫',
    'comic': '漫画', 'costume': '服装角色', 'maid': '女仆',
    'sailor': '水手服', 'jk': '制服', 'persona': '人设',
    # 成人向
    'nsfw': '成人向', 'uncensored': '无限制', 'nude': '裸体',
    'porn': '成人向', 'pussy': '女性部位', 'breast': '胸部',
    'boob': '胸部', 'ass': '臀部', 'nipple': '乳头', 'thigh': '大腿',
    'adult': '成人向',
    # 风格 / 画质
    'style': '画风', 'artstyle': '画风', 'realistic': '写实',
    'illustration': '插画', 'painting': '绘画', 'ink': '水墨',
    'pixelart': '像素风', 'aesthetic': '美学', 'vibe': '氛围',
    'mood': '情绪', 'atmosphere': '氛围', 'mix': '混合',
    'abstract': '抽象', 'quality': '画质', 'detail': '细节',
    'concept': '概念', 'render': '渲染',
    'detail boost': '细节增强', 'detail enhancer': '细节增强器',
    'enhance': '增强', 'enhancer': '增强器', 'improve': '改善',
    'better': '更好', 'boost': '增强', 'fix': '修复',
    'sharpen': '锐化', 'upscale': '放大', 'restore': '修复',
    'denoise': '降噪', 'refine': '精修', 'hires': '高清',
    '4k': '4K 高清', '8k': '8K 超清', 'noise': '噪点',
    # 物品
    'object': '物品', 'item': '物品', 'thing': '物件', 'prop': '道具',
    'vehicle': '载具', 'car': '汽车', 'bike': '摩托车',
    'motorcycle': '摩托车', 'gun': '枪械', 'weapon': '武器',
    'robot': '机器人', 'toy': '玩具', 'wear': '穿戴',
    'wearable': '可穿戴', 'apparel': '服装',
    # 服装
    'outfit': '服装', 'clothing': '衣物', 'costume': '戏服',
    'dress': '连衣裙', 'shirt': '衬衫', 'coat': '外套',
    'hat': '帽子', 'shoe': '鞋', 'jewelry': '首饰', 'glasses': '眼镜',
    'gloves': '手套', 'socks': '袜子',
    # 场景
    'scene': '场景', 'background': '背景', 'architecture': '建筑',
    'building': '建筑', 'city': '城市', 'street': '街道',
    'room': '房间', 'forest': '森林', 'garden': '花园',
    'landscape': '风景', 'place': '地点', 'world': '世界',
    'bridge': '桥梁', 'tower': '高塔', 'ruins': '废墟',
    'place': '地点',
    # 功能 / 效果
    'effect': '特效', 'glow': '发光', 'bloom': '辉光', 'flare': '耀斑',
    'light': '光效', 'volumetric': '体积光', 'particle': '粒子',
    'magic': '魔法', 'fx': '特效', 'energy': '能量',
    'rain': '下雨', 'smoke': '烟雾', 'fire': '火焰',
    'fog': '雾气', 'texture': '材质', 'material': '材质',
    'reflection': '反射', 'relight': '重新打光', 'motionblur': '运动模糊',
    'shadow': '阴影', 'lighting': '光照',
    # 视频
    'video': '视频', 'v2v': '视频转视频', 'i2v': '图转视频',
    't2v': '文生视频', 'i2i': '图生图', 't2i': '文生图',
    'frame': '帧', 'animate': '动画', 'motion': '运动',
    'consistency': '一致性', 'temporal': '时序', 'lightx2v': '轻量视频加速',
    'turbo': '加速', 'lightning': '极速',
    # 工具 / 训练参数
    'distill': '蒸馏', 'distilled': '蒸馏', 'pruned': '剪枝',
    'patch': '补丁', 'sparse': '稀疏', 'quant': '量化',
    'nf4': '4比特量化', 'fp8': '8比特浮点', 'int8': '8比特量化',
    'lcm': '潜空间一致性',  'dpm': '采样器',
    'lightx': '轻量', 'c1-st': '训练步数标记', 'steps': '步数',
    'st5000': '5000步', 'st6000': '6000步', 'st8000': '8000步',
    'st10000': '10000步', '2step': '2步', '4step': '4步',
    '4steps': '4步', '8step': '8步', 'lcs': '一致性采样',
    'c1': '训练步数', 'f1': '变体标记', 'vbvr': '视频一致性',
    '4b': '4B 参数量', '9b': '9B 参数量', '14b': '14B 参数量',
    'a14b': '14B 参数量', 'wan': 'Wan 视频模型', 'wan2.1': 'Wan2.1',
    'wan2.2': 'Wan2.2', 'jc': '素材', '1.5': 'SD1.5',
    'xl': 'SDXL', 
}


def cn(word):
    """把命中的关键词翻成中文。词典里没有的原样返回（多半是中文词）。"""
    if not word:
        return ''
    w = str(word).strip()
    if not w:
        return ''
    # 先整词查，再退回首字母（处理 'oc ' 这类带空格的键）
    if w in WORD_CN:
        return WORD_CN[w]
    low = w.lower()
    if low in WORD_CN:
        return WORD_CN[low]
    if low + ' ' in WORD_CN:
        return WORD_CN[low + ' ']
    # 已经在中文里的直接给
    return w


def cn_list(words, limit=6):
    """命中词列表 → 去重后的中文列表（保持原字段的列表语义）。"""
    out, seen = [], set()
    for w in words or []:
        c = cn(w)
        if c and c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= limit:
            break
    return out


def cn_join(words, limit=4):
    """命中词列表 → 中文短语（去重后用「、」连起来）。"""
    return '、'.join(cn_list(words, limit))

# ---------------------------------------------------------------- 类别定义
# key 是内部 id，label 是给用户看的名字，folder 用于一键整理时的目录名
CATEGORIES = [
    {'key': 'portrait', 'label': '人像', 'folder': '人像',
     'desc': '改脸、五官、肤色、皮肤质感'},
    {'key': 'character', 'label': '角色', 'folder': '角色',
     'desc': '特定角色 / 动漫人物 / 原创 OC'},
    {'key': 'style', 'label': '风格', 'folder': '风格',
     'desc': '画风、笔触、配色倾向'},
    {'key': 'object', 'label': '物品', 'folder': '物品',
     'desc': '教模型认某个具体东西'},
    {'key': 'effect', 'label': '功能', 'folder': '功能',
     'desc': '加光晕、加雨雪、材质等效果'},
    {'key': 'scene', 'label': '场景', 'folder': '场景',
     'desc': '特定环境、建筑、空间'},
    {'key': 'outfit', 'label': '服装', 'folder': '服装',
     'desc': '服装、鞋、首饰、眼镜'},
    {'key': 'quality', 'label': '画质', 'folder': '画质',
     'desc': '提细节、锐化、减伪影'},
    {'key': 'concept', 'label': '概念', 'folder': '概念',
     'desc': '抽象概念 / 视觉体系'},
    # 下面两类是实测真实库后加的：用户库里 271 个文件里有 40 多个是
    # Wan 视频模型的 LoRA（lightx2v / I2V / T2V / distill），硬套「人像/风格」
    # 毫无意义 —— 它们是另一套东西，必须单独归类。
    {'key': 'video', 'label': '视频', 'folder': '视频',
     'desc': '视频模型（ Wan 等）的 LoRA：补帧、提速、动作一致性'},
    {'key': 'utility', 'label': '工具', 'folder': '工具',
     'desc': '蒸馏加速、补丁类，不是画风或人像'},
]
CAT_BY_KEY = {c['key']: c for c in CATEGORIES}
CAT_ORDER = [c['key'] for c in CATEGORIES]

# ------------------------------------------------------------ 关键词权重表
# 命中即累加，分高者胜。中文和英文分开写：中文名和英文名混用很常见。
# 权重 3 = 强信号（几乎就是这个类型）；2 = 中等；1 = 弱（要多个一起中）。
KEYWORDS = {
    'portrait': {
        3: ['portrait', 'face', 'headshot', '人像', '人脸', '写真', '颜值',
            '面部', '脸型', '肖像', 'uncensored', 'nsfw', 'pussy', 'nude',
            '成人', '色情'],
        2: ['skin', 'facial', 'beauty', 'girl', 'woman', 'glamour', 'body',
            'skin', '皮肤', '人物', '模特', '女性', '男性', '脸', 'breast',
            'boob', 'ass', 'porn'],
        1: ['eye', 'smile', '发', '妆', '唇', 'nipple', 'thigh'],
    },
    'character': {
        3: ['character', 'oc ', '角色', '人物设定', '动漫人物', '卡通人物',
            'ip角色', '原创角色'],
        2: ['人物', '角色设计', 'oc人', 'persona', 'boy', 'girl',
            '少年', '少女', '少女战士', 'sailor', 'maid', '女仆'],
        1: ['oc', '动漫', 'anime', '二次元'],
    },
    'style': {
        3: ['style', '画风', '风格', '水彩', '油画', '国画', '水墨', '版画',
            '线稿', '赛璐璐', '厚涂', '插画风', 'pixelart'],
        2: ['illustration', 'painting', 'artstyle', 'render', '插画', '绘画',
            '手绘', '素描', '概念 art', 'artstyle'],
        1: ['anime', 'comic', '写实', 'realistic', 'ink', '素材', '笔刷'],
    },
    'object': {
        3: ['object', 'vehicle', '物件', '物体', '道具', '机械', '机甲',
            '载具', '产品', '摩托', 'motorcycle', '汽车', 'vehicle'],
        2: ['item', 'prop', 'car', 'bike', 'gun', 'weapon', 'robot',
            '摩托', '机车', '飞机', '舰', '船', '家具', '电器', '乐器',
            '花', '植物', '动物', '猫', '狗', '鸟', '建筑'],
        1: ['thing', 'toy', '模型', '摆件'],
    },
    'effect': {
        3: ['effect', '特效', '光效', '光晕', 'glow', 'bloom', '体积光',
            'volumetric', '粒子', 'particle', '柔光', '光影', '逆光',
            '边缘光', '透光', '反光'],
        2: ['magic', 'fx', 'rain', '雨', '雪', '雾', 'fog', 'smoke',
            '烟', '火焰', 'fire', 'energy', '能量', '材质', 'texture',
            'motionblur', '运动模糊', '反射', 'reflection', '透光',
            '光斑', '耀斑', 'flare'],
        1: ['light', '光', '影', 'shadow'],
    },
    'scene': {
        3: ['scene', '场景', '室内', '室内场景', '建筑', 'architecture',
            '背景', 'background', 'landscape', '风景', '城市', 'city',
            'forest', '森林', '街道', 'street'],
        2: ['room', '室内', '房子', '房间', 'bridge', '桥', 'tower', '塔',
            'ruins', '废墟', 'garden', '花园', 'room'],
        1: ['环境', 'place', 'world'],
    },
    'outfit': {
        3: ['outfit', 'clothing', '服装', '衣服', '服饰', 'dress', '裙',
            '汉服', '和服', 'jk', '西装', '校服', '鞋', 'shoe', '靴',
            '帽', 'hat', '眼镜', 'glasses', '首饰', 'jewelry', '帽'],
        2: ['costume', 'wear', 'costume', 'wearable', 'apparel', '衣',
            'wear', '裤', 'coat', '外套', 'shirt', '衬衫', 'dress'],
        1: ['style', '造型'],
    },
    'quality': {
        3: ['quality', '画质', '细节增强', 'detail enhancer', 'detail boost',
            '高清修复', '超分', 'upscale', '锐化', 'sharpen', '画质增强'],
        2: ['quality', '画质', '画质提升', '细节', 'detail', '高清', '修复',
            'restore', '增强', 'enhance', 'improve', 'fix', 'denoise',
            '降噪', '4k', '8k', 'hires'],
        1: ['better', 'boost', 'refine', '精修'],
    },
    'concept': {
        3: ['concept', '概念', 'abstract', '抽象', '体系', '风格化',
            'aesthetic', '美学', '氛围', 'vibe'],
        2: ['mood', 'atmosphere', '主题', '风格 mix', '融合'],
        1: ['mix', '混合'],
    },
    'video': {
        3: ['lightx2v', 'i2v', 't2v', 'v2v', 'video', 'wan2.2', 'wan2.1',
            'animate', 'relight', 'frame', '4step', '4steps', 'distill',
            '视频', '动作一致性'],
        2: ['wan', 'animate', 'c1-st', 'st5000', 'st6000', 'st8000',
            'st10000', 'lcs', 'consistency', '一致性', 'vbvr', 'noise',
            '4b', '9b', '14b', 'a14b'],
        1: ['t2i', 'i2i', 'frame', 'f1'],
    },
    'utility': {
        3: ['distill', 'distilled', 'turbo', 'lightning', 'dpm', '补丁',
            'patch', '加速', 'lcm'],
        2: ['fp8', 'int8', 'nf4', 'quant', 'pruned', 'sparse'],
        1: ['8step', '2step'],
    },
}

# -------------------------------------------------------------- 大模型识别
# 用于一键整理时的第一层目录。用户要求「同大模型下」分类整理。
BASES = [
    ('krea2', 'Krea2', ['krea2', 'krea-2', 'krea 2', 'krea_2', 'krea']),
    ('flux', 'FLUX', ['flux', 'flux1', 'flux.1', 'flux-dev', 'flux-schnell']),
    ('illustrious', 'Illustrious', ['illustrious', 'illust']),
    ('pony', 'Pony', ['pony', 'pony6', 'pony8']),
    ('noobai', 'NoobAI', ['noobai', 'noob', 'noob-ai']),
    ('animagine', 'Animagine', ['animagine', 'animagine-xl']),
    ('sdxl', 'SDXL', ['sdxl', 'xl_base', 'xl-base', 'sdxl_base']),
    ('sd15', 'SD1.5', ['sd15', 'sd1.5', 'sd_1.5', 'v1-5', '1.5']),
    ('sd3', 'SD3', ['sd3', 'sd3-medium', 'stable-diffusion-3']),
    ('hunyuan', 'Hunyuan', ['hunyuan', 'hunyuan-diT']),
    ('wan', 'Wan', ['wan2.2', 'wan2.1', 'wan 2.2', 'wan 2.1', 'wanvideo',
                    'wanlora', 'wan_']),
    ('ltx', 'LTX', ['ltx-video', 'ltxv']),
    ('svd', 'SVD', ['svd_xt', 'stable-video']),
    ('hailuo', 'Hailuo', ['hailuo', 'minimax']),
]

# 底模名保留英文原名（FLUX / SDXL / Pony 是行业通用词，翻译成「弗勒克斯」
# 用户反而认不出、也没法去 Civitai 搜），但配一句中文说明 —— 鼠标悬停就能
# 知道它到底是什么、能不能用在本工具默认的 Krea 2 测试里。
BASE_DESC = {
    'krea2': '本工具默认测试的大模型，兼容性最好',
    'flux': 'FLUX 系大模型，得配 FLUX 底模才能用',
    'illustrious': '偏二次元画风的大模型',
    'pony': '偏二次元的 SDXL 微调模型',
    'noobai': '二次元人物向的大模型',
    'animagine': '偏二次元 / 动漫画风的大模型',
    'sdxl': 'SDXL 系，成熟稳定的大模型',
    'sd15': '老牌 SD1.5，生态最全但分辨率低',
    'sd3': 'SD3 系新一代大模型',
    'hunyuan': '国产大模型（混元）',
    'wan': '视频生成模型，本工具测图片时用不上',
    'ltx': '视频生成模型（LTX），本工具测图片时用不上',
    'svd': '视频生成模型（SVD），本工具测图片时用不上',
    'hailuo': '视频生成模型（海螺），本工具测图片时用不上',
}


def _norm(s):
    """归一化：小写 + 分隔符统一成空格 + 首尾补空格（便于词边界匹配）。"""
    s = (s or '').lower()
    for ch in '_-./\\|()[]{}#@+,':
        s = s.replace(ch, ' ')
    return ' ' + ' '.join(s.split()) + ' '


def _match(text_norm, words):
    """按**词边界**匹配，避免子串误命中。

    之前用 `w in text` 有两个真实 bug：
      · 'car' 命中 'carpet'、'art' 命中 'party'；
      · 归一化把 '1.5' 拆成 '1 5'，导致 SD1.5 永远认不出来。
    中文没有空格分词，所以含中文的词仍走子串（'人像' 出现在任何地方都该算命中）。
    """
    hits = []
    for w in words:
        w = str(w).strip().lower()
        if not w:
            continue
        if re.search(r'[\u4e00-\u9fff]', w):
            if w in text_norm:
                hits.append(w)
            continue
        pat = r'(?<![a-z0-9])' + re.escape(w).replace(r'\ ', r'\s+') + \
              r'(?![a-z0-9])'
        if re.search(pat, text_norm):
            hits.append(w)
    return hits


def detect_base(*texts):
    """从文件名 / 元数据 / 目录名里认底模。认不出返回 ('', '', [])。"""
    blob = _norm(' | '.join(t for t in texts if t))
    for key, label, words in BASES:
        hw = _match(blob, words)
        if hw:
            return key, label, hw[:3]
    return '', '', []


def classify(name, rel_dir='', tags=None, out_name='', base=''):
    """给一个 LoRA 打标签。

    返回 {cat, cat_label, score, runner_up, ratio, confidence, why, tags, base}
    confidence: 'high' 有把握 / 'low' 拿不准（前端标「待确认」）
    why 是给人看的一句依据 —— 让用户能判断机器有没有瞎猜。
    """
    fname = _norm(name)
    ddir = _norm(rel_dir.replace('\\', ' / ').replace('/', ' / '))
    # 训练 tag 和输出名权重折半：它们经常和文件名重复，不能当独立证据叠加
    meta = _norm(' , '.join(list(tags or [])[:14]) + ' , ' + (out_name or ''))

    scores, why_by = {}, {}
    for key, tiers in KEYWORDS.items():
        total, hits = 0.0, []
        # 同一个词表分别打在三个信息源上，权重不同：文件名最可信，
        # 目录名次之（很多人已经手工分过一轮），训练 tag 最弱。
        for weight, src, blob in ((3.0, '名', fname),
                                  (2.1, '目录', ddir),
                                  (1.5, '训练tag', meta)):
            for x in _match(blob, tiers[3]):
                total += weight
                hits.append((src, x))
            for x in _match(blob, tiers[2]):
                total += weight * 0.67
                hits.append((src, x))
            for x in _match(blob, tiers[1]):
                total += weight * 0.33
                hits.append((src, x))
        if total > 0:
            scores[key] = total
            why_by[key] = hits

    # 注意：**不要**因为认出底模就给某类别加权。底模（Krea2/Flux/Pony）和
    # LoRA 类型完全无关 —— 之前加了 0.3 分，结果 'sdxl_motorcycle' 因为名字
    # 里带 sdxl 就被判成人像。底模只用于「一键整理」的第一层目录。
    bkey, blabel, _bh = detect_base(name, rel_dir, ' '.join(tags or []),
                                    out_name, base)

    # 消歧：detail / 细节 / 画质 这类词在「人像细节优化」这种名字里很常见，
    # 它们本身不说明 LoRA 是干什么的。只有当另一个类别有**具体**信号
    # （skin / face / 人像）时，才把通用画质词让位给它 ——
    # 否则 'photorealistic_skin_detail' 会被 'detail' 带去画质分类。
    GENERIC = {'detail', '细节', 'quality', '画质', 'enhance', '增强', 'fix'}
    specific = {}
    for k, s in scores.items():
        if k == 'quality':
            continue
        hits = {w for _src, w in why_by.get(k, [])}
        if not (hits & GENERIC):        # 这一类的命中里没有通用词 = 具体信号
            specific[k] = s
    if 'quality' in scores and specific:
        best_spec = max(specific.items(), key=lambda kv: kv[1])
        # 只在差距不大时才让位：'photorealistic_skin_detail'（skin 3 分 vs
        # detail 2 分）判人像是对的；但如果具体信号很弱，宁可保留画质分类，
        # 也不要把一个明显的画质 LoRA 改判成人像（阈值 1.3 是实测出来的）。
        if best_spec[1] >= 3 and scores['quality'] <= best_spec[1] * 1.3:
            scores.pop('quality')
            why_by.pop('quality', None)

    if not scores:
        return {
            'cat': 'unknown', 'cat_label': '未识别', 'score': 0.0,
            'runner_up': '', 'ratio': 0.0, 'confidence': 'low',
            'why': '文件名、文件夹名和训练标注里都没找到类型线索',
            'base': bkey, 'base_label': blabel, 'tags': [], 'adult': False,
        }

    ranked = sorted(scores.items(), key=lambda kv: -kv[1])
    top, top_s = ranked[0]
    second, second_s = (ranked[1] if len(ranked) > 1 else ('', 0.0))
    ratio = top_s / second_s if second_s > 0 else 99.0

    # 置信度：绝对分要够高，且明显甩开第二名
    if top_s >= 5 and ratio >= 1.6:
        conf = 'high'
    elif top_s >= 3 and ratio >= 1.25:
        conf = 'high'
    else:
        conf = 'low'

    # 底线：**只有一个弱词命中就不给结论**。
    # 实测 '漫转真触发词-Ricklistic' 只靠一个「发」字拿到 1.0 分就被判成人像 ——
    # 这种猜错比不猜更糟：用户会以为标签可信，整理时把文件搬错地方。
    # 1 分 = 只有一个 tier-1 弱词；一个 tier-2 词（2 分）刚好够判。
    WEAK = top_s < 2.0
    hits = why_by.get(top, [])
    seen, parts = set(), []
    for src, w in hits:
        k = (src, w)
        if k in seen:
            continue
        seen.add(k)
        # 来源名也中文化：「名」→「文件名」，「训练tag」→「训练标注」
        src_cn = {'名': '文件名', '目录': '文件夹名',
                  '训练tag': '训练标注'}.get(src, src)
        parts.append('%s含「%s」' % (src_cn, cn(w)))
    why = '、'.join(parts[:4]) if parts else '线索很弱'
    if WEAK:
        return {
            'cat': 'unknown', 'cat_label': '未识别', 'score': round(top_s, 1),
            'runner_up': second, 'ratio': round(ratio, 2), 'confidence': 'low',
            'why': '只有很弱的线索（%s），不敢替你决定类型' % why,
            'base': bkey, 'base_label': blabel,
            'tags': cn_list([w for _s, w in hits], 6),
            'adult': _is_adult(hits),
        }
    if conf == 'low':
        alt = CAT_BY_KEY.get(second)
        # 只有真的有第二名才提它 —— ratio 为 99 表示没有竞争者，
        # 那时候说「和「」分数接近」是 bug（第二名是空字符串）。
        if alt and second:
            why += '；但和「%s」分数接近，可能需要你确认' % alt['label']
        else:
            why += '；把握不大，建议你确认一下'

    return {
        'cat': top,
        'cat_label': CAT_BY_KEY[top]['label'],
        'score': round(top_s, 1),
        'runner_up': second,
        'ratio': round(ratio, 2),
        'confidence': conf,
        'why': why,
        'base': bkey,
        'base_label': blabel,
        'tags': cn_list([w for _s, w in hits], 6),
        'adult': _is_adult(hits),
    }


ADULT_WORDS = {'uncensored', 'nsfw', 'pussy', 'nude', 'porn', 'breast',
               'boob', 'ass', 'nipple', 'thigh', '色情', '成人'}


def _is_adult(hits):
    """成人向内容单独标一个角。

    这类 LoRA 技术上确实属于「改人的」，归到人像没错；但它和人像 LoRA
    的用法完全不同（触发词、审查、能不能公开分享），用户在库里扫一眼
    时需要立刻分辨出来，所以给它一个独立标记而不是混在人像里。
    """
    return any(w in ADULT_WORDS for _s, w in hits)


def suggest_folders(info, mode='base_cat'):
    """按标签算出目标相对目录。

    mode:
      'base_cat'  大模型/类别   （用户点名要的：同大模型下再分功能）
      'cat'       类别
      'base'      大模型
      'label'     用户自定义标签（用户自己写的分类，优先级最高）
    认不出类别 / 底模时返回 ''，调用方负责跳过（宁可不动也不要放错地方）。
    """
    # 自定义标签优先：用户自己写的分类比机器猜的准。
    if mode == 'label':
        labels = info.get('labels') or []
        return labels[0] if labels else ''

    cat = CAT_BY_KEY.get(info.get('cat') or '')
    if not cat:
        return ''
    bkey = info.get('base') or ''
    blabel = info.get('base_label') or ''
    if mode == 'cat':
        return cat['folder']
    if mode == 'base':
        return blabel or ''
    if mode == 'base_cat':
        if not blabel:
            return cat['folder']
        return '%s/%s' % (blabel, cat['folder'])
    return ''


def _already_in(rel, dest):
    """判断文件是不是已经待在合适的目录里。

    不能只比「路径以 dest 开头」—— 用户的实际习惯是把两级用连字符写成一个
    文件夹名，比如 `Krea2-风格/`。实测真实库里就是这么分的，如果只认
    `Krea2/风格/`，一键整理会把 271 个文件全部判成"需要搬"，计划里
    一条都没有却显示"没需要搬的"，前后矛盾。

    规则：路径的**任一层**等于目标里的任一层（忽略大小写），就算已就位。
    只比一层，跨盘搬迁那种 `风格/xxx/Krea2-风格` 的情况也能认出来。
    """
    parts = [p.strip().lower() for p in
             rel.replace('\\', '/').split('/')[:-1] if p.strip()]
    want = [p.strip().lower() for p in dest.split('/') if p.strip()]
    if not want:
        return False
    joined = ''.join(parts)                       # 'krea2 风格'
    flat = ''.join(want)                          # 'krea2 风格'
    for p in parts:
        if p in want:
            return True
    # 连写形式：Krea2-风格 / Krea2功能 / krea2_功能
    for w in want:
        if w in joined:
            return True
    return flat in joined and len(want) == 1


def organize_plan(rows, mode='base_cat'):
    """算出「谁该搬到哪」。

    rows: lib_tag_scan() 的行 —— 扁平结构，每行本身就是 classify() 的结果
    （带 rel / name）。也兼容 {'rel':..,'info':{..}} 的旧包法。
    返回 {plan:[{rel, name, dest, cat_label, conf}], skipped:[{rel,reason}]}
    已经在目标目录里的直接跳过 —— 反复点「一键整理」不会来回搬。
    """
    plan, skipped = [], []
    for r in rows:
        # 扁平行（server.py 实际传的就是这种）与嵌套行都支持
        info = r.get('info') if isinstance(r.get('info'), dict) else r
        dest = suggest_folders(info, mode)
        if not dest:
            reason = ('没加自定义标签，先不搬' if mode == 'label'
                      else '认不出类别或底模，先不搬')
            skipped.append({'rel': r.get('rel', ''), 'reason': reason})
            continue
        cur = (r.get('rel') or '').replace('\\', '/')
        if _already_in(cur, dest):
            skipped.append({'rel': r['rel'],
                            'reason': '已经在「%s」里了' % dest})
            continue
        plan.append({'rel': r['rel'], 'name': r.get('name', ''), 'dest': dest,
                     'cat_label': info.get('cat_label', ''),
                     'conf': info.get('confidence', 'low'),
                     'base_label': info.get('base_label', '')})
    return {'plan': plan, 'skipped': skipped, 'mode': mode}

# -*- coding: utf-8 -*-
"""
测试方案库（V0.3 新增）—— 按 LoRA 类型给出「怎么测」

为什么要按类型分：不同类型的 LoRA，最适 strength 差一个数量级，测试的
标准也不同。用一套固定档位去测所有 LoRA，会得出「这个 LoRA 崩了」这种
错误结论：

  · 概念/功能 LoRA（加物件、加效果）0.2 就该生效，测到 0.8 必然崩坏；
  · 人像/风格 LoRA 0.3 几乎看不出变化，要到 0.7~0.9 才认得出来；
  · 角色 LoRA 必须带触发词，不带就测不出任何东西。

每条方案 = 类型 + 一句话说明 + 推荐档位 + 标准测试提示词 + 判读要点。
提示词全部按 Krea 2 的习惯写：英文自然语言长句、只写肯定句、单一视觉母题
（堆一串视觉符号会让模型把注意力平均分配，反而测不出 LoRA 的特征）。

RANK_BANDS 是「什么样的 LoRA 该被测到什么档位」，是给用户看的经验值：
  concept  概念/物件    0.2~0.8
  effect   功能/效果    0.2~0.7
  style    画风/风格    0.4~1.0
  portrait 人像/真人    0.4~1.0
  character 角色/OC     0.5~1.0
  scene    场景/建筑    0.3~0.9
  item     物品/服装    0.3~0.9
  quality  画质/细节    0.3~0.8
"""

# 每个方案至少一条标准测试提示词；弱项再补一条针对性补充提示词。
PROFILES = [
    {
        'id': 'portrait',
        'name': '人像 LoRA',
        'kind': 'portrait',
        'cat': '人像',
        'summary': '改脸、改五官、改肤色、改皮肤质感的 LoRA。',
        'how': '用固定同一个人物、同一套光的提示词横向比。判读重点是「像不像本人」'
               '和「脸有没有糊」——强度往上走，人脸最先崩的就是眼睛和牙齿。',
        'band': '0.4 ~ 1.0',
        'strengths': [0.4, 0.6, 0.8, 1.0],
        'size': '768 x 1024',
        'steps': 8,
        'focus': ['五官有没有变（这是 LoRA 生效的直接证据）',
                  '皮肤质感是变好还是变塑料',
                  '眼睛、牙齿有没有糊掉（崩坏的第一个信号）'],
        'prompts': [
            {'label': '人像·中性光（标准）',
             'text': 'A young East Asian woman sits by a window in soft afternoon light, '
                     'wearing a plain grey knit top. Her head is turned slightly to the left, '
                     'her eyes look toward something outside the frame, and her expression '
                     'is calm. A plain wall fills the background, kept soft and unobtrusive '
                     'so the face carries all the attention.'},
            {'label': '人像·强侧光（看皮肤质感）',
             'text': 'A close-up of a young man standing in hard side light, most of his '
                     'face falling into shadow while the lit edge of his cheek and brow '
                     'stays bright. Skin texture is clearly visible, with pores and fine '
                     'hairs catching the light. The background is a dark, empty interior.'},
        ],
    },
    {
        'id': 'character',
        'name': '角色 / OC LoRA',
        'kind': 'character',
        'cat': '角色',
        'summary': '某个具体角色、动漫人物、原创 OC 的 LoRA。',
        'how': '必须填触发词（工具能自动从 safetensors 里找回），否则测出来的是'
               '「什么都没发生」。提示词里不要再描述这个角色的外貌特征 —— 让 LoRA '
               '自己提供，否则会和 LoRA 打架。',
        'band': '0.5 ~ 1.0',
        'strengths': [0.5, 0.7, 0.85, 1.0],
        'size': '768 x 1024',
        'steps': 8,
        'focus': ['触发词是否真的生效（不生效时画面和基线几乎一样）',
                  '角色特征是否稳定（不同提示词下是否还是同一个人）',
                  '高强度下会不会带进训练集的背景或构图'],
        'prompts': [
            {'label': '角色·半身（标准）',
             'text': 'A young woman stands in a quiet library aisle, seen from the waist up. '
                     'She wears a dark uniform jacket over a white shirt, and her hands rest '
                     'on the edge of a wooden shelf. Soft light falls from a window somewhere '
                     'off to one side, and the rows of books behind her blur into a warm brown '
                     'bokeh.'},
            {'label': '角色·全身（看服装是否被带进来）',
             'text': 'A slim young man stands on a rooftop at dusk, seen head to toe. He wears '
                     'a long dark coat that reaches his ankles, and his hair moves slightly in '
                     'the wind. The sky behind him is a deep blue fading to orange near the '
                     'horizon, and city lights begin to appear far below.'},
        ],
    },
    {
        'id': 'style',
        'name': '风格 / 画风 LoRA',
        'kind': 'style',
        'cat': '风格',
        'summary': '改整体画风、笔触、配色倾向的 LoRA。',
        'how': '用「内容中性」的提示词测 —— 主体越具体，风格越容易被内容盖住。'
               '判读看三件事：线条/笔触有没有变、配色有没有变、'
               '以及高强度下细节是不是糊成一片。',
        'band': '0.4 ~ 1.0',
        'strengths': [0.4, 0.6, 0.8, 1.0],
        'size': '1024 x 1024',
        'steps': 8,
        'focus': ['笔触和线条的变化（最明显）',
                  '整体色调有没有被带偏',
                  '细节在最高档是否糊掉（风格 LoRA 崩起来很慢但很狠）'],
        'prompts': [
            {'label': '风格·街景（标准）',
             'text': 'A quiet street market in the late afternoon, with several stalls under '
                     'striped awnings and a few people walking between them. A single warm '
                     'street lamp has just come on, and the wet cobblestones reflect it. '
                     'The whole scene is rendered in one consistent artistic style.'},
            {'label': '风格·静物（看笔触）',
             'text': 'A weathered wooden table holds a chipped enamel mug, a folded cotton '
                     'cloth, and three dried persimmons. Morning light rakes across the '
                     'surface from the left, revealing the grain of the wood and a thin '
                     'layer of dust.'},
        ],
    },
    {
        'id': 'object',
        'name': '物品 / 物件 LoRA',
        'kind': 'object',
        'cat': '物品',
        'summary': '教模型认某个具体东西：某款车、某种机械、某类道具。',
        'how': '让这个东西**单独出现**，配上简单背景。测的是「认不认得出」和'
               '「结构对不对」，不是好看不好看。物品 LoRA 的强度上限往往比'
               '人像低得多，超过 0.8 常常直接把画面带跑偏。',
        'band': '0.3 ~ 0.8',
        'strengths': [0.3, 0.5, 0.65, 0.8],
        'size': '1024 x 1024',
        'steps': 8,
        'focus': ['结构对不对（这是物品 LoRA 崩掉的主要形式）',
                  '材质表现（金属/塑料/玻璃有没有做出来）',
                  '0.3 就已经认得出的话，说明不需要更高档'],
        'prompts': [
            {'label': '物品·单体（标准）',
             'text': 'A single vintage motorcycle stands on an empty asphalt lot, seen from '
                     'the side at a slight angle. The fuel tank is a deep red with a thin '
                     'cream stripe, and the chrome exhaust pipe runs low along the right side. '
                     'The background is a flat grey concrete wall, and the light is flat and '
                     'overcast.'},
            {'label': '物品·使用场景',
             'text': 'A worn leather work bag rests on a wooden bench under a railway '
                     'platform roof, with its strap hanging over one side. Dim light falls '
                     'from a single bulb above, and the platform edge and tracks recede into '
                     'the dark behind it.'},
        ],
    },
    {
        'id': 'effect',
        'name': '功能 / 效果 LoRA',
        'kind': 'effect',
        'cat': '功能',
        'summary': '给画面加某种效果的 LoRA：加光晕、加雨雪、加体积光、加胶片颗粒。',
        'how': '这是**最容易误判**的一类：效果 LoRA 在低强度就完全生效，'
               '用常规档位去测会一路测到崩坏，然后误判成「这个 LoRA 不好」。'
               '务必从 0.2 起步。判读看「效果出现了没有」和「画面有没有被动摇」。',
        'band': '0.2 ~ 0.7',
        'strengths': [0.2, 0.35, 0.5, 0.7],
        'size': '1024 x 1024',
        'steps': 8,
        'focus': ['效果有没有出现（这是主判据）',
                  '主体有没有被效果淹没（0.5 之后要盯这个）',
                  '有没有出现训练集自带的痕迹（如固定的光斑位置）'],
        'prompts': [
            {'label': '功能·逆光（标准，适合测光效类）',
             'text': 'A person stands between tall trees with the sun low behind them, so '
                     'their whole body reads as a dark silhouette. Strong light bursts through '
                     'the gaps between the trunks, and dust drifts slowly through the beams. '
                     'The ground is covered with dry leaves.'},
            {'label': '功能·夜景（适合测光晕/辉光类）',
             'text': 'A quiet city intersection after rain, seen from a slightly elevated '
                     'angle. Wet asphalt mirrors the red tail lights of a few stopped cars, '
                     'and vapor rises from a manhole in the middle of the crossing. '
                     'A street lamp glows overhead and lights the falling rain.'},
        ],
    },
    {
        'id': 'scene',
        'name': '场景 / 建筑 LoRA',
        'kind': 'scene',
        'cat': '场景',
        'summary': '某个特定场景、某种建筑、某种环境类型的 LoRA。',
        'how': '用中性内容 + 该类型的典型视角。场景 LoRA 主要改的是空间感和'
               '材质，判读看透视是否合理、材质是否统一。',
        'band': '0.3 ~ 0.9',
        'strengths': [0.3, 0.5, 0.7, 0.9],
        'size': '1024 x 768',
        'steps': 8,
        'focus': ['透视是否合理（场景 LoRA 崩在结构上）',
                  '材质是否统一（不要一半写实一半插画）',
                  '内容有没有被 LoRA 强行替换掉'],
        'prompts': [
            {'label': '场景·建筑（标准）',
             'text': 'A narrow stone alley in an old town after rain, with wet cobblestones '
                     'reflecting a single warm street lamp. Laundry lines cross overhead '
                     'between the walls, and the sky above the alley is deep blue.'},
            {'label': '场景·室内',
             'text': 'An empty reading room with tall wooden shelves along both walls, lit '
                     'only by a single window at the far end. Dust drifts in the slanting '
                     'light, and the floor is a worn stone tile.'},
        ],
    },
    {
        'id': 'outfit',
        'name': '服装 / 配饰 LoRA',
        'kind': 'item',
        'cat': '服装',
        'summary': '某种服装、鞋子、首饰、眼镜的 LoRA。',
        'how': '用半身或全身，让衣物占足够画面。判读重点是「结构有没有穿歪」——'
               '袖子、领口、肩带这类地方最容易出错。',
        'band': '0.3 ~ 0.9',
        'strengths': [0.3, 0.5, 0.7, 0.9],
        'size': '768 x 1024',
        'steps': 8,
        'focus': ['衣物结构是否合理（袖子/领口/肩带）',
                  '材质表现（针织/皮革/雪纺的区别）',
                  '有没有把人物特征一起改掉（越界了）'],
        'prompts': [
            {'label': '服装·全身（标准）',
             'text': 'A woman stands against a plain grey wall, seen head to toe. She wears a '
                     'long wool coat with a wide collar and a row of buttons down the front, '
                     'the hem reaching her mid-calf. The light is even and soft, with no '
                     'strong shadow.'},
            {'label': '服装·半身特写',
             'text': "A close-up of a man's shoulders and chest, wearing a heavy knit "
                     'sweater with a thick rolled collar. The wool texture is clearly '
                     'visible, and the background is a dark, out-of-focus interior.'},
        ],
    },
    {
        'id': 'quality',
        'name': '画质 / 细节增强 LoRA',
        'kind': 'quality',
        'cat': '画质',
        'summary': '提细节、加锐度、减伪影的 LoRA。',
        'how': '这类 LoRA 几乎不改变构图，判读只能靠**局部放大**和工具给出的'
               '锐度指标。跑完报告后把最高档和基线并排放大看边缘。',
        'band': '0.3 ~ 0.8',
        'strengths': [0.3, 0.5, 0.65, 0.8],
        'size': '1024 x 1024',
        'steps': 8,
        'focus': ['边缘是否更实（放大看，别只看缩略图）',
                  '有没有出现过锐化光晕',
                  '肤色是否被推得过硬（过增强的典型症状）'],
        'prompts': [
            {'label': '画质·毛发/织物（标准）',
             'text': 'A close-up of a long-haired cat sitting on a windowsill, seen from the '
                     'side. Individual hairs along its back and ears catch the light, and the '
                     'texture of the woven curtain behind it is clearly resolved.'},
            {'label': '画质·建筑边缘',
             'text': 'A modern glass building rises against a pale sky, photographed from '
                     'below at a slight angle. The edges of each floor and the reflections in '
                     'the glass are crisp and clearly separated.'},
        ],
    },
    {
        'id': 'concept',
        'name': '概念 / 抽象风格 LoRA',
        'kind': 'concept',
        'cat': '概念',
        'summary': '把某个抽象概念或特定视觉体系注入模型的 LoRA。',
        'how': '这类 LoRA 通常需要触发词，而且效果是「整体气质」而不是某个具体'
               '物体。判读看它是否带进了该有的视觉元素，同时有没有把画面带偏。',
        'band': '0.2 ~ 0.8',
        'strengths': [0.2, 0.4, 0.6, 0.8],
        'size': '1024 x 1024',
        'steps': 8,
        'focus': ['目标视觉元素是否出现',
                  '有没有引入不想要的训练集特征',
                  '内容主体是否还能被正常识别'],
        'prompts': [
            {'label': '概念·中性场景（标准）',
             'text': 'An empty road runs straight through an open plain toward a low line of '
                     'hills. A single person stands on the road in the middle distance, small '
                     'in the frame. The sky takes up most of the image, and the light is even '
                     'and slightly cool.'},
            {'label': '概念·人像检验',
             'text': 'A woman stands in an open field facing the camera, her hair moving '
                     'slightly in the wind. She wears a plain dark jacket, and the sky behind '
                     'her is bright and washed out.'},
        ],
    },
]

# 供前端下拉框用：按「测试对象」分组显示
PROFILE_GROUPS = [
    ('最常用的四类', ['portrait', 'style', 'object', 'effect']),
    ('人相关', ['character', 'outfit']),
    ('场景与画质', ['scene', 'quality']),
    ('其他', ['concept']),
]


def profile_by_id(pid):
    for p in PROFILES:
        if p['id'] == pid:
            return p
    return None


def profiles_payload():
    """给前端的完整方案库（带 id 索引方便前端直接取）。"""
    return {
        'profiles': PROFILES,
        'groups': [{'title': t, 'ids': ids} for t, ids in PROFILE_GROUPS],
        'bands': {p['id']: p['band'] for p in PROFILES},
    }

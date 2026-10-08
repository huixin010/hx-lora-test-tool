# -*- coding: utf-8 -*-
"""LoRA 品质等级：劣品 / 下品 / 中品 / 上品。

沿用炼丹的意象（工具叫「验丹炉」），等级名用户一眼能懂：
连出图都不行 = 劣品，能用但没效果 = 下品，能用 = 中品，又稳又好 = 上品。

颜色按用户要求从低到高：白 → 绿 → 蓝 → 紫。
注意白色在白底上等于看不见，所以每个等级都**同时给一个浅色底**
（标签是「彩色字 + 同色系浅底」），白字那档底色改成中灰，才读得出来。

定级依据全部来自报告里已经算好的指标，不重新测量：
  - 有没有可用档（verdict == 'bad' → 直接劣品）
  - 推荐档的画质比值（sharp_ratio）
  - 风格偏移量级（drift）
  - 有没有档位崩坏（crashed）

分级门槛写在 GRADE_RULES 里，一眼能调。
"""

# key, 名称, 字色, 浅底色, 一句话说明
GRADES = [
    ('rej',  '劣品', 'var(--hx-grade-rej-fg)', 'var(--hx-grade-rej-bg)', '没有一个档位能用'),
    ('low',  '下品', 'var(--hx-grade-low-fg)', 'var(--hx-grade-low-bg)', '能用但效果很弱，或画质反而下降'),
    ('mid',  '中品', 'var(--hx-grade-mid-fg)', 'var(--hx-grade-mid-bg)', '正常可用，是这个LoRA 的真实水平'),
    ('top',  '上品', 'var(--hx-grade-top-fg)', 'var(--hx-grade-top-bg)', '效果明显、画质不掉、没有崩坏档'),
]
GRADE_BY_KEY = {k: {'key': k, 'label': lb, 'fg': fg, 'bg': bg, 'desc': d}
                for k, lb, fg, bg, d in GRADES}
# 前端下拉框的固定顺序：从低到高
GRADE_ORDER = [k for k, _, _, _, _ in GRADES]
# 「未定级」用的空值
NO_GRADE = ''


def grade_keys():
    return list(GRADE_ORDER)


def clean_grade(v):
    """把外部传入的等级收拾成合法 key，非法一律当「未定级」。"""
    v = (v or '').strip()
    return v if v in GRADE_BY_KEY else NO_GRADE


def grade_info(key):
    return GRADE_BY_KEY.get(clean_grade(key))


def grade_of(summary):
    """按报告指标自动定级。summary 就是 summarize_lora() 的返回值。

    返回 (key, 理由) —— 理由会显示在报告里，用户能看到凭什么给这个级。
    """
    if not summary:
        return NO_GRADE, '没有可用的测试数据'

    verdict = summary.get('verdict')
    best = summary.get('best')
    sharp = summary.get('sharp_ratio')
    drift = summary.get('drift') or 0.0
    crashed = summary.get('crashed') or []

    # 1) 一个档都用不了 → 劣品，没有再看的必要
    if best is None or verdict == 'bad':
        return 'rej', '全部档位崩坏，没有可用强度'

    # 2) 生效太弱（几乎看不出差别）→ 下品。常见于 LoRA 训练不足或漏了触发词。
    if summary.get('effect') == 'none' or drift < 0.03:
        return 'low', '风格偏移仅 %.2f，几乎看不出变化' % drift

    # 3) 画质明显掉（比基线糊了）→ 下品
    if sharp is not None and sharp < 0.9:
        return 'low', '画质比基线低 %.0f%%，越用越糊' % abs((sharp - 1) * 100)

    # 4) 偏移过大 = 改得太狠，LoRA 压过了底模，不是好东西。
    #    这条必须排在「上品」前面，否则画质好的强力 LoRA 会被误判成上品。
    if drift >= 0.40:
        return 'mid', '风格偏移 %.2f，改动过大，已压过底模' % drift

    # 5) 有崩坏档 → 最高只能到中品
    if crashed:
        return 'mid', '可用，但 %s 档崩坏' % '、'.join('%g' % x for x in crashed)

    # 6) 生效明显 + 画质不掉（≥基线）→ 上品
    if drift >= 0.10 and (sharp is None or sharp >= 0.9):
        why = '风格偏移 %.2f' % drift
        if sharp is not None:
            why += '，画质比基线%s' % ('持平' if sharp < 1.005
                                      else '高 %.0f%%' % ((sharp - 1) * 100))
        return 'top', why + '，无崩坏档'

    # 7) 其余可用情况 → 中品
    return 'mid', '可用，风格偏移 %.2f（幅度偏小）' % drift


def badge_html(key, extra_cls='', title=''):
    """报告页与库页面共用的等级标签 HTML（颜色走内联，前端零CSS 依赖）。"""
    g = grade_info(key)
    if not g:
        return ''
    t = (' title="%s"' % title.replace('"', '&quot;')) if title else ''
    return ('<span class="qgrade %s"%s style="color:%s;background:%s">%s</span>'
            % (extra_cls, t, g['fg'], g['bg'], g['label']))

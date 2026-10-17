"""只提取参数，不读取或修改已锁定的 L2。"""
import re

from .negation import affirmative_text


def fill_slots(message):
    slots = {'prompt': message, 'tier': 'preview'}
    positive = affirmative_text(message)
    for pattern, style in [(r'水彩|watercolor', 'watercolor'), (r'油画|oil painting', 'oil_painting'),
                           (r'赛博朋克|cyberpunk', 'cyberpunk'), (r'动画风|动漫风|anime.style', 'anime')]:
        if re.search(pattern, positive, re.I):
            slots['style'] = style
    duration = re.search(r'(\d+(?:\.\d+)?)\s*(?:秒|s\b|seconds?\b)', message, re.I)
    if duration:
        slots['duration_sec'] = float(duration[1])
    else:
        chinese = re.search(r'(十二|十一|十|[一二三四五六七八九])\s*秒', message)
        if chinese:
            slots['duration_sec'] = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12}[chinese[1]]
    ratio = re.search(r'(?<!\d)(16:9|9:16|1:1|4:3|3:4)(?!\d)', message)
    if ratio:
        slots['ratio'] = ratio[1]
    if re.search(r'抠|去背景|去除背景|透明底|remove.*(?:bg|background)|cut out', positive, re.I):
        slots.update(op='matting', background='transparent')
    slots['ref_role'] = 'first_frame'
    return slots

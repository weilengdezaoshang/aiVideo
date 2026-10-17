"""确定性门闩。一般说法仍由例句向量定 L2。"""
import re

from .negation import affirmative_text, negative_clauses

WEAK_MOTION = r'动起来|活一点|活一[些点]|生动一些|动一动|动态的感觉|弄动一点|眨一下|\banimate\b|more alive|bit of motion'
NEGATIVE_VIDEO = r"(?:别|不要|不需要|无需|不做|不生成|别生成|暂时不要)(?:把.{0,12})?(?:做成|生成|任何)?\s*(?:视频|动画|短片)|(?:no|without)\s+(?:video|animation)|(?:don't|do not)\s+(?:make|generate|create).{0,12}(?:video|animation)"
VIDEO = r'(?:做成|生成|输出|转成|改成|改为|做个|做|出|制作).{0,12}(?:视频|短片)|(?:make|generate|create|output|convert).{0,30}\b(?:video|clip)\b|图生视频|文生视频|\d+\s*秒.{0,12}(?:广告|视频|短片)'


def apply_gates(message, feasibility, *, direct_i2v=False):
    image = 'image_edit' if feasibility.has_image else 'text_to_image'
    positive = affirmative_text(message)
    # ‘动画风格’被否定不等于拒绝视频；否定分句不能再触发强视频门闩。
    rejected_video = any(re.search(r'视频|短片|动画(?!风|样式)|\b(?:video|animation(?![-\s]+style)|clip)\b', clause, re.I)
        for clause in negative_clauses(message))
    if rejected_video and re.search(VIDEO, positive, re.I):
        return None, ['video_negation_conflict']
    if rejected_video or re.search(r'只要静图|只要图片|only.*still', message, re.I):
        return image, ['negative_video']
    if re.search(r'动画风格|动画风|动漫风|anime.style', message, re.I) and not re.search(VIDEO, positive, re.I):
        return image, ['style_as_art']
    if re.search(r'^(?:你好|谢谢|好的|hi|hello|thanks)[!！。 ]*$', message, re.I):
        return 'other', ['greeting']
    if re.search(r'^(?:分析|检查|解释|画布上|帮我梳理)|先问清楚', message):
        return None, ['meta_clarify']
    if re.search(VIDEO, positive, re.I):
        if feasibility.has_image:
            direct = direct_i2v and re.search(r'直接图生视频|一步.*(?:I2V|图生视频)|one.step.*(?:video|i2v)', message, re.I)
            return 'image_to_video' if direct else 'image_to_multiframe', ['strong_video']
        return 'text_to_video', ['strong_video']
    if re.search(WEAK_MOTION, message, re.I):
        return None, ['weak_motion']
    return None, []

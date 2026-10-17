"""按分句隔离否定作用域，供确定性门闩和操作槽位共同使用。"""
import re

NEGATION = r"不要|不用|不需要|无需|(?<![分区识告类鉴])别|不准|不得|不必|不是要|不做|不生成|不输出|不制作|不转换|不抠|不去|不移除|不删除|不想|不能|禁止|\b(?:do not|don't|never|without|no)\b"


def affirmative_text(message):
    """删除否定分句；逗号、转折和‘只’引出的新要求重新开始作用域。"""
    clauses = re.split(r"[，,。；;！？!?\n]|(?:但是|但|而是|只要|只是|只把|仅把)|\b(?:but|instead)\b", message, flags=re.I)
    return '；'.join(clause for clause in clauses if not re.search(NEGATION, clause, re.I))


def negative_clauses(message):
    clauses = re.split(r"[，,。；;！？!?\n]|(?:但是|但|而是|只要|只是|只把|仅把)|\b(?:but|instead)\b", message, flags=re.I)
    return [clause for clause in clauses if re.search(NEGATION, clause, re.I)]

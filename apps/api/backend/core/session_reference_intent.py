"""Recognize explicit references only; creative request understanding stays in the planner."""

import re


def reference_intent(text: str, *, session_id: str, batch_id: str | None = None) -> dict | None:
    """Return resolver query fields. No assets, geometry, or permissions come from text.

    Multiple explicit conflicting references require clarification rather than picking
    the first regex match. The caller resolves the returned query against DB assets.
    """
    found = []
    if re.search(r"刚才(?:那|的|生成)|刚刚(?:那|的|生成)|上一张|上一个|最近(?:那|的)|latest|last one", text, re.I):
        found.append({"kind": "latest"})
    for word, direction in ((r"左边|左侧|最左|left", "left"), (r"右边|右侧|最右|right", "right")):
        if re.search(word, text, re.I):
            found.append({"kind": "spatial", "spatial": direction})
    ordinals = re.findall(r"第\s*([0-9]+|[一二两三四五六七八九十]+)\s*(?:张|个|幅|段)", text)
    digits = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    for token in ordinals:
        number = int(token) if token.isdigit() else digits.get(token)
        if number is None and token.count("十") == 1:
            tens, ones = token.split("十")
            if (not tens or tens in digits) and (not ones or ones in digits):
                number = (digits[tens] if tens else 1) * 10 + (digits[ones] if ones else 0)
        if number is not None and number > 0:
            found.append({"kind": "ordinal", "ordinal": number, "batch_id": batch_id})
    if not found:
        if re.search(r"选中(?:的|那)|当前(?:这|选中)|selected", text, re.I):
            found.append({"kind": "selected"})
        else:
            return None
    distinct = {tuple(sorted(item.items())) for item in found}
    if len(distinct) != 1:
        raise ValueError("ambiguous_reference_intent")
    return {"caller_session_id": session_id, **found[0]}

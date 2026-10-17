"""按不同 L2 的边距判置信度，避免同类近重复挤掉真正竞争意图。"""
def decide_from_hits(hits, strong=0.82, margin=0.12, minimum=0.55):
    if not hits or hits[0]['score'] < minimum:
        return None, 'none'
    top = hits[0]
    competing = next((hit['score'] for hit in hits if hit['l2'] != top['l2']), -1)
    if top['score'] >= strong and top['score'] - competing >= margin:
        return top['l2'], 'strong'
    return None, 'weak'

"""批量重建；线上请求永不更新向量权重或写索引。"""
from .retriever import approved_items, library_digest


async def build_index(library, embedder):
    items = approved_items(library)
    if library.get('embed_model_id') != embedder.model_id:
        raise ValueError('例句库与侧车模型版本不一致')
    # 结构标签必须与查询一致；同一例句不跨结构偷换标签。
    texts = [f"{item['text']}\n[has_image={str(item['l2'] in {'image_edit', 'image_to_multiframe', 'image_to_video'}).lower()}]" for item in items]
    vectors = []
    for start in range(0, len(texts), 64):
        vectors.extend(await embedder.embed(texts[start:start + 64]))
    return {'embed_model_id': embedder.model_id, 'exemplars_version': library['exemplars_version'],
        'library_digest': library_digest(library), 'vectors': vectors}

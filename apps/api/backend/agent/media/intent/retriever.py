"""小规模批准例句库：批量索引、版本校验、可行集合内余弦检索。"""
import hashlib
import json
import math
from pathlib import Path
import yaml
from .embedder import EmbedError, validate_vectors

DEFAULT_LIBRARY = Path(__file__).with_name('exemplars.yaml')


def load_library(path=DEFAULT_LIBRARY):
    value = yaml.safe_load(Path(path).read_text(encoding='utf-8'))
    if not isinstance(value, dict) or not isinstance(value.get('items'), list):
        raise EmbedError('例句库格式无效')
    ids = [item['id'] for item in value['items']]
    if len(ids) != len(set(ids)):
        raise EmbedError('例句 ID 重复')
    return value


def approved_items(library):
    items = [item for item in library['items'] if item.get('approval') == 'approved' and
            item.get('approved_by') and not item.get('evaluation_only') and item.get('l2')]
    for item in items:
        if item['l2'] not in {'text_to_image', 'image_edit', 'text_to_video', 'image_to_multiframe', 'image_to_video', 'other', 'unsupported'} or not isinstance(item.get('text'), str) or not item['text'].strip():
            raise EmbedError('批准例句的标签或原话无效')
    return items


def library_digest(library):
    return hashlib.sha256(json.dumps(library, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Retriever:
    def __init__(self, library, vectors):
        self.library = library
        self.items = approved_items(library)
        if len(self.items) > 2000:
            raise EmbedError('批准例句超过暴力检索上限')
        self.vectors = validate_vectors(vectors, len(self.items)) if self.items else []
        if len(vectors) != len(self.items):
            raise EmbedError('索引与批准例句不一致')

    @classmethod
    def from_cache(cls, library_path, index_path, model_id):
        library = load_library(library_path)
        index = json.loads(Path(index_path).read_text(encoding='utf-8'))
        if (index['library_digest'] != library_digest(library) or index['embed_model_id'] != model_id or
                index['exemplars_version'] != library['exemplars_version'] or library.get('embed_model_id') != model_id):
            raise EmbedError('索引版本不一致，请重新索引')
        return cls(library, index['vectors'])

    def search(self, vector, possible, limit=5):
        validate_vectors([vector], 1)
        hits = []
        for item, candidate in zip(self.items, self.vectors):
            if item['l2'] not in possible:
                continue
            if len(vector) != len(candidate):
                raise EmbedError('查询与索引维度不一致')
            score = sum(a*b for a, b in zip(vector, candidate)) / math.sqrt(sum(x*x for x in vector)*sum(x*x for x in candidate))
            hits.append({'id': item['id'], 'l2': item['l2'], 'score': score, 'tags': item.get('tags', [])})
        # 每类保留最相似例句，防止同标签近重复遮住真正的第二候选。
        diverse = {}
        for hit in sorted(hits, key=lambda hit: (-hit['score'], hit['id'])):
            diverse.setdefault(hit['l2'], hit)
        return list(diverse.values())[:limit]

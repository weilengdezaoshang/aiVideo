"""结构可行性与部署能力分离；能力关闭不得挤掉视频候选。"""
from .types import Feasibility

CAPABILITIES = {
    'text_to_image': ('text_to_image',), 'image_edit': ('image_edit',),
    'text_to_video': ('text_to_video',), 'image_to_video': ('image_to_video',),
    'image_to_multiframe': ('image_edit', 'frames_to_video'),
}


def build_feasibility(has_image, catalog, *, direct_i2v=False):
    possible = ['text_to_image', 'text_to_video', 'other', 'unsupported']
    if has_image:
        possible += ['image_edit', 'image_to_multiframe']
        if direct_i2v:
            possible.append('image_to_video')
    available = {}
    for l2, caps in CAPABILITIES.items():
        available[l2] = bool(catalog.config.enabled and all(catalog.entries.get(cap) and
            catalog.entries[cap].enabled and catalog.entries[cap].binding and
            'preview' in catalog.entries[cap].tiers for cap in caps))
    return Feasibility(possibleL2=possible, has_image=has_image, capabilityOk=available)

import type { Inspiration } from '../ui/media-cards.js'

export const inspiration: Inspiration[] = [
  {
    id: 'sea',
    title: '海风来信',
    image: '/workspace/assets/sailboat.png',
    category: '电影感',
    ratio: '4:3',
    aspect: '1.36',
    prompt: '一艘橙色帆船驶过蔚蓝海面，远处是薄雾中的群岛，电影感，自然光。',
  },
  {
    id: 'flower',
    title: '微观花园',
    image: '/workspace/assets/flower.png',
    category: '自然',
    ratio: '3:4',
    aspect: '1',
    prompt: '一朵半透明的杏橙色花朵，细腻的花瓣纹理，深青色虚化背景，微距摄影，自然侧光。',
  },
  {
    id: 'perfume',
    title: '光的容器',
    image: '/workspace/assets/perfume.png',
    category: '产品',
    ratio: '1:1',
    aspect: '1.15',
    prompt:
      '琥珀色玻璃香水瓶置于蜂蜜色砂岩上，温暖的自然光，细腻的玻璃折射，高级产品摄影，无文字。',
  },
  {
    id: 'creature',
    title: '森林来客',
    image: '/workspace/assets/creature.png',
    category: '插画',
    ratio: '3:4',
    aspect: '1',
    prompt:
      '一只毛茸茸的白色小兽栖息在森林的苔藓上，林间光束，细腻毛发，柔和的电影感，原创幻想生物。',
  },
  {
    id: 'ocean',
    title: '潮汐之间',
    image: '/landing/assets/ocean.jpg',
    category: '自然',
    ratio: '16:9',
    aspect: '1.2',
    prompt: '俯拍碧绿色海面，细密白色浪花，安静舒展的自然纹理，航拍摄影。',
  },
  {
    id: 'architecture',
    title: '沙丘里的留白',
    image: '/landing/assets/architecture.jpg',
    category: '电影感',
    ratio: '3:4',
    aspect: '.85',
    prompt: '无边沙丘中的极简混凝土建筑，柔和阴天，温暖沙色，建筑摄影，宁静的电影画面。',
  },
  {
    id: 'still-life',
    title: '一束日常',
    image: '/landing/assets/flower.jpg',
    category: '产品',
    ratio: '3:4',
    aspect: '.95',
    prompt: '琥珀色玻璃瓶里的一枝橙色鲜花，浅灰背景，留白构图，自然窗光，静物摄影。',
  },
  {
    id: 'green',
    title: '向光而生',
    image: '/landing/assets/plant.jpg',
    category: '自然',
    ratio: '3:4',
    aspect: '1.2',
    prompt: '自然光中的绿色植物，柔和的叶片阴影，清新安静的氛围，细腻植物摄影。',
  },
]
export function filterInspiration(
  items: Inspiration[],
  category: string,
  query: string,
): Inspiration[] {
  const text = query.trim().toLocaleLowerCase()
  return items.filter(
    (item) =>
      (category === '精选' || item.category === category) &&
      `${item.title} ${item.category} ${item.prompt}`.toLocaleLowerCase().includes(text),
  )
}

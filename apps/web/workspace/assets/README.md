# 首页独立图片素材

2026-09-10，使用内置 imagegen，为创作首页生成4张独立示例图片。无第三方外部API。图像主题对应V2设计稿，非截图裁片，不含UI文本。来源原图仍保存在Codex generated_images目录，本目录为项目使用副本。

每张提示词均追加：Standalone production website gallery image asset. Edge-to-edge image, no border, no UI, no text, no watermark, no collage. High quality.

- sailboat.png: Cinematic photograph, landscape 4:3. A small sailboat with vivid coral-orange triangular sail on sparkling turquoise Mediterranean sea, airy hazy layered mountainous islands in the background, natural midday sunshine. Boat near lower middle, generous sea and sky, elegant film still.
- flower.png: Fine art macro photograph, portrait 4:5. A single translucent pale apricot-orange poppy flower with luminous thin glasslike delicate petals and fine orange veins, delicate dark stamens, against deep teal softly blurred background. Large blossom fills frame, beautiful directional sunlight.
- perfume.png: Luxury product photograph, square. A sculptural round amber glass perfume bottle with brass atomizer and no text or branding, placed on a honey-colored sandstone slab, monumental rough sandstone backdrop, warm sunlight and long shadow, glass refraction. Bottle fills middle of frame.
- creature.png: Cinematic fantasy illustration with photoreal fur, portrait 4:5. One tiny adorable round white furry woodland creature with small dark eyes and small ears, resting in glowing moss in a lush forest, soft natural sunbeams, dark emerald bokeh. Charming original creature, detailed white fur, fill lower center.

画面比例和裁切由 Inspiration 数据与 CSS object-fit 控制。默认延迟加载，详情读取原图。未来提供真实视频时另加视频源，不以静态图片配播放图标。

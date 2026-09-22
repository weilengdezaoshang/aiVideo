# 帧屿集项目官网

静态站点入口为 `index.html`，样式为 `cinematic.css`。从仓库根目录运行 `python3 -m http.server 7802 --bind 127.0.0.1 --directory website` 可本地预览。

GitHub Pages 工作流只发布 `website/`，不打包配置、密钥或运行数据。修改推送至 `main` 后自动发布到 https://weilengdezaoshang.github.io/aiVideo/ 。

官网展示新版云模型平台的产品方向。公开仓库的应用仍是早期实现，新版代码尚未同步；不要在网页添加无法由当前公开代码执行的新版安装命令。

主视觉 `assets/dream-ocean.jpg` 使用内置 image_gen 原创生成；其他图片和 FRAYUNE 标识复用项目设计资产。镜头动效可暂停，减少动态偏好会关闭动画。页面不使用第三方即梦素材。

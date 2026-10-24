# 固定中文字体

由父代理为本块准备的未修改 Noto Sans SC Regular（约 8 MB）。来源与 SHA-256 见 source.json，许可证见 OFL.txt，保留字体内嵌版权。后端在 Docker 的 backend 目录随代码分发，无需运行时下载。建议默认 font_id=noto-sans-sc，使用此文件做中文实际测试；浏览器应从白名单字体只读接口加载同一文件。无须新增 fontTools 依赖，若需要 cmap 可使用已有解析代码。不要使用系统专有中文字体作为 CI 依赖。

上游：https://github.com/notofonts/noto-cjk/tree/main/Sans/SubsetOTF/SC

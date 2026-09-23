# 智能体头像

把你生成好的扁平插画头像放进这个目录，按 **智能体 ID** 命名即可被自动加载：

```
<智能体ID>.png        例如 default.png、coder.png、writer.png
```

- 规格：1024×1024、透明 PNG、头肩居中。
- 文件名里的 ID 必须和 QwenPaw 里该智能体的 `id` 完全一致（可在 `/api/agents` 看到）。
- 放好后重新 `npm run build` 即生效。
- 没有对应文件时，自动回退到「首字母 + 主题色块」头像。

办公室背景大图（可选）放在上一级 `src/assets/office-bg.png`（或 .jpg），缺省时使用 CSS 渐变地板。

# 铜减量化研究工作台设计规范

> 让调研、审核、确定性计算与结果分析在一条可追溯的工作流中连续完成。

## 1. Visual Theme & Atmosphere

**Style**：专业研究工作台

**Keywords**：克制、清晰、证据、连续、精确、低干扰、可审阅

**Tone**：理性且安静；避免营销化、游戏化、装饰性过强。

**Feel**：像一张持续展开的研究底稿，步骤和结论始终在同一视野中。

**Interaction Tier**：L1 精致静态

**Dependencies**：CSS 与原生 JavaScript。

## 2. Color Palette & Roles

```css
:root {
  --bg: #f6f7f5;
  --surface: #ffffff;
  --surface-alt: #eef2ef;
  --surface-hover: #f8faf8;
  --border: #dbe3de;
  --border-hover: #a7b9af;
  --text: #18231d;
  --text-secondary: #526158;
  --text-tertiary: #748278;
  --accent: #176b4d;
  --accent-hover: #0f5139;
  --bg-rgb: 246, 247, 245;
  --accent-rgb: 23, 107, 77;
  --success: #167447;
  --error: #b93a3a;
  --warning: #9a6817;
}
```

- 所有颜色使用变量；组件中不直接写色值。
- 绿色只表示工作流推进、已确认结果和主操作。
- 警告与错误仅用于审核状态，不作为常规装饰。

## 3. Typography Rules

```css
@import url('https://fonts.googleapis.com/css2?family=Noto+Sans+SC:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');
```

| Role | Font | Size | Weight | Line Height | Letter Spacing |
|---|---|---:|---:|---:|---:|
| 页面标题 | Noto Sans SC | 24px | 700 | 1.35 | 0.01em |
| 区块标题 | Noto Sans SC | 16px | 700 | 1.45 | 0.01em |
| 卡片标题 | Noto Sans SC | 14px | 600 | 1.5 | 0.01em |
| 正文 | Noto Sans SC | 15px | 400 | 1.75 | 0.02em |
| 标签 | Noto Sans SC | 12px | 600 | 1.5 | 0.03em |
| 数值与 ID | JetBrains Mono | 12px | 500 | 1.5 | 0 |

- 标题不使用渐变、投影或全大写。
- 禁止使用图标字体、花体和过细字号表达研究结论。

## 4. Component Stylings

### Buttons

```css
.btn { border: 1px solid transparent; border-radius: 8px; min-height: 40px; padding: 9px 16px; transition: background .18s ease, border-color .18s ease, transform .18s ease; }
.btn.primary { background: var(--accent); color: var(--surface); }
.btn.primary:hover { background: var(--accent-hover); transform: translateY(-1px); }
.btn.primary:active { transform: translateY(0); }
.btn.ghost { background: var(--surface); border-color: var(--border); color: var(--text); }
.btn.ghost:hover { background: var(--surface-hover); border-color: var(--border-hover); }
.btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.btn:disabled { opacity: .46; cursor: not-allowed; transform: none; }
```

### Cards and stage panels

```css
.card, .stage-panel { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; box-shadow: 0 1px 2px rgba(var(--accent-rgb), .04); }
.stage-panel:hover { border-color: var(--border-hover); }
.stage-panel.is-active { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(var(--accent-rgb), .10); }
.stage-panel:focus-within { border-color: var(--accent); }
```

### Stage rail, tags and links

```css
.stage-rail { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.stage-tab { background: var(--surface-alt); border: 1px solid var(--border); color: var(--text-secondary); border-radius: 8px; min-height: 44px; }
.stage-tab[aria-current='step'] { color: var(--accent); background: var(--surface); border-color: var(--accent); }
.tag { border-radius: 999px; padding: 3px 8px; color: var(--text-secondary); background: var(--surface-alt); }
a { color: var(--accent); text-decoration-thickness: 1px; text-underline-offset: 3px; }
a:hover { color: var(--accent-hover); }
```

## 5. Layout Principles

- 页面最大宽度为 1600px，整体内边距为 24px。
- 桌面端采用 320px 配置栏与自适应工作台；配置栏不再独占半屏。
- 工作台依次放置：研究范围、四阶段进度轨、当前阶段面板、证据与结果。
- 默认只展开当前阶段；已完成阶段以可折叠摘要保留。
- 间距采用 8/12/16/24/32px 节奏；卡片内边距 20px。

```css
.app-shell { max-width: 1600px; margin: 0 auto; display: grid; grid-template-columns: 320px minmax(0, 1fr); gap: 24px; padding: 24px; }
.workbench { min-width: 0; display: grid; gap: 16px; }
.stage-content { display: grid; gap: 16px; }
```

## 6. Depth & Elevation

| Level | Treatment | Use |
|---|---|---|
| Flat | 无阴影，边框 | 表格、折叠内容 |
| Subtle | 1px 边框与极浅阴影 | 常规卡片 |
| Active | 强调色外环 | 当前操作阶段 |
| Overlay | 仅使用实体背景和边框 | 审核提示与错误消息 |

不使用大面积毛玻璃、浮夸投影或漂浮卡片堆叠。

## 7. Animation & Interaction

**Motion Philosophy**：用短暂位移和透明度变化标记流程推进，不让动画掩盖数据。

```css
.reveal { opacity: 0; transform: translateY(10px); transition: opacity .28s ease, transform .28s ease; }
.reveal.in-view { opacity: 1; transform: translateY(0); }
.stage-panel { transition: border-color .18s ease, box-shadow .18s ease, background .18s ease; }
```

```js
const observer = new IntersectionObserver(entries => entries.forEach(entry => {
  if (entry.isIntersecting) { entry.target.classList.add('in-view'); observer.unobserve(entry.target); }
}), { threshold: .12 });
document.querySelectorAll('.reveal').forEach(element => observer.observe(element));
```

- 阶段完成时只更新状态色和摘要，不播放循环动画。
- 当前阶段自动滚入可视区域；用户手动展开的内容不被强制收起。

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { animation-duration: .01ms !important; transition-duration: .01ms !important; scroll-behavior: auto !important; }
}
```

## 8. Do's and Don'ts

### Do

- 保留调研与减量化的版本边界，但把它们放在同一工作台。
- 在进入计算前显式展示功能单位、基准强度与来源。
- 用阶段摘要呈现已完成内容，避免重复占用页面。
- 让审核、计算与结果状态可一眼分辨。
- 表格和证据链接优先于装饰图形。

### Don't

- ❌ 不创建第二个页面或跳转到单独的减量化界面。
- ❌ 不把模型推理文本当作研究结论的视觉焦点。
- ❌ 不用渐变标题、霓虹色或大面积动效表现“智能”。
- ❌ 不把基准强度输入隐藏在结果之后。
- ❌ 不让审核通过后丢失调研结果的可见摘要。
- ❌ 不用颜色单独传达状态；同时提供文字。
- ❌ 不在桌面端把配置栏做得比工作台更宽。
- ❌ 不在移动端保留双栏布局或制造横向滚动。

## 9. Responsive Behavior

| Name | Width | Key Changes |
|---|---:|---|
| Desktop | > 1100px | 左侧配置栏与右侧连续工作台 |
| Tablet | 641–1100px | 配置栏置顶，阶段轨保持两列 |
| Mobile | ≤ 640px | 单列、阶段轨两列、结果表可横向滚动 |

触摸目标最小 44px。移动端将配置、审核、计算输入和结果按当前阶段纵向展开。

```css
@media (max-width: 1100px) { .app-shell { grid-template-columns: 1fr; } .panel-left { position: static; max-height: none; } }
@media (max-width: 640px) { .app-shell { padding: 12px; gap: 12px; } .stage-rail { grid-template-columns: repeat(2, minmax(0, 1fr)); } .result-header { align-items: flex-start; flex-direction: column; gap: 12px; } }
```

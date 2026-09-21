# GitHub Issue 标准格式（固化自 Portfonia 项目惯例，2026-09-21 起）

本项目此前的 issue 格式是"问题描述在 body，排查过程+权衡在第一条 comment，设计和契约约束在第二条 comment"（2 段式，见 issue #69 等历史记录）。2026-09-21（issue #72）起改用从 Portfonia 项目（`~/Portfonia`）学来的 5 段式标准，更适合有实际设计权衡、需要区分"要做什么"和"怎么做"的 issue。简单的一次性技术债/纯 bug 修复仍可酌情用旧的 2 段式，不强制。

## 结构

**Issue body（简短，顶层概览）**：

```markdown
## Summary

一段话说清楚：现象 + 根因（如果已经排查清楚）+ 为什么值得做。

## In scope

- 本 issue 具体要改的点，逐条列出。

## Out of scope

- 明确排除的边界，防止实现时范围蔓延。

## Links

（parent issue / 相关 issue，没有就写"无 parent，说明触发来源"）

[Requirements](评论链接) | [Reasons](评论链接) | [Exploration](评论链接) | [Design](评论链接) | [Contract constraints](评论链接)

Detail → comments: Requirements, Reasons, Exploration, Design, Contract constraints.
```

**5 条独立 comment**，每条以对应的 `## <Section>` 标题开头：

1. **Requirements** — 精确的、可核对的需求点，逐条列出（不是叙事）。如果涉及代码改动，可以直接贴伪代码/函数签名。
2. **Reasons** — 为什么要做这件事：根因、触发场景、真实证据（日志/复现步骤/用户反馈原话）。
3. **Exploration** — 排查过程记录：读了哪些代码、验证了什么假设、推翻了什么错误归因、关键数据/日志摘录。这一段是"留给自己和未来 session 的排查笔记"，可以比 Reasons 更细节化。
4. **Design** — 具体技术方案：改哪个文件、哪个函数、大致代码结构。如果还没有完整方案（占位型 issue），如实写"待实现 session 设计"，不要为了填满格式编造方案。允许在结尾列"待实现 session 决策的开放点"，标注哪些细节留给以后判断，不要在这一步替后面的人武断拍板。
5. **Contract constraints** — 明确不变的边界、既有行为约束、预算/性能上限、下游影响范围（是否需要重启常驻进程等）。

## 创建顺序（避免评论链接是死链）

1. 先用占位 Links（如"（无 parent，见下方 comments）"）创建 issue，拿到 issue number。
2. 依次发 5 条 comment，记录每条返回的评论 URL（`#issuecomment-<id>`）。
3. 用 `gh issue edit` 把 body 的 Links 段落替换成真正的 5 个锚点链接。

## 何时用哪种格式

- **5 段式（本文档）**：有实际设计权衡、多个改动点、需要区分"为什么/怎么排查/怎么做/边界在哪"的 issue。
- **旧 2 段式**（body=问题描述，comment1=排查+权衡，comment2=设计+契约）：单一根因的简单 bug 修复，没有真正的设计分支需要讨论。

不确定选哪种时，默认用 5 段式——多写几个标题的成本远低于事后想不起来"这个决定当时为什么这么定"。

## 参考实例

- 本项目 issue #72（异动归因搜索精度不足）：完整 5 段式落地案例。
- Portfonia 项目 issue #539（Vigil resumable Setup/Activate flow）：更复杂的多阶段设计案例，Exploration 段落包含生产环境复现证据和带日期的决策记录（"Decisions recorded 2026-09-19"）。

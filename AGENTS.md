# AGENTS.md

本项目的 agent 规则只维护一份，就是仓库根目录的 **`CLAUDE.md`**。开始工作前先完整读一遍，按它执行。本文件只作指针，不重复写规则。

补充说明（写给非 Claude 的 agent，例如 Codex）：
- `CLAUDE.md` 要求先读两份 Obsidian 文档。没有 Obsidian MCP 时，直接读本地文件，vault 根目录是 `$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview/`。
- `CLAUDE.md` 里提到的 Claude 专属工具（Skill、memory、EnterWorktree、MemPalace MCP 等），换成你自己的等价做法；没有等价物就跳过。规则本身照样遵守：TDD、原子写入、禁止 emoji、不写死用户名路径、git 与 worktree 纪律、不擅自合并或发起 review。
- 详细背景看这些文件：`docs/design.md`、`docs/PITFALLS.md`、`docs/playbooks/`。旧版 AGENTS.md 的原文存档在 `docs/playbooks/agents_md_legacy_202605.md`，内容已过时，只供溯源。

# AGENTS.md

本项目的 agent 规则只维护一份，就是仓库根目录的 **`CLAUDE.md`**。开始工作前先完整读一遍，按它执行。本文件只作指针，不重复写规则。

补充说明（写给非 Claude 的 agent，例如 Codex）：
- `CLAUDE.md` 要求先读两份 Obsidian 文档。没有 Obsidian MCP 时，直接读本地文件，vault 根目录是 `$HOME/Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview/`。
- `CLAUDE.md` 里提到的 Claude 专属工具（Skill、memory、EnterWorktree、MemPalace MCP 等），换成你自己的等价做法；没有等价物就跳过。规则本身照样遵守：TDD、原子写入、禁止 emoji、不写死用户名路径、git 与 worktree 纪律、不擅自合并或发起 review。
- 详细背景看这些文件：`docs/design.md`、`docs/PITFALLS.md`、`docs/playbooks/`。旧版 AGENTS.md 的原文存档在 `docs/playbooks/agents_md_legacy_202605.md`，内容已过时，只供溯源。

## Cursor Cloud

- 安装：Ubuntu 默认镜像没有 `ensurepip`。先装 `python3.12-venv`，再执行 `python3.12 -m venv .venv && .venv/bin/pip install -r requirements.txt`。
- `scripts/llm_client.py`、`sas_review.py`、`telegram_commands.py` 从 `$HOME/Homepage/llm_json_utils.py` 导入 `parse_llm_json`。这个文件不在本仓库，宿主机上的 canonical 副本也不进 git。Cloud Agent 的 install 只在该路径不存在时写入一份与 `docs/design.md`（2026-07-19）和 `docs/PITFALLS.md` #82 契约一致的实现，已有文件不覆盖。
- 测试不是 pytest。逐个运行 `.venv/bin/python scripts/test_*.py`。`test_issue111_quiet_deepen.py` 和 `test_issue113_extract_quality.py` 要读 gitignore 的 `archives/202609/*-intel-snapshot.json`，全新 clone 没有这些生产快照。
- `run_finance.py` 需要 `OBSIDIAN_PATH` 指向含 `Hermes/Daily Intelligence/watchlist.md` 的目录。API key 放在环境变量或 `~/Daily_Intelligence/.env`。没有 key 时，价格、Pass 0 和代码摘要仍会写入月度报告；邮件和 Telegram 会跳过。MemPalace（`localhost:8765`）失败即跳过。
- 当前锁定的 yfinance 对单只 ticker 仍返回 MultiIndex，`fetch_prices` 的单标的分支会把 `Close` 当成 Series。验证价格时至少放两只 ticker。

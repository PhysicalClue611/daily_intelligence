"""
Daily Intelligence — AM prediction calibration + SAS candidate log
=====================================================================
AM prediction calibration closed loop (issue #10) and SAS candidate evidence
log (issue #31). Extracted from run_finance.py (issue #42, 2026-07-17) to
shrink that file.

Leaf module except for one deferred import: _evaluate_am_predictions() needs
call_llm/LLM_MODEL, which stayed in run_finance.py's core. A module-level
import would be circular (run_finance.py imports evaluate_am_calibration
from this module), so the import happens inside the function body instead —
safe because it only runs once run_finance.py has finished importing this
module.
"""
import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from report_writers import _monthly_path

_HOME = os.path.expanduser("~")
_IN_CONTAINER = os.path.exists("/opt/data")
if _IN_CONTAINER:
    _OBSIDIAN_ROOT = "/opt/obsidian"
else:
    _OBSIDIAN_ROOT = os.path.join(
        _HOME, "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview"
    )

OBSIDIAN  = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))
_PROJ_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")

# ── AM prediction calibration (issue #10) ────────────────────────────────────
# Self-contained, fail-open block. PM slot only: locates today's AM report's
# "## 可验证信号" list (added by VERIFIABLE_SIGNALS_INSTRUCTION_P1/P2 above),
# has a cheap LLM judge each claim against today's actual price/news data,
# writes the verdict as a knowledge entry. Only appends to the PM report
# itself if the evaluation step judges the result important enough to flag
# (most days it won't). Entirely optional: any failure here is caught and
# logged, the main report pipeline is unaffected either way.
#
# Durability (2026-07-02, per user's flag that MemPalace's `finance` room has
# been fully rebuilt multiple times recently): Obsidian is the source of
# truth, not MemPalace. _load_recent_calibration_notes() reads the Obsidian
# log directly for the AM-side "recursive improvement" injection — it does
# NOT depend on get_finance_context()'s generic MemPalace search picking this
# content up, which was never targeted or guaranteed anyway. The MemPalace
# drawer write is kept as a secondary enrichment layer only (nice-to-have for
# ad-hoc semantic queries), never the sole channel. A local backup mirror
# (outside Obsidian, outside git — see .gitignore) protects against Obsidian-
# side loss (sync conflicts, accidental deletion) independently of MemPalace.

CALIBRATION_LOG_PATH = OBSIDIAN / "Hermes/Daily Intelligence/预判校准记录.md"
CALIBRATION_BACKUP_PATH = _PROJ_DIR / "backups" / "预判校准记录_backup.md"

_CALIBRATION_SYSTEM_PROMPT = (
    "你是一名负责复盘财经分析准确率的助理。任务是拿今早报告里的具体预判，"
    "对照今天实际发生的情况做核验，并提炼出对未来分析有参考价值的教训。"
    "只输出要求的 JSON，不要附加其他文字。"
)


def _extract_report_section(month_text: str, date_str: str, slot_label: str) -> str:
    """Extract one day's report section from a monthly file, bounded by the
    next date-stamped '## YYYY-MM-DD ...' header — not by internal '## '
    subsections within the report itself (see docs/PITFALLS.md #25, the same
    bug pattern in the old KG section-locator)."""
    pattern = re.compile(
        rf"## {re.escape(date_str)} {re.escape(slot_label)}\n(.*?)(?=\n## \d{{4}}-\d{{2}}-\d{{2}} |\Z)",
        re.DOTALL,
    )
    m = pattern.search(month_text)
    return m.group(1) if m else ""


def _extract_verifiable_signals(am_section_text: str) -> str:
    """Pull the '## 可验证信号' bullet list out of an AM report section, if present."""
    m = re.search(r"## 可验证信号\s*\n(.*?)(?=\n## |\n\n---\n|\Z)", am_section_text, re.DOTALL)
    return m.group(1).strip() if m else ""


def _evaluate_am_predictions(signals_text: str, price_table: str, news_context: str, date_str: str) -> dict:
    """Single cheap LLM call: judge each AM 'verifiable signal' against today's
    actual outcome data. Returns {} on any failure or empty input (fail-open).
    Cost: ~$0.0005 (small prompt, deepseek-v4-flash via OR)."""
    # Deferred import: call_llm/LLM_MODEL stayed in run_finance.py's core (issue
    # #42 split); module-level import here would be circular since run_finance.py
    # imports evaluate_am_calibration from this module.
    from run_finance import call_llm, LLM_MODEL
    if not OPENROUTER_API_KEY or not signals_text.strip():
        return {}
    prompt = f"""今日日期：{date_str}

今早 AM 报告中的可验证信号清单：
{signals_text}

今日实际结果：
## 价格数据
{price_table}

## 新闻/宏观上下文（节选）
{news_context[:3000]}

任务：
1. 对清单中每一条，判断 hit（命中）/ miss（未命中）/ inconclusive（无法判断），给一句话理由。
2. 写一段"知识条目"（knowledge_entry）——不是简单罗列对错，而是提炼一条对未来分析有参考价值的教训或验证
   （例如某类判断的系统性偏差、某条框架逻辑被验证有效），供未来生成 AM 报告时参考。50-150字。
3. 判断今天的校验结果是否重要到需要出现在今晚报告正文里。原则：
   - 只有当某条高置信度判断被证伪、或某条核心框架逻辑被验证、或存在需要立即警惕的偏差模式时才算"重要"
   - 普通的命中/未命中是常态，不是新闻，不需要展示
   - 宁可少展示，不要为了"有内容"就展示

输出 JSON（不要附加任何其他文字）：
{{
  "verdicts": [{{"claim": "...", "verdict": "hit|miss|inconclusive", "reasoning": "..."}}],
  "knowledge_entry": "...",
  "worth_surfacing": true,
  "surface_blurb": "若 worth_surfacing 为 true，一段可直接放进今晚报告正文的文字（含具体原因）；否则留空字符串"
}}
"""
    try:
        return call_llm(prompt, model=LLM_MODEL, system_prompt=_CALIBRATION_SYSTEM_PROMPT)
    except Exception as e:
        logger.warning(f"AM prediction evaluation failed (non-fatal): {e}")
        return {}


def _append_calibration_entry(path: Path, entry: str, header: str) -> bool:
    """Append-only write to `path` (creating with `header` if new). Append mode
    never truncates existing content — no in-place-overwrite data-loss risk
    (unlike `open(path, 'w')`, see global CLAUDE.md 破坏性文件写入安全). Returns
    True on success; caller logs and treats failure as non-fatal."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text(header + entry, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(entry)
    return True


def _write_calibration_knowledge(date_str: str, knowledge_entry: str, verdicts: list) -> None:
    """Write the same entry to two independent append-only stores, plus a
    MemPalace drawer as a secondary enrichment layer:

    1. Obsidian 预判校准记录.md — source of truth, human-readable, what
       _load_recent_calibration_notes() reads back for the AM injection.
    2. CALIBRATION_BACKUP_PATH — local mirror outside Obsidian and outside
       git (see .gitignore), independent of iCloud sync issues.
    3. MemPalace drawer (room=finance) — best-effort semantic-search
       enrichment only. Per user's flag (2026-07-02) that this room has been
       fully rebuilt multiple times, nothing here depends on this surviving;
       (1) and (2) are the durable stores.

    Each write is independently fail-open — one failing doesn't block the
    others or the main report pipeline."""
    if not knowledge_entry:
        return
    verdict_lines = "\n".join(
        f"- [{v.get('verdict', '?')}] {v.get('claim', '')} — {v.get('reasoning', '')}"
        for v in verdicts if isinstance(v, dict)
    )
    entry = f"\n## {date_str}\n\n{knowledge_entry}\n\n{verdict_lines}\n\n---\n"
    header = "---\nsource: Daily Intelligence 预判校准\n---\n\n# AM 预判校准记录\n"

    try:
        _append_calibration_entry(CALIBRATION_LOG_PATH, entry, header)
        logger.info(f"Calibration knowledge written → {CALIBRATION_LOG_PATH.name}")
    except Exception as e:
        logger.warning(f"Calibration knowledge write (Obsidian) failed (non-fatal): {e}")

    try:
        _append_calibration_entry(CALIBRATION_BACKUP_PATH, entry, header)
        logger.info(f"Calibration knowledge backed up → {CALIBRATION_BACKUP_PATH}")
    except Exception as e:
        logger.warning(f"Calibration knowledge local backup failed (non-fatal): {e}")

    try:
        resp = httpx.post(
            "http://localhost:8765/mempalace/add_drawer",
            json={
                "wing": "paperview",
                "room": "finance",
                "content": f"预判校准 {date_str}：\n{knowledge_entry}",
                "source_file": "Hermes/Daily Intelligence/预判校准记录.md",
                "added_by": "daily-intel-calibration",
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info("Calibration MemPalace drawer written (best-effort enrichment)")
    except Exception as e:
        logger.warning(f"Calibration MemPalace drawer skipped (non-fatal, not the durable store): {e}")


# ── SAS candidate evidence log (issue #31) ────────────────────────────────
# Pass 2 flags news/events matching the Investment Operating Manual's 7.4
# internal-signal list or Section 6 cognitive-upgrade criteria via the
# `sas_candidates` JSON field (see USER_PROMPT_TEMPLATE_P2 rule ⑧). This is
# a pure evidence queue for the semi-annual SAS review (Manual Section 7.1:
# scoring requires a human-written 100-300-char rationale with no price
# lookback) — it never drives automated scoring or trading advice.

SAS_CANDIDATE_LOG_PATH = OBSIDIAN / "Hermes/Daily Intelligence/SAS候选证据日志.md"

_SAS_CANDIDATE_HEADER = (
    "---\nsource: Daily Intelligence SAS候选证据\n---\n\n# SAS 候选证据日志\n\n"
    "> 供半年 SAS 复审（Investment Operating Manual v1.0 第7节）人工批阅使用，"
    "不参与自动打分，评分权仍在人工。\n"
)


def write_sas_candidate_log(date_str: str, slot_label: str, candidates: list) -> None:
    """Append-only write of Pass 2-flagged SAS candidate evidence. Fail-open —
    a write failure here must never affect the main report pipeline."""
    if not candidates:
        return
    lines = []
    for c in candidates:
        if not isinstance(c, dict):
            continue
        ticker = c.get("ticker", "").strip()
        category = c.get("category", "").strip()
        fact = c.get("fact", "").strip()
        if not ticker or not category or not fact:
            continue
        lines.append(f"- **{ticker}** [{category}] {fact}")
    if not lines:
        return
    entry = f"\n## {date_str} {slot_label}\n\n" + "\n".join(lines) + "\n"
    try:
        _append_calibration_entry(SAS_CANDIDATE_LOG_PATH, entry, _SAS_CANDIDATE_HEADER)
        logger.info(f"SAS candidate evidence written → {SAS_CANDIDATE_LOG_PATH.name} ({len(lines)} entries)")
    except Exception as e:
        logger.warning(f"SAS candidate log write failed (non-fatal): {e}")


def _load_recent_calibration_notes(max_entries: int = 5, max_chars: int = 1200) -> str:
    """Read recent AM-calibration knowledge entries directly from Obsidian
    (falling back to the local backup mirror if the Obsidian file is missing
    or unreadable) for injection into today's AM prompt. Deliberately does
    NOT go through MemPalace — see _write_calibration_knowledge docstring for
    why. Fail-open: returns "" on any error or if nothing has been recorded
    yet (e.g. first days after this feature shipped)."""
    for path in (CALIBRATION_LOG_PATH, CALIBRATION_BACKUP_PATH):
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            entries = re.findall(r"\n## (\d{4}-\d{2}-\d{2})\n\n(.*?)(?=\n## \d{4}-\d{2}-\d{2}\n|\Z)", text, re.DOTALL)
            if not entries:
                continue
            recent = entries[-max_entries:]
            lines = [f"- [{date}] {body.split(chr(10)+chr(10))[0].strip()}" for date, body in recent]
            body = "\n".join(lines)
            if len(body) > max_chars:
                body = body[:max_chars] + "\n[...truncated]"
            return "## 近期预判校准教训（供参考，非当前持仓）\n" + body + "\n"
        except Exception as e:
            logger.warning(f"Reading calibration notes from {path} failed (non-fatal): {e}")
            continue
    return ""


def evaluate_am_calibration(today_et: str, run_slot: str, price_table: str,
                            finnhub_news_section: str, sonar_macro_section: str,
                            report_md: str) -> str:
    """Top-level entry point for the calibration step — PM slot only. Returns
    report_md unchanged, or with a '## 预判校验' section appended if the
    evaluation judged today's result worth surfacing. Fully fail-open: any
    exception here just returns report_md unchanged and logs a warning."""
    if run_slot != "pm":
        return report_md
    try:
        am_slot_label = "开盘前简报"
        month_path = _monthly_path(today_et)
        if not month_path.exists():
            return report_md
        month_text = month_path.read_text(encoding="utf-8")
        am_section = _extract_report_section(month_text, today_et, am_slot_label)
        signals_text = _extract_verifiable_signals(am_section)
        if not signals_text:
            logger.info("No '可验证信号' section found in today's AM report, skipping calibration")
            return report_md

        news_context = "\n".join(filter(None, [finnhub_news_section, sonar_macro_section]))
        evaluation = _evaluate_am_predictions(signals_text, price_table, news_context, today_et)
        knowledge_entry = evaluation.get("knowledge_entry", "")
        verdicts = evaluation.get("verdicts", [])
        if knowledge_entry:
            _write_calibration_knowledge(today_et, knowledge_entry, verdicts)

        if evaluation.get("worth_surfacing") and evaluation.get("surface_blurb"):
            report_md += f"\n\n---\n\n## 预判校验\n{evaluation['surface_blurb']}\n"
            logger.info("Calibration result surfaced in PM report")
        return report_md
    except Exception as e:
        logger.warning(f"AM prediction calibration step failed (non-fatal): {e}")
        return report_md



"""
Daily Intelligence — report writers
=====================================
Everything that writes a pipeline artifact somewhere: monthly report file
path/dedup helpers, Context Log writer, MemPalace drawer
push, Obsidian report append, Telegram send, email footer. Extracted from
run_finance.py (issue #42, 2026-07-17) to shrink that file.

Leaf module: does not import from run_finance.py.
"""
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from telegram_utils import call_telegram
from budget_trackers import TAVILY_DAILY_LIMIT

_HOME = os.path.expanduser("~")
_IN_CONTAINER = os.path.exists("/opt/data")
if _IN_CONTAINER:
    _OBSIDIAN_ROOT = "/opt/obsidian"
else:
    _OBSIDIAN_ROOT = os.path.join(
        _HOME, "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview"
    )

OBSIDIAN    = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))
_PROJ_DIR   = Path(os.path.dirname(os.path.abspath(__file__))).parent
REPORTS_DIR = OBSIDIAN / "Hermes/Daily Intelligence/Daily Reports"
ET = ZoneInfo("America/New_York")
logger = logging.getLogger(__name__)

# ── Monthly file helpers ──────────────────────────────────────────────────────

def _monthly_path(date_str: str) -> Path:
    """Return path for the monthly report file, e.g. Daily_Intel_report_202604.md"""
    ym = date_str[:7].replace("-", "")  # "2026-04" → "202604"
    return REPORTS_DIR / f"Daily_Intel_report_{ym}.md"


def _monthly_dedup(date_str: str, slot_label: str) -> bool:
    """Return True if this slot's report already exists in the monthly file."""
    p = _monthly_path(date_str)
    if not p.exists():
        return False
    return f"## {date_str} {slot_label}" in p.read_text(encoding="utf-8")


def _context_log_path(date_str: str) -> Path:
    ym = date_str[:7].replace("-", "")
    return REPORTS_DIR / f"Daily_Intel_context_{ym}.md"


def write_context_log(
    date_str: str,
    slot_label: str,
    now_et: "datetime",
    price_table: str,
    news_items: list,
    triggered_geo_topics: list,
    sonar_macro_section: str,
    all_search_jobs: list,
    intel_snapshot: dict | None = None,
) -> None:
    """Append structured context snapshot to monthly context log in Obsidian (gets mined).
    Contains: price table, geo-triggered news headlines, Sonar macro, search queries.
    Does not duplicate archived intelligence snapshot full text. Fail-open.
    """
    try:
        ym_display = date_str[:7]
        path = _context_log_path(date_str)
        path.parent.mkdir(parents=True, exist_ok=True)

        lines: list[str] = []

        # Monthly file header (only on first write)
        if not path.exists():
            lines.append(f"---\ndate: {ym_display}\nsource: Daily Intelligence context log\n---\n")
            lines.append(f"# Daily Intelligence Context Log {ym_display}\n")

        lines.append(f"\n## [Context] {date_str} {slot_label}")
        lines.append(f"_运行时间: {now_et.strftime('%Y-%m-%d %H:%M %Z')}_\n")

        # Price snapshot
        lines.append("### 价格快照")
        lines.append(price_table.strip())
        lines.append("")

        if intel_snapshot is not None:
            lines.append("### 标的情报摘要")
            for entity in intel_snapshot.get("entities", []):
                move = entity.get("move") or {}
                coverage = entity.get("coverage") or {}
                lead = (entity.get("items") or [{}])[0].get("title") or "无标题线索"
                status = "异动待归因" if move.get("flags") else "无异动"
                lines.append(
                    f"- {entity['ticker']} {status}；主线索：{lead}；"
                    f"Finnhub={coverage.get('finnhub', 0)}、Google News={coverage.get('google_news', 0)}、"
                    f"RSS={coverage.get('rss', 0)}、Guardian={coverage.get('guardian', 0)}、"
                    f"SEC 8-K={coverage.get('sec_8k', 0)}、Yahoo RSS={coverage.get('yahoo_rss', 0)}；"
                    f"错误={'; '.join(coverage.get('errors') or []) or '无'}"
                )
            lines.append("")

        # Legacy callers may still pass geo-matched NewsItem objects.
        triggered_items = [item for item in news_items if item.topics]
        if triggered_items:
            lines.append("### 触发新闻（命中地缘/异动相关）")
            for item in triggered_items[:30]:
                ts = item.published.strftime("%m-%d %H:%M UTC")
                topics_str = ", ".join(item.topics)
                lines.append(f"- [{ts}] [{topics_str}] {item.title} ({item.source})")
            lines.append("")

        # Sonar macro snapshot (strip section header if present)
        if sonar_macro_section and sonar_macro_section.strip():
            lines.append("### 宏观快照（Sonar）")
            sonar_clean = re.sub(r"^#+\s+[^\n]+\n", "", sonar_macro_section, count=1).strip()
            lines.append(sonar_clean[:2000])
            lines.append("")

        # Search queries used
        if all_search_jobs:
            lines.append("### 搜索任务")
            for job in all_search_jobs:
                lines.append(f"- `{job.get('query', '')}` (days={job.get('days', 1)})")
            lines.append("")

        lines.append("---\n")

        with path.open("a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        logger.info(f"Context log written → {path.name}")
    except Exception as e:
        logger.warning(f"Context log write failed (non-fatal): {e}")


# ── MemPalace drawer writer ───────────────────────────────────────────────────

def _mempalace_add_daily_drawer(date_str: str, slot: str, report_md: str) -> None:
    """Push today's report as a per-day MemPalace drawer. Fail-open."""
    try:
        ym = date_str[:7].replace("-", "")
        source_file = f"Hermes/Daily Intelligence/Daily Reports/Daily_Intel_report_{ym}.md"
        slot_label = "am" if slot == "am" else "pm"
        content = f"日期：{date_str} {slot_label}\n\n{report_md}"
        resp = httpx.post(
            "http://localhost:8765/mempalace/add_drawer",
            json={
                "wing": "paperview",
                "room": "finance",
                "content": content,
                "source_file": source_file,
                "added_by": "daily-intel",
            },
            timeout=10,
        )
        resp.raise_for_status()
        result = resp.json()
        reason = result.get("reason", "new")
        logger.info(f"MemPalace drawer {result.get('drawer_id','?')} [{reason}]")
    except Exception as e:
        logger.warning(f"MemPalace add_drawer skipped: {e}")


# ── Obsidian writer ───────────────────────────────────────────────────────────

def write_report(today_et: str, slot_label: str, markdown: str, budget: dict) -> None:
    """Append report to monthly file Daily_Intel_report_YYYYMM.md."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _monthly_path(today_et)
    ym_display = today_et[:7]  # "2026-04"

    tavily_line = (f"_Tavily 手动本次: {budget.get('used', 0)}/{budget.get('_run_cap', 0)}_"
                   if budget.get("_manual") else
                   f"_Tavily: {budget.get('used', 0)}/{TAVILY_DAILY_LIMIT}_")
    section = (
        f"\n## {today_et} {slot_label}\n"
        f"{tavily_line}\n\n"
        f"{markdown}\n\n---\n"
    )

    if not path.exists():
        header = f"---\ndate: {ym_display}\nsource: Daily Intelligence\n---\n\n# Daily Intelligence {ym_display}\n"
        path.write_text(header + section, encoding="utf-8")
    else:
        with path.open("a", encoding="utf-8") as f:
            f.write(section)
    logger.info(f"Report appended: {path} [{today_et} {slot_label}]")


# ── Telegram sender ──────────────────────────────────────────────────────────

_TG_LIMIT = 4096


def _md_to_tg_html(md: str) -> str:
    """Convert report markdown to Telegram-compatible HTML (subset: b, code)."""
    import html as _html
    lines = []
    for line in md.split("\n"):
        if line.startswith("# "):
            line = f"<b>{_html.escape(line[2:])}</b>"
        elif line.startswith("## "):
            line = f"<b>{_html.escape(line[3:])}</b>"
        elif line.startswith("### "):
            line = f"<b>{_html.escape(line[4:])}</b>"
        elif line.startswith("---"):
            line = "─────────────"
        else:
            line = _html.escape(line)
            line = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", line)
            line = re.sub(r"`(.+?)`", r"<code>\1</code>", line)
        lines.append(line)
    return "\n".join(lines)


def _tg_chunks(text: str, limit: int = _TG_LIMIT) -> list[str]:
    """Split text into chunks ≤ limit, breaking on newlines where possible."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        cut = text.rfind("\n", 0, limit)
        if cut == -1:
            cut = limit
        chunks.append(text[:cut])
        text = text[cut:].lstrip("\n")
    return chunks


def send_telegram_report(report_md: str, subject: str) -> bool:
    """Send finance report via Telegram bot. Returns True if all chunks sent."""
    token = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        logger.warning("Telegram credentials not configured, skipping")
        return False

    # report_md already starts with "# [Daily_Intel] ..." — don't add subject again
    body_html = _md_to_tg_html(report_md)
    chunks = _tg_chunks(body_html)

    success = True
    for i, chunk in enumerate(chunks):
        resp = call_telegram(token, "sendMessage", {"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"})
        if resp.get("ok"):
            logger.info(f"Telegram sent chunk {i+1}/{len(chunks)}")
        else:
            logger.warning(f"Telegram send failed (chunk {i+1}): {resp}")
            success = False
    return success


def send_telegram_alert(text: str) -> bool:
    """Send a short plaintext failure alert to Telegram. Fail-open — never raises."""
    token = os.getenv("FINANCE_TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("FINANCE_TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    resp = call_telegram(token, "sendMessage", {"chat_id": chat_id, "text": text})
    return bool(resp.get("ok"))


# ── Email footer (finance-specific) ──────────────────────────────────────────

def _ibkr_auth_note() -> str:
    """IBKR gateway integration — currently disabled. Returns '' unconditionally.

    Original implementation checked Client Portal Gateway auth status and added
    a footer warning when the session had expired. Re-enable by restoring the
    body below and shipping the ibkr/ module alongside this script.
    """
    # IBKR_DISABLED: gateway not bundled in this distribution
    return ""


def _fmt_llm_meta(meta: dict) -> str:
    """Format call_llm()'s _llm_meta into a short human-readable provider/retry summary."""
    if not meta:
        return "调用失败（已发送告警）"
    if meta.get("fallback"):
        return (f"OR flex fallback → {meta['model']} via {meta.get('provider', 'n/a')}"
                f"（主模型 {meta.get('primary_attempts', '?')} 次尝试均失败）")
    provider = meta.get("provider", "n/a")
    attempts = meta.get("attempts", 1)
    if attempts > 1:
        return f"OR/{provider}（第 {attempts} 次尝试成功）"
    return f"OR/{provider}"


def finance_footer(date_str: str, budget: dict) -> str:
    ibkr_note = _ibkr_auth_note()
    return (
        f"\n\n---\n"
        f"_Daily_Intel · {date_str} ET_\n"
        f"{ibkr_note}"
    )

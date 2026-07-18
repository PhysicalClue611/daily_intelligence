"""
Daily Intelligence — report writers
=====================================
Everything that writes a pipeline artifact somewhere: monthly report file
path/dedup helpers, Context Log + Extract Archive writers, MemPalace drawer
push, Obsidian report append, Telegram send, email footer. Extracted from
run_finance.py (issue #42, 2026-07-17) to shrink that file.

Leaf module: does not import from run_finance.py (sas_review.py accesses
several of these via `import run_finance as rf; rf.X`, which still works —
see run_finance.py's re-export block). write_extract_archive()'s
_source_confidence_tags comes from scoring_utils.py, a shared leaf module —
not a deferred `from run_finance import ...` inside the function body (the
original approach here, before PR #43 review feedback pointed out it
silently assumed run_finance.py is registered in sys.modules as
"run_finance", which is false when it's run directly as the entrypoint, as
launchd does — Python registers it as "__main__" instead).
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
from scoring_utils import _source_confidence_tags

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
ARCHIVE_DIR = _PROJ_DIR / "archives"  # Extract full-text archive (outside Obsidian, never mined)
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


def get_last_report_date() -> str:
    """Find the most recent report date across all monthly files."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    monthly_files = sorted(REPORTS_DIR.glob("Daily_Intel_report_*.md"), reverse=True)
    for mf in monthly_files:
        text = mf.read_text(encoding="utf-8")
        dates = re.findall(r"^## (\d{4}-\d{2}-\d{2})", text, re.MULTILINE)
        if dates:
            return max(dates)
    return "N/A（首次运行）"


# ── Context log + Extract archive writers ────────────────────────────────────

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
) -> None:
    """Append structured context snapshot to monthly context log in Obsidian (gets mined).
    Contains: price table, geo-triggered news headlines, Sonar macro, search queries.
    Does NOT contain Tavily Extract full text (see write_extract_archive). Fail-open.
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

        # Triggered news (geo-matched items only, not all 300)
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


def write_extract_archive(
    date_str: str,
    slot: str,
    now_et: "datetime",
    all_search_jobs: list,
    filtered: list,
    extract_results: list,
    extra_keywords: list | None = None,
) -> None:
    """Write cleaned Tavily Extract full text to local archive outside Obsidian.
    Never mined by MemPalace. Preserves original intelligence for audit/mid-term review.
    Cleaning: lines < 60 chars stripped (nav/ads/links). Fail-open.
    `extra_keywords` is build_keyword_set()'s output, threaded into the same
    corroboration fingerprint used by format_extract_results() so the archive and
    the LLM-facing tags agree (issue #19 follow-up).
    """
    if not extract_results and not filtered:
        return
    try:
        ym = date_str[:7].replace("-", "")
        out_dir = ARCHIVE_DIR / ym
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{date_str}-{slot}-extract.md"

        lines: list[str] = []
        lines.append(f"# Extract Archive: {date_str} {slot.upper()}")
        lines.append(f"_生成时间: {now_et.strftime('%Y-%m-%d %H:%M %Z')}_\n")

        # Search queries
        if all_search_jobs:
            lines.append("## 搜索任务")
            for job in all_search_jobs:
                lines.append(f"- {job.get('query', '')}")
            lines.append("")

        # Layer 2b filtered candidates (with score + URL)
        if filtered:
            lines.append("## 搜索结果候选（Layer 2b 过滤后，进入 Extract 的10条）")
            for i, r in enumerate(filtered, 1):
                title = (r.get("title") or r.get("url") or "")[:100]
                url = r.get("url", "")
                score = float(r.get("score") or 0)
                lines.append(f"{i}. [score:{score:.2f}] {title}")
                lines.append(f"   {url}")
            lines.append("")

        # Extract full text (cleaned)
        if extract_results:
            lines.append("## Extract 全文")
            for r in extract_results:
                url = r.get("url", "")
                chunks = r.get("chunks") or []
                raw = r.get("raw_content", "")
                full_text = " ".join((c.get("content") or "") for c in chunks) or raw
                lines.append(f"\n### {url}")
                lines.append(f"[{_source_confidence_tags(url, full_text, filtered, extra_keywords)}]\n")
                if chunks:
                    for chunk in chunks:
                        text = chunk.get("content") or ""
                        # Strip lines < 60 chars (navigation, ads, single-word fragments)
                        clean = "\n".join(
                            ln for ln in text.splitlines() if len(ln.strip()) >= 60
                        ).strip()
                        if clean:
                            lines.append(clean)
                            lines.append("")
                elif raw:
                    clean = "\n".join(
                        ln for ln in raw.splitlines() if len(ln.strip()) >= 60
                    ).strip()
                    if clean:
                        lines.append(clean[:8000])  # cap raw fallback
                        lines.append("")

        path.write_text("\n".join(lines), encoding="utf-8")
        logger.info(f"Extract archive written → archives/{ym}/{path.name}")
    except Exception as e:
        logger.warning(f"Extract archive write failed (non-fatal): {e}")


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

    section = (
        f"\n## {today_et} {slot_label}\n"
        f"_Tavily: {budget.get('used', 0)}/{TAVILY_DAILY_LIMIT}_\n\n"
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

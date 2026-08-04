"""
Daily Intelligence — AM prediction calibration + SAS candidate log
=====================================================================
AM prediction calibration closed loop (issue #10) and SAS candidate evidence
log (issue #31). Extracted from run_finance.py (issue #42, 2026-07-17) to
shrink that file.

Leaf module: does not import from run_finance.py. _evaluate_am_predictions()
gets call_llm from llm_client.py, a shared leaf module — not a
deferred `from run_finance import ...` inside the function body (the
original approach here, before PR #43 review feedback pointed out it
silently assumed run_finance.py is registered in sys.modules as
"run_finance", which is false when it's run directly as the entrypoint, as
launchd does — Python registers it as "__main__" instead).
"""
import json
import logging
import os
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from report_writers import _monthly_path
from llm_client import call_llm

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

# Structured metrics substrate (2026-07-18, issue #10 follow-up). The two
# paths above are prose, human-readable, and are what AM re-reads for
# qualitative "recursive improvement". This JSONL is a parallel,
# machine-computable record of the same daily verdicts — one line per PM
# run — so hit-rate / inconclusive-rate / miss-type trends can be computed
# without re-parsing markdown prose. Project-local, gitignored, append-only
# like every other tracker in this project (see 破坏性文件写入安全 in global
# CLAUDE.md — 'a' mode only, never 'w').
CALIBRATION_METRICS_PATH = _PROJ_DIR / "finance_calibration_log.jsonl"

_MISS_TYPES = ("framework_falsified", "threshold_miscalibrated")

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
    Cost: ~$0.0005 (small prompt, google/gemma-4-31b-it via OR, "am_calibration"
    stage — see llm_config.py, split out from report_pass1 in issue #59)."""
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
2. 对每一条额外标注 resolvable_from_eod_data（true/false）：该条判断能否仅凭今日收盘价/成交量/汇率/收益率等
   已有数据判定，不需要额外的新闻/事件内容确认。若判断结果依赖"某新闻是否发生/某证词说了什么"这类无法从
   价格表直接读出的信息，则为 false。
3. 若某条 verdict 为 miss，额外标注 miss_type：
   - "framework_falsified"：预判背后的因果逻辑本身在今天被证明是错的（如"地缘风险应传导至科技股"但实际没传导）
   - "threshold_miscalibrated"：方向/逻辑基本合理，只是具体价位/百分比阈值设得不对（卡在了边界外一点点，
     或波动幅度被低估/高估）
   非 miss 的条目该字段留 null。
4. 写一段"知识条目"（knowledge_entry）——不是简单罗列对错，而是提炼一条对未来分析有参考价值的教训或验证
   （例如某类判断的系统性偏差、某条框架逻辑被验证有效），供未来生成 AM 报告时参考。50-150字。
5. 判断今天的校验结果是否重要到需要出现在今晚报告正文里。原则：
   - 只有当某条高置信度判断被证伪、或某条核心框架逻辑被验证、或存在需要立即警惕的偏差模式时才算"重要"
   - 普通的命中/未命中是常态，不是新闻，不需要展示
   - 宁可少展示，不要为了"有内容"就展示

输出 JSON（不要附加任何其他文字）：
{{
  "verdicts": [{{"claim": "...", "verdict": "hit|miss|inconclusive", "reasoning": "...",
                 "resolvable_from_eod_data": true, "miss_type": "framework_falsified|threshold_miscalibrated|null"}}],
  "knowledge_entry": "...",
  "worth_surfacing": true,
  "surface_blurb": "若 worth_surfacing 为 true，一段可直接放进今晚报告正文的文字（含具体原因）；否则留空字符串"
}}
"""
    try:
        return call_llm(prompt, stage="am_calibration", system_prompt=_CALIBRATION_SYSTEM_PROMPT)
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


def _normalize_verdict(raw) -> str:
    """LLM output may drift on casing ('Hit', 'MISS') even with an explicit
    enum in the prompt — normalize before matching so a drifted string
    doesn't silently vanish from both counts and n_total (which would
    otherwise drop the whole day's metrics row while the prose knowledge
    entry still gets written, letting the two diverge)."""
    return str(raw).strip().lower() if raw is not None else ""


def _coerce_bool(raw) -> bool:
    """Same LLM-JSON-drift defense for booleans: some models emit "true"/1
    instead of a JSON boolean. Only recognized truthy forms count — anything
    else (including absence) is False, matching the previous strict
    `is True` behavior for the common case."""
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        return raw.strip().lower() in ("true", "yes", "1")
    return False


def _write_calibration_metrics(date_str: str, verdicts: list) -> None:
    """Append one aggregated JSON line to CALIBRATION_METRICS_PATH — the
    structured counterpart to _write_calibration_knowledge()'s prose entry.
    Fail-open: a write failure here must never affect the main report
    pipeline (same contract as every other writer in this module)."""
    if not verdicts:
        return
    counts = {"hit": 0, "miss": 0, "inconclusive": 0}
    resolvable = 0
    miss_type_counts = {"framework_falsified": 0, "threshold_miscalibrated": 0, "unclassified": 0}
    for v in verdicts:
        if not isinstance(v, dict):
            continue
        verdict = _normalize_verdict(v.get("verdict"))
        if verdict not in counts:
            logger.warning(f"Calibration verdict unrecognized, dropping this row: {v.get('verdict')!r}")
            continue
        counts[verdict] += 1
        # Only count resolvable_from_eod_data for verdicts that were
        # actually accepted above — otherwise a row with an unrecognized
        # verdict string can still increment `resolvable` while contributing
        # nothing to n_total, letting resolvable_rate exceed 1.0 downstream.
        if _coerce_bool(v.get("resolvable_from_eod_data")):
            resolvable += 1
        if verdict == "miss":
            miss_type = _normalize_verdict(v.get("miss_type")) or None
            if miss_type in _MISS_TYPES:
                miss_type_counts[miss_type] += 1
            else:
                miss_type_counts["unclassified"] += 1
    n_total = counts["hit"] + counts["miss"] + counts["inconclusive"]
    if n_total == 0:
        return
    record = {
        "date": date_str,
        "n_hit": counts["hit"],
        "n_miss": counts["miss"],
        "n_inconclusive": counts["inconclusive"],
        "n_total": n_total,
        "n_resolvable_from_eod": resolvable,
        "miss_type_counts": miss_type_counts,
    }
    try:
        CALIBRATION_METRICS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CALIBRATION_METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        logger.info(f"Calibration metrics logged → {CALIBRATION_METRICS_PATH.name} ({record})")
    except Exception as e:
        logger.warning(f"Calibration metrics write failed (non-fatal): {e}")


def compute_calibration_metrics(window_days: int = 10) -> dict:
    """Read CALIBRATION_METRICS_PATH and compute rolling stats over the most
    recent `window_days` recorded days (one JSONL line = one PM run's
    aggregated verdicts, not one signal — so this is trading days, not
    individual signal count). Returns {} if the file is missing or has fewer
    than 3 recorded days (not enough to be meaningful). Fail-open: any parse
    error on an individual line is skipped, not fatal."""
    if not CALIBRATION_METRICS_PATH.exists():
        return {}
    records = []
    try:
        for line in CALIBRATION_METRICS_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            # Validate shape before it ever reaches dedup/sort below: a line
            # that's valid JSON but not an object (e.g. `[]`, `123`) would
            # raise AttributeError on .get(), and a missing/null/malformed
            # date would either collapse onto a shared "" dedup key or make
            # records.sort() raise TypeError comparing None to str — either
            # way escaping the fail-open contract this docstring promises,
            # with no caller-side try/except on the TG command path.
            if not isinstance(obj, dict) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(obj.get("date") or "")):
                continue
            records.append(obj)
    except Exception as e:
        logger.warning(f"Reading calibration metrics failed (non-fatal): {e}")
        return {}
    # Dedup by date, last write wins: FINANCE_FORCE_RUN (see the "强制运行"
    # TG command) explicitly bypasses run_finance.py's same-day dedup check,
    # so a manual re-run can append a second line for a date already
    # recorded. The write side stays pure append-only (audit trail, no
    # in-place edits) — dedup happens only here, at read time, so a re-run
    # doesn't silently double-count that day's verdicts into the rolling
    # window or inflate the "N trading days" the window claims to span.
    by_date = {}
    for r in records:
        by_date[r.get("date", "")] = r
    records = list(by_date.values())
    if len(records) < 3:
        return {}

    records.sort(key=lambda r: r.get("date", ""))
    window = records[-window_days:]

    n_hit = sum(r.get("n_hit", 0) for r in window)
    n_miss = sum(r.get("n_miss", 0) for r in window)
    n_inconclusive = sum(r.get("n_inconclusive", 0) for r in window)
    n_resolvable = sum(r.get("n_resolvable_from_eod", 0) for r in window)
    n_total = n_hit + n_miss + n_inconclusive
    n_decidable = n_hit + n_miss

    miss_types = {"framework_falsified": 0, "threshold_miscalibrated": 0, "unclassified": 0}
    for r in window:
        mt = r.get("miss_type_counts", {})
        for k in miss_types:
            miss_types[k] += mt.get(k, 0)

    return {
        "window_days": len(window),
        "date_range": (window[0].get("date", ""), window[-1].get("date", "")),
        "n_total_signals": n_total,
        "hit_rate": round(n_hit / n_decidable, 3) if n_decidable else None,
        "inconclusive_rate": round(n_inconclusive / n_total, 3) if n_total else None,
        "resolvable_rate": round(n_resolvable / n_total, 3) if n_total else None,
        "n_miss": n_miss,
        "miss_type_counts": miss_types,
    }


def format_calibration_metrics_report(window_days: int = 10) -> str:
    """Human-readable summary of compute_calibration_metrics(), for the TG
    on-demand ("校准统计") command only — the AM prompt banner does NOT use
    this; _load_recent_calibration_notes() builds its own shorter one-line
    banner directly from compute_calibration_metrics(), since the full
    multi-line report here is sized for a human reading Telegram, not for
    prompt-budget-conscious LLM injection. Returns a "not enough data yet"
    message rather than an empty string, so the TG command always has
    something legible to send back."""
    m = compute_calibration_metrics(window_days)
    if not m:
        return "校准数据不足（少于3个交易日记录），暂无统计可展示。"
    hit_pct = f"{m['hit_rate']*100:.0f}%" if m["hit_rate"] is not None else "N/A"
    inc_pct = f"{m['inconclusive_rate']*100:.0f}%" if m["inconclusive_rate"] is not None else "N/A"
    res_pct = f"{m['resolvable_rate']*100:.0f}%" if m["resolvable_rate"] is not None else "N/A"
    mt = m["miss_type_counts"]
    mt_total = mt["framework_falsified"] + mt["threshold_miscalibrated"] + mt["unclassified"]
    mt_line = "无 miss 记录"
    if mt_total:
        mt_line = (
            f"框架证伪 {mt['framework_falsified']}/{mt_total}、"
            f"阈值未卡准 {mt['threshold_miscalibrated']}/{mt_total}、"
            f"未分类 {mt['unclassified']}/{mt_total}"
        )
    start, end = m["date_range"]
    return (
        f"预判校准统计（{start} ~ {end}，{m['window_days']}个交易日，{m['n_total_signals']}条信号）\n"
        f"命中率（可判定内）：{hit_pct}\n"
        f"inconclusive率：{inc_pct}\n"
        f"仅凭EOD数据可判定的信号占比：{res_pct}\n"
        f"miss 类型分布：{mt_line}"
    )


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

            # Quantitative anchor ahead of the prose lessons (issue #10
            # follow-up, 2026-07-18): gives AM a number to calibrate against,
            # not just qualitative anecdotes. Degrades silently — if there's
            # not enough structured history yet, this adds nothing and the
            # banner is skipped rather than showing a "not enough data" stub
            # inside the AM prompt (that message is only useful to a human
            # reading the TG command, not to the LLM generating predictions).
            metrics = compute_calibration_metrics()
            stats_banner = ""
            if metrics and metrics.get("hit_rate") is not None:
                hit_pct = round(metrics["hit_rate"] * 100)
                inc_pct = round(metrics["inconclusive_rate"] * 100) if metrics.get("inconclusive_rate") is not None else None
                stats_banner = f"## 近期预判校准统计（近{metrics['window_days']}个交易日）\n命中率{hit_pct}%"
                if inc_pct is not None:
                    stats_banner += f"，inconclusive率{inc_pct}%"
                stats_banner += "\n\n"

            return stats_banner + "## 近期预判校准教训（供参考，非当前持仓）\n" + body + "\n"
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
        _write_calibration_metrics(today_et, verdicts)

        if evaluation.get("worth_surfacing") and evaluation.get("surface_blurb"):
            report_md += f"\n\n---\n\n## 预判校验\n{evaluation['surface_blurb']}\n"
            logger.info("Calibration result surfaced in PM report")
        return report_md
    except Exception as e:
        logger.warning(f"AM prediction calibration step failed (non-fatal): {e}")
        return report_md



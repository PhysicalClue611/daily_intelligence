#!/usr/bin/env python3
"""
Quarterly SAS deep-dive (issue #32).

Triggers automatically once per tracked core holding, on the 3rd NYSE trading
day after that ticker's earnings release (reaction-day-aware: AMC releases
start counting from the next session). Scores the four Strategic Alpha Score
dimensions directly via a stronger model (Claude Sonnet, not the cheap daily
DeepSeek models) — unlike the daily AM/PM pipeline, which never auto-scores
SAS and only logs candidate evidence (see issue #31).

Design reference: GitHub issue #32 (full history of decisions + the 2026-07-09
data-source verification that shaped this — Finnhub institutional-ownership
and yfinance options data both turned out to be unusable; edgartools covers
Form 4 + 10-K instead).

Manual trigger:
    .venv/bin/python scripts/sas_review.py --ticker INTC

Automatic (all tracked tickers whose earnings-trigger window matches today):
    .venv/bin/python scripts/sas_review.py
"""
import sys, os

_HOME = os.path.expanduser("~")
_IN_CONTAINER = os.path.exists("/opt/data")
if _IN_CONTAINER:
    _OBSIDIAN_ROOT = "/opt/obsidian"
else:
    _OBSIDIAN_ROOT = os.path.join(
        _HOME, "Library/Mobile Documents/iCloud~md~obsidian/Documents/Paperview"
    )
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
_DI_ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_DI_ENV_FILE)

import argparse
import json
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx

import run_finance as rf
import fetch_prices as fp
import sec_edgar_utils as seu
from finance_email import send_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("sas_review")

OBSIDIAN = Path(os.getenv("OBSIDIAN_PATH", _OBSIDIAN_ROOT))
_PROJ_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent

TRACKED_TICKERS_PATH = _PROJ_DIR / "sas_tracked_tickers.json"
SAS_REVIEW_DIR = OBSIDIAN / "Finance" / "SAS_Review"
CANDIDATE_LOG_PATH = OBSIDIAN / "Hermes/Daily Intelligence/SAS候选证据日志.md"

FINNHUB_API_KEY = fp.FINNHUB_API_KEY
FINNHUB_BASE = fp.FINNHUB_BASE

# No fallback model for v1 — deliberate, per issue #32 ("先不接 fallback，观察实际效果").
SAS_REVIEW_MODEL = "~anthropic/claude-sonnet-latest"

EARNINGS_ANCHOR_MAX_RETRIES = 3
INSIDER_LOOKBACK_DAYS = 120
FINNHUB_NEWS_LOOKBACK_HOURS = 90 * 24


# ── Persistent tracked-ticker list (issue #32 point 1) ────────────────────────
# Once a core individual holding crosses >2% weight it stays tracked forever —
# a full exit doesn't erase the point of comparing future re-entries against
# past SAS judgment. Only manual editing of the JSON file removes a ticker.

def _load_tracked_tickers() -> list[str]:
    if not TRACKED_TICKERS_PATH.exists():
        return []
    try:
        return json.loads(TRACKED_TICKERS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Tracked tickers file corrupt, treating as empty (non-fatal): {e}")
        return []


def _save_tracked_tickers(tickers: list[str]) -> None:
    """Atomic write (temp file + os.replace) — never open(path,'w') directly
    on a file whose prior content has standing value (global CLAUDE.md 破坏性
    文件写入安全)."""
    tmp_path = TRACKED_TICKERS_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(sorted(set(tickers)), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, TRACKED_TICKERS_PATH)


def _update_tracked_tickers() -> list[str]:
    tracked = set(_load_tracked_tickers())
    before = set(tracked)
    weights = rf._get_portfolio_weights()
    core = set(rf._get_core_holding_tickers())
    for ticker in core:
        if weights.get(ticker, 0) > 2.0:
            tracked.add(ticker)
    if tracked != before:
        _save_tracked_tickers(sorted(tracked))
        logger.info(f"SAS tracked tickers updated: {sorted(tracked)} (added: {sorted(tracked - before)})")
    return sorted(tracked)


# ── Earnings-trigger detection ─────────────────────────────────────────────────

def _get_last_earnings_event(ticker: str, lookback_days: int = 45) -> dict | None:
    """Most recent past earnings event for `ticker` from Finnhub's calendar.
    Fail-open: returns None on any error (caller treats as "no trigger today",
    NOT as the fail-closed anchor — that's _fetch_earnings_anchor)."""
    if not FINNHUB_API_KEY:
        return None
    try:
        today = date.today()
        r = httpx.get(
            f"{FINNHUB_BASE}/calendar/earnings",
            params={
                "from": (today - timedelta(days=lookback_days)).isoformat(),
                "to": today.isoformat(),
                "symbol": ticker,
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        events = r.json().get("earningsCalendar", [])
        past = [e for e in events if e.get("date") and e["date"] <= today.isoformat()]
        if not past:
            return None
        return max(past, key=lambda e: e["date"])
    except Exception as e:
        logger.debug(f"{ticker}: earnings calendar lookup failed (non-fatal): {e}")
        return None


def _third_trading_day_after(earnings_date_str: str, hour: str) -> date | None:
    """3rd NYSE trading day after the market's first chance to react to the
    earnings release. AMC (after-close) releases push the reaction to the next
    session; BMO (before-open) and DMH react same-day. Day counting: reaction
    session = day 1, so "3rd trading day" = session_offset(reaction, +2)."""
    try:
        import exchange_calendars as xcals
        nyse = xcals.get_calendar("XNYS")
        session = nyse.date_to_session(earnings_date_str, direction="next")
        if hour == "amc":
            session = nyse.next_session(session)
        third = nyse.session_offset(session, 2)
        return third.date()
    except Exception as e:
        logger.warning(f"Trading-day offset calc failed for {earnings_date_str}: {e}")
        return None


def _is_triggered_today(ticker: str) -> bool:
    event = _get_last_earnings_event(ticker)
    if not event:
        return False
    third_day = _third_trading_day_after(event["date"], event.get("hour", ""))
    if third_day is None:
        return False
    return third_day == date.today()


# ── Fail-closed earnings anchor (issue #32 point 5: retry, then alert) ────────

def _fetch_earnings_anchor(ticker: str) -> dict | None:
    """Actual EPS/revenue surprise data — the anti-hallucination anchor. Retries
    same-day with backoff; on exhaustion returns None and the caller sends an
    explicit Telegram alert (not a silent failure) so the user can run the
    manual mode later. This is the ONE fail-closed step in the pipeline — no
    analysis proceeds without real reported numbers."""
    if not FINNHUB_API_KEY:
        return None
    last_error = None
    for attempt in range(EARNINGS_ANCHOR_MAX_RETRIES):
        try:
            if attempt > 0:
                wait = 5 * (2 ** attempt)
                logger.info(f"{ticker}: earnings anchor retry {attempt}/{EARNINGS_ANCHOR_MAX_RETRIES - 1} after {wait}s")
                time.sleep(wait)
            r = httpx.get(
                f"{FINNHUB_BASE}/stock/earnings",
                params={"symbol": ticker, "token": FINNHUB_API_KEY},
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            if data:
                return data[0]
            last_error = "empty response"
        except Exception as e:
            last_error = e
            logger.warning(f"{ticker}: earnings anchor fetch attempt {attempt + 1} failed: {e}")
    logger.error(f"{ticker}: earnings anchor exhausted {EARNINGS_ANCHOR_MAX_RETRIES} attempts: {last_error}")
    return None


# ── Context assembly ────────────────────────────────────────────────────────────

def _load_candidate_entries(ticker: str, max_entries: int = 10) -> str:
    """Pull this ticker's entries out of issue #31's SAS候选证据日志.md."""
    if not CANDIDATE_LOG_PATH.exists():
        return ""
    try:
        text = CANDIDATE_LOG_PATH.read_text(encoding="utf-8")
        pattern = re.compile(rf"^- \*\*{re.escape(ticker)}\*\* \[(.+?)\] (.+)$", re.MULTILINE)
        matches = pattern.findall(text)
        if not matches:
            return ""
        return "\n".join(f"- [{cat}] {fact}" for cat, fact in matches[-max_entries:])
    except Exception as e:
        logger.debug(f"Candidate log read failed (non-fatal): {e}")
        return ""


def _fetch_tavily_context(ticker: str) -> str:
    """One basic Tavily search for broader quarterly context. Shares the same
    daily budget pool as the AM/PM pipeline (load_budget/save_budget)."""
    budget = rf.load_budget()
    if rf.budget_remaining(budget) < 1:
        return ""
    try:
        year = date.today().year
        results = rf.tavily_search(
            f"{ticker} quarterly earnings review strategy {year}",
            budget, days=100, search_depth="basic", max_results=8,
        )
        rf.save_budget(budget)
        if not results:
            return ""
        lines = [f"- [{r.get('published_date', '?')}] {r.get('title', '')}: {r.get('content', '')[:300]}"
                 for r in results[:8]]
        return "\n".join(lines)
    except Exception as e:
        logger.warning(f"{ticker}: Tavily context fetch failed (non-fatal): {e}")
        return ""


def _load_sas_rubric() -> str:
    """Full SAS scoring methodology (Manual Section 7) — separate from
    run_finance.py's _load_framework(), which only extracts the daily-report
    excerpts (Section 2/6/7.4). Scoring needs the complete rubric text."""
    manual_path = OBSIDIAN / "Finance" / "Investment Operating Manual v1.0.md"
    if not manual_path.exists():
        return ""
    text = re.sub(r"^---.*?---\s*", "", manual_path.read_text(encoding="utf-8"), flags=re.DOTALL)
    m = re.search(r"7\. Strategic Alpha Score.*?\n\n(.*?)(?=\n\s*8\. 如何阅读 SAS)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


# ── Prompt + LLM call ────────────────────────────────────────────────────────────

SYSTEM_PROMPT_SAS = """你是一名专业投资分析师，负责对个人投资者的核心持仓做季度深度复盘，依据 Strategic Alpha Score（SAS）方法论对四个维度打分。

打分纪律（不可违反）：
- 四个维度（Strategic Space / Execution / Expectation Gap / Alpha Potential）的打分依据必须是执行事实、战略进展、内部信号（内部人交易、监管文件语言变化）——不得以股价涨跌、市值高低、市场情绪作为打分理由
- 若分析中需要引用股价相关的事实性数据（如距历史高点的回撤幅度、52周区间位置），必须直接使用下方提供的真实计算数据作为引用来源，不得凭记忆编造历史价格或百分比
- 每项打分范围 0-10，附 100-300 字依据，具体引用下方提供的事实来源（财报数字/内部人交易/风险披露变化/候选证据日志条目），不写空泛评价
- 若某维度缺乏足够事实支撑打分，如实说明证据不足，不得为了给出分数而编造理由

输出语言：中文。"""

USER_PROMPT_TEMPLATE_SAS = """标的：{ticker}
本次复盘日期：{today}
触发原因：{trigger_reason}

## SAS 评分方法论（Investment Operating Manual 第7节）
{rubric}

## 财报锚点（真实披露数据，防幻觉锚点）
{anchor_text}

## 持仓计算信号（真实计算值，仅供事实引用，不作为打分理由）
{holding_signal}

## 内部人交易（SEC Form 4，仅列开放市场真实买入，已排除RSU归属/缴税卖出等routine事件）
{insider_text}

## 监管文件语言变化（10-K risk factors，最近两期对比）
{risk_factor_text}

## SAS 候选证据日志（issue #31 日常流水线累积的相关记录）
{candidate_text}

## 综合情报（新闻+搜索，节选）
{news_text}
{tavily_text}

## 持仓与框架背景
{personal_context}

---

任务：对照上方 SAS 评分方法论，对 {ticker} 的 Strategic Space / Execution / Expectation Gap / Alpha Potential 四个维度打分，每项给出分数(0-10)+100-300字依据（引用具体事实来源，不写空泛评价，禁止以股价作为打分理由）。最后写一段不超过200字的综合总结。

请输出以下JSON（不要附加任何其他文字）：
{{
  "scores": {{
    "strategic_space": {{"score": 0, "rationale": "..."}},
    "execution": {{"score": 0, "rationale": "..."}},
    "expectation_gap": {{"score": 0, "rationale": "..."}},
    "alpha_potential": {{"score": 0, "rationale": "..."}}
  }},
  "summary": "..."
}}
"""


def _call_sas_review_llm(system_prompt: str, user_prompt: str, max_retries: int = 2) -> dict:
    """No fallback model for v1 (deliberate, per issue #32). Extracts the
    authoritative per-request USD cost from OpenRouter's usage.cost field
    (verified present on 2026-07-09 — no hardcoded price table needed)."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            if attempt > 0:
                wait = 2 ** attempt
                logger.info(f"SAS review LLM retry {attempt}/{max_retries} after {wait}s")
                time.sleep(wait)
            resp = httpx.post(
                rf.OR_BASE_URL,
                headers={
                    "Authorization": f"Bearer {rf.OPENROUTER_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": SAS_REVIEW_MODEL,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    "max_tokens": 4000,
                    "temperature": 0.2,
                },
                timeout=180,
            )
            resp.raise_for_status()
            data = resp.json()
            usage = data.get("usage", {})
            content = data["choices"][0]["message"].get("content", "")
            json_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            json_str = json_match.group(1) if json_match else content.strip()
            json_str = re.sub(r"^[^{]*", "", json_str)
            json_str = re.sub(r"[^}]*$", "", json_str)
            result = json.loads(json_str)
            result["_cost_usd"] = usage.get("cost", 0.0)
            result["_model_resolved"] = data.get("model", SAS_REVIEW_MODEL)
            return result
        except Exception as e:
            last_error = e
            logger.warning(f"SAS review LLM attempt {attempt + 1} failed: {e}")
    logger.error(f"SAS review LLM exhausted retries: {last_error}")
    return {}


# ── Output ───────────────────────────────────────────────────────────────────────

_DIM_LABELS = {
    "strategic_space": "Strategic Space",
    "execution": "Execution",
    "expectation_gap": "Expectation Gap",
    "alpha_potential": "Alpha Potential",
}


def _format_review_markdown(ticker: str, today: str, result: dict, anchor: dict) -> str:
    scores = result.get("scores", {})
    lines = [f"## {today}", ""]
    if anchor:
        lines.append(
            f"财报锚点：{anchor.get('period', '?')} 季度 EPS 实际 {anchor.get('actual', '?')} "
            f"vs 预期 {anchor.get('estimate', '?')}（surprise {anchor.get('surprisePercent', '?')}%）"
        )
        lines.append("")
    for key, label in _DIM_LABELS.items():
        d = scores.get(key, {})
        lines.append(f"**{label}**：{d.get('score', '?')}/10")
        lines.append(d.get("rationale", ""))
        lines.append("")
    if result.get("summary"):
        lines.append(f"**总结**：{result['summary']}")
        lines.append("")
    lines.append(f"_本次分析成本：${result.get('_cost_usd', 0):.4f}（{result.get('_model_resolved', SAS_REVIEW_MODEL)}）_")
    lines.append("")
    lines.append("---")
    return "\n".join(lines)


def _atomic_append(path: Path, entry: str, header: str) -> None:
    """Read-existing + write-to-temp + os.replace — the file's whole prior
    content is rewritten atomically rather than opened in append mode, so a
    crash mid-write can't leave a half-written entry (global CLAUDE.md 破坏性
    文件写入安全; this data has multi-quarter comparison value, worth the
    extra rigor over run_finance.py's simpler open(path,'a') pattern)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else header
    new_content = existing.rstrip("\n") + "\n\n" + entry + "\n"
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(new_content, encoding="utf-8")
    os.replace(tmp_path, path)


def write_sas_review_file(ticker: str, today: str, result: dict, anchor: dict) -> Path:
    path = SAS_REVIEW_DIR / f"{ticker}.md"
    header = f"---\nsource: Daily Intelligence SAS季度深度复盘\nticker: {ticker}\n---\n\n# {ticker} SAS 季度深度复盘\n"
    entry = _format_review_markdown(ticker, today, result, anchor)
    _atomic_append(path, entry, header)
    logger.info(f"SAS review written → {path}")
    return path


def send_review_email(ticker: str, today: str, result: dict, anchor: dict) -> None:
    wl = rf.load_watchlist()
    recipients = wl.get("recipients", [])
    if not recipients:
        logger.warning("No recipients configured, skipping SAS review email")
        return
    body = f"# {ticker} SAS 季度深度复盘 — {today}\n\n" + _format_review_markdown(ticker, today, result, anchor)
    send_report(subject=f"[Daily_Intel] {ticker} 季度 SAS 复盘 {today}", markdown_body=body, recipients=recipients)


# ── Orchestration ────────────────────────────────────────────────────────────────

def run_ticker_review(ticker: str, trigger_reason: str) -> bool:
    logger.info(f"Running SAS review for {ticker} ({trigger_reason})")

    anchor = _fetch_earnings_anchor(ticker)
    if not anchor:
        rf.send_telegram_alert(
            f"[!] SAS季度复盘 {ticker} 失败：财报锚点数据拉取重试耗尽（Finnhub /stock/earnings），"
            f"未生成分析。可手工重跑：sas_review.py --ticker {ticker}"
        )
        return False

    holding_signal = rf._compute_holding_signals() or "（无计算信号）"
    insider_buys = seu.get_insider_buys(ticker, lookback_days=INSIDER_LOOKBACK_DAYS)
    insider_text = (
        "\n".join(
            f"- {b['date']} {b.get('insider', '?')}（{b.get('position', '?')}）"
            f"买入 {b['shares']} 股 @ {b.get('price_per_share', '?')}（价值约 {b.get('value', '?')}）"
            for b in insider_buys
        )
        if insider_buys else "近期无公开市场买入记录（不代表负面信号，多数季度本就没有）"
    )
    risk_diff = seu.get_risk_factor_diff_input(ticker)
    risk_factor_text = (
        f"最新（{risk_diff['latest']['period']}）：\n{risk_diff['latest']['text'][:3000]}\n\n"
        f"上一期（{risk_diff['previous']['period']}）：\n{risk_diff['previous']['text'][:3000]}"
        if risk_diff else "（无法获取两期可对比的10-K risk factors）"
    )
    candidate_text = _load_candidate_entries(ticker) or "（候选证据日志无该标的记录）"
    news_text = rf.fetch_finnhub_news([ticker], hours=FINNHUB_NEWS_LOOKBACK_HOURS) or "（无 Finnhub 新闻）"
    tavily_text = _fetch_tavily_context(ticker) or "（无 Tavily 搜索结果）"
    personal_context = rf._load_personal_context()
    rubric = _load_sas_rubric() or "（无法读取 Manual 第7节，检查文件路径）"

    today = date.today().isoformat()
    user_prompt = USER_PROMPT_TEMPLATE_SAS.format(
        ticker=ticker, today=today, trigger_reason=trigger_reason,
        rubric=rubric,
        anchor_text=json.dumps(anchor, ensure_ascii=False, indent=2),
        holding_signal=holding_signal,
        insider_text=insider_text,
        risk_factor_text=risk_factor_text,
        candidate_text=candidate_text,
        news_text=news_text,
        tavily_text=tavily_text,
        personal_context=personal_context,
    )
    result = _call_sas_review_llm(SYSTEM_PROMPT_SAS, user_prompt)
    if not result or "scores" not in result:
        rf.send_telegram_alert(
            f"[!] SAS季度复盘 {ticker} 失败：LLM 调用未返回有效打分结果。可手工重跑：sas_review.py --ticker {ticker}"
        )
        return False

    write_sas_review_file(ticker, today, result, anchor)
    send_review_email(ticker, today, result, anchor)
    logger.info(f"SAS review complete for {ticker}, cost=${result.get('_cost_usd', 0):.4f}")
    return True


def main():
    parser = argparse.ArgumentParser(description="Quarterly SAS deep-dive (issue #32)")
    parser.add_argument("--ticker", help="Manually run for a specific ticker, bypassing trigger checks")
    args = parser.parse_args()

    if args.ticker:
        run_ticker_review(args.ticker.upper(), trigger_reason="手工指定")
        return

    tracked = _update_tracked_tickers()
    if not tracked:
        logger.info("No tracked tickers yet, exiting")
        return

    any_triggered = False
    for ticker in tracked:
        if _is_triggered_today(ticker):
            any_triggered = True
            run_ticker_review(ticker, trigger_reason="财报后第3个交易日")
    if not any_triggered:
        logger.info("No ticker triggered today, exiting silently")


if __name__ == "__main__":
    main()

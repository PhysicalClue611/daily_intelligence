"""
Daily Intelligence — budget trackers
=====================================
Load/save/remaining helpers for every quota- or spend-capped external service
(Tavily, SerpApi, Adanos, Apify, Brave). Extracted from run_finance.py (issue
#42, 2026-07-17) to shrink that file. The genuinely duplicated boilerplate
(read JSON / check reset period key / atomic write) across these five is now
parameterized into `budget_tracker.py`'s `load_quota`/`save_quota`/`remaining`
(issue #41) — the functions below are thin per-service wrappers over that
primitive. What is deliberately NOT folded into the shared primitive: the
increment amount and the trigger condition for counting a call against the
quota, which differ per service (Adanos counts on any HTTP response before
raise_for_status; Apify counts on success and on timeout-after-send but not
on connect error; Brave counts on success only and self-saves inside its own
fetch function) — those stay at each call site in run_finance.py/
intel_sources.py, unchanged.

Leaf module: does not import from run_finance.py, to avoid a circular import
(run_finance.py imports these names back for its own use and for
sas_review.py's `rf.load_budget` / `rf.budget_remaining` access pattern).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from budget_tracker import load_quota, save_quota, remaining

_PROJ_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent
ET = ZoneInfo("America/New_York")

BUDGET_PATH           = _PROJ_DIR / "finance_tavily_budget.json"
TAVILY_DAILY_LIMIT     = 20
SERPAPI_BUDGET_PATH    = _PROJ_DIR / "finance_serpapi_budget.json"
SERPAPI_MONTHLY_LIMIT  = 250
ADANOS_BUDGET_PATH     = _PROJ_DIR / "finance_adanos_budget.json"
ADANOS_MONTHLY_LIMIT   = 200  # free tier is 250/mo; leave headroom for TG follow-ups/manual reruns
APIFY_BUDGET_PATH      = _PROJ_DIR / "finance_apify_budget.json"
APIFY_MONTHLY_LIMIT    = 60   # runs/month, not results
BRAVE_BUDGET_PATH      = _PROJ_DIR / "finance_brave_budget.json"
BRAVE_MONTHLY_LIMIT    = 800  # conservative under the ~1000-query $5 credit estimate

# ── Tavily budget ────────────────────────────────────────────────────────────

def load_budget() -> dict:
    return load_quota(BUDGET_PATH, "daily")


def save_budget(budget: dict) -> None:
    """Atomic write via the shared budget_tracker primitive — see its
    save_quota() docstring for why this must never be a direct
    open(path,'w')/write_text() (global CLAUDE.md 破坏性文件写入安全). See
    issue #41."""
    save_quota(BUDGET_PATH, budget)


def budget_remaining(budget: dict) -> int:
    return remaining(budget, TAVILY_DAILY_LIMIT)


# ── SerpApi monthly budget ────────────────────────────────────────────────────

def _this_month_et() -> str:
    return datetime.now(ET).strftime("%Y-%m")

def load_serpapi_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(SERPAPI_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_serpapi_budget(budget: dict) -> None:
    """Atomic write (temp file + os.replace) — see save_budget() docstring; same
    reasoning applies to this monthly counter. See issue #41."""
    tmp_path = SERPAPI_BUDGET_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(budget))
    os.replace(tmp_path, SERPAPI_BUDGET_PATH)


# ── Adanos monthly budget ─────────────────────────────────────────────────────

def load_adanos_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(ADANOS_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_adanos_budget(budget: dict) -> None:
    """Atomic write (temp file + os.replace) — see save_budget() docstring; same
    reasoning applies to this monthly counter. See issue #41."""
    tmp_path = ADANOS_BUDGET_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(budget))
    os.replace(tmp_path, ADANOS_BUDGET_PATH)

def adanos_remaining(budget: dict) -> int:
    return max(0, ADANOS_MONTHLY_LIMIT - budget.get("used", 0))


# ── Apify (Reddit sentiment) monthly budget ────────────────────────────────────

def load_apify_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(APIFY_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_apify_budget(budget: dict) -> None:
    """Atomic write — this file enforces a real-money spending cap (global CLAUDE.md
    破坏性文件写入安全), same reasoning as save_brave_budget()."""
    tmp_path = APIFY_BUDGET_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(budget))
    os.replace(tmp_path, APIFY_BUDGET_PATH)

def apify_remaining(budget: dict) -> int:
    return max(0, APIFY_MONTHLY_LIMIT - budget.get("used", 0))


# ── Brave News monthly budget ─────────────────────────────────────────────────

def load_brave_budget() -> dict:
    ym = _this_month_et()
    try:
        data = json.loads(BRAVE_BUDGET_PATH.read_text())
        if data.get("year_month") == ym:
            return data
    except Exception:
        pass
    return {"year_month": ym, "used": 0}

def save_brave_budget(budget: dict) -> None:
    """Atomic write (temp file + os.replace) — never open(path,'w')/write_text()
    directly on this file (global CLAUDE.md 破坏性文件写入安全): a crash or kill
    mid-write would truncate it, and the next load_brave_budget() would then
    silently reset the hard spending cap this file exists to enforce."""
    tmp_path = BRAVE_BUDGET_PATH.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(budget))
    os.replace(tmp_path, BRAVE_BUDGET_PATH)

def brave_remaining(budget: dict) -> int:
    return max(0, BRAVE_MONTHLY_LIMIT - budget.get("used", 0))

def serpapi_remaining(budget: dict) -> int:
    return max(0, SERPAPI_MONTHLY_LIMIT - budget.get("used", 0))


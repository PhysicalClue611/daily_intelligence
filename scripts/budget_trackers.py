"""
Daily Intelligence — budget trackers
=====================================
Load/save/remaining helpers for every quota- or spend-capped external service
(Tavily, SerpApi, Adanos, Apify, Brave). Extracted from run_finance.py (issue
#42, 2026-07-17) to shrink that file. The genuinely duplicated boilerplate
(read JSON / check reset period key / atomic write) across these five is now
parameterized into `quota_store.py`'s `load_quota`/`save_quota`/`remaining`
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
import os
from pathlib import Path

from quota_store import load_quota, save_quota, remaining

_PROJ_DIR = Path(os.path.dirname(os.path.abspath(__file__))).parent

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
    """Atomic write via the shared quota_store primitive — see its
    save_quota() docstring for why this must never be a direct
    open(path,'w')/write_text() (global CLAUDE.md 破坏性文件写入安全). See
    issue #41."""
    save_quota(BUDGET_PATH, budget)


def budget_remaining(budget: dict) -> int:
    return remaining(budget, TAVILY_DAILY_LIMIT)


# ── SerpApi monthly budget ────────────────────────────────────────────────────

def load_serpapi_budget() -> dict:
    return load_quota(SERPAPI_BUDGET_PATH, "monthly")

def save_serpapi_budget(budget: dict) -> None:
    """Atomic write via the shared quota_store primitive — see
    save_quota() docstring. See issue #41."""
    save_quota(SERPAPI_BUDGET_PATH, budget)

def serpapi_remaining(budget: dict) -> int:
    return remaining(budget, SERPAPI_MONTHLY_LIMIT)


# ── Adanos monthly budget ─────────────────────────────────────────────────────

def load_adanos_budget() -> dict:
    return load_quota(ADANOS_BUDGET_PATH, "monthly")

def save_adanos_budget(budget: dict) -> None:
    """Atomic write via the shared quota_store primitive — see
    save_quota() docstring. See issue #41."""
    save_quota(ADANOS_BUDGET_PATH, budget)

def adanos_remaining(budget: dict) -> int:
    return remaining(budget, ADANOS_MONTHLY_LIMIT)


# ── Apify (Reddit sentiment) monthly budget ────────────────────────────────────

def load_apify_budget() -> dict:
    return load_quota(APIFY_BUDGET_PATH, "monthly")

def save_apify_budget(budget: dict) -> None:
    """Atomic write via the shared quota_store primitive — this file
    enforces a real-money spending cap (global CLAUDE.md 破坏性文件写入安全).
    See save_quota() docstring."""
    save_quota(APIFY_BUDGET_PATH, budget)

def apify_remaining(budget: dict) -> int:
    return remaining(budget, APIFY_MONTHLY_LIMIT)


# ── Brave News monthly budget ─────────────────────────────────────────────────

def load_brave_budget() -> dict:
    return load_quota(BRAVE_BUDGET_PATH, "monthly")

def save_brave_budget(budget: dict) -> None:
    """Atomic write via the shared quota_store primitive — this file
    enforces a real-money spending cap (global CLAUDE.md 破坏性文件写入安全).
    See save_quota() docstring."""
    save_quota(BRAVE_BUDGET_PATH, budget)

def brave_remaining(budget: dict) -> int:
    return remaining(budget, BRAVE_MONTHLY_LIMIT)


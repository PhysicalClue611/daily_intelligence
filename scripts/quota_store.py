"""
Daily Intelligence — generic budget/quota tracker primitive (issue #41).

Parameterized load/reset/save/remaining for integer-usage, period-keyed budget
files (Tavily/SerpApi/Adanos/Apify/Brave). Does NOT encode increment amount,
increment trigger (count-on-response vs success-only vs timeout-carve-out), or
who calls save() — those stay at each call site by design; folding them in
here would collapse real behavioral differences documented in issue #41's
call-site survey (Adanos counts on any HTTP response before raise_for_status,
Apify counts on success + timeout-after-send but not on connect error, Brave
counts on success only and self-saves inside its own fetch function). This
module only removes the "read JSON, check reset key, atomic-write JSON"
boilerplate that was genuinely duplicated five times.

Leaf module: no import from run_finance.py, to avoid a circular import
(budget_trackers.py imports these names, and run_finance.py re-imports
budget_trackers.py's names for its own use and for sas_review.py's
`rf.load_budget` / `rf.budget_remaining` cross-module access pattern).
"""
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
Period = Literal["daily", "monthly"]


def _period_key(period: Period) -> tuple[str, str]:
    """Returns (key_name, current_value) for the given reset period."""
    now = datetime.now(ET)
    if period == "daily":
        return "date", now.strftime("%Y-%m-%d")
    return "year_month", now.strftime("%Y-%m")


def load_quota(path: Path, period: Period, extra_defaults: dict | None = None) -> dict:
    """Load a period-keyed usage counter file. Returns a fresh dict (period key +
    used=0 + any extra_defaults) if the file is missing, unparseable, or from a
    prior period — mirrors the original five trackers' hardcoded-fresh-dict-on-
    mismatch behavior exactly. (Parallel's tracker in telegram_commands.py uses
    a different partial-schema-backfill contract and is intentionally not
    routed through this function — see issue #41.)"""
    key_name, current = _period_key(period)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            if data.get(key_name) == current:
                return data
        except Exception:
            pass
    fresh = {key_name: current, "used": 0}
    if extra_defaults:
        fresh.update(extra_defaults)
    return fresh


def save_quota(path: Path, budget: dict) -> None:
    """Atomic write (temp file + os.replace) — never open(path,'w')/write_text()
    directly on this file (global CLAUDE.md 破坏性文件写入安全): a crash or kill
    mid-write would truncate it, and the next load_quota() would then silently
    reset the quota/spend cap this file exists to enforce."""
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(budget))
    os.replace(tmp_path, path)


def remaining(budget: dict, limit: int, used_field: str = "used") -> int:
    """max(0, limit - used) — identical domain/formula across all five
    integer-based trackers."""
    return max(0, limit - budget.get(used_field, 0))

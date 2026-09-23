"""Pass 2 background signals that are useful only when their state changes."""

import json
import re
from datetime import datetime
from pathlib import Path

from intel_collect import archived_intel_snapshot_paths


def read_previous_intel_snapshot_state(root: Path, before: datetime) -> dict:
    """Read the newest previously completed report state from an intelligence snapshot."""
    for path in archived_intel_snapshot_paths(root):
        try:
            intel_snapshot = json.loads(path.read_text(encoding="utf-8"))
            if datetime.fromisoformat(intel_snapshot["as_of"]) < before and isinstance(intel_snapshot.get("context_state"), dict):
                return intel_snapshot["context_state"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return {}


def current_state(liquidity_section: str, weights: dict, stats: dict,
                  previous: dict | None = None) -> dict:
    previous = previous or {}
    tier = re.search(r"整体[：:]\s*(正常|观察|警戒)", liquidity_section or "")
    extrema = dict(previous.get("range_extrema", {}))
    for ticker, item in stats.items():
        if {"latest_close", "high_close", "low_close"} <= item.keys():
            extrema[ticker] = {"close": item["latest_close"],
                               "high": item["high_close"], "low": item["low_close"]}
    return {"liquidity_tier": tier.group(1) if tier else previous.get("liquidity_tier"),
            "weights": weights or previous.get("weights", {}), "range_extrema": extrema}


def changed_background(liquidity_section: str, current: dict, previous: dict) -> tuple[str, str]:
    """Inject a FRED tier change, position overload/crossing, and true 52w extrema."""
    old_tier = previous.get("liquidity_tier")
    new_tier = current.get("liquidity_tier")
    liquidity = liquidity_section if new_tier and old_tier and new_tier != old_tier else ""
    lines = []
    old_weights = previous.get("weights", {})
    for ticker, weight in current.get("weights", {}).items():
        old = old_weights.get(ticker)
        if (weight > 15 and (old is None or old <= 15)) or (old is not None and old > 15 >= weight):
            label = "超过15%" if weight > 15 else "降至15%及以下"
            lines.append(f"- {ticker} 占组合{weight}%（{label}）")
    old_extrema = previous.get("range_extrema", {})
    for ticker, item in current.get("range_extrema", {}).items():
        close = item.get("close")
        old = old_extrema.get(ticker, {})
        if close is None:
            continue
        if close >= item.get("high", float("inf")) and (not old or close > old.get("high", float("inf"))):
            lines.append(f"- {ticker} 收盘价{close}，52周新高")
        elif close <= item.get("low", float("-inf")) and (not old or close < old.get("low", float("-inf"))):
            lines.append(f"- {ticker} 收盘价{close}，52周新低")
    return liquidity, ("【本次变化的背景信号】\n" + "\n".join(lines)) if lines else ""

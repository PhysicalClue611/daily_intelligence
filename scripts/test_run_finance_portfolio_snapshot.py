"""
Regression test for _get_portfolio_snapshot() / _get_core_holding_tickers() /
_get_portfolio_weights() (run_finance.py).

Root cause: all three functions matched the IB section header with a hardcoded
account number regex `## IB（账户 0611）`. portfolio-agent switched to rendering
the real IB account id (`## IB（账户 U10907387）`) instead of the old masked
"0611" label, so the header regex silently stopped matching — `ib_section` came
back None, and each function fell back to an empty/header-only result. In
production this meant the AM/PM Pass 2 prompt received zero IB holdings (no
ticker list, no cost basis, no position weights), which is indistinguishable
from "holdings section omitted" in the report.

No pytest dependency — writes a fake portfolio_report_latest.md under a temp
OBSIDIAN root and monkeypatches rf.OBSIDIAN, matching the pattern used by the
other test_*.py scripts in this directory.

Run: python scripts/test_run_finance_portfolio_snapshot.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import run_finance as rf

FAKE_REPORT = """# 投资组合持仓报告
**生成时间（美东）：2026-08-29 22:30 EDT**

**组合总市值：4,667,708 CNY　/　694,517 USD**

## IB（账户 U10907387）

### AMKR AMKR U10907387
- 品种：美股　货币：USD
- 持仓：150.0　均价：46.77333535　总成本：7016.00
- 现价：48.1200　市值：7218.00　浮盈：+202.00（+2.9%）
- 备注：TWS自动拉取

### INTC INTC U10907387
- 品种：美股　货币：USD
- 持仓：600.0　均价：32.617522　总成本：19570.51
- 现价：89.2500　市值：53550.00　浮盈：+33979.49（+173.6%）
- 备注：TWS自动拉取

### QQQM QQQM U10907387
- 品种：美股　货币：USD
- 持仓：450.0　均价：132.4551111　总成本：59604.80
- 现价：295.0000　市值：132750.00　浮盈：+73145.20（+122.7%）
- 备注：TWS自动拉取

### CASH IB现金（USD） U10907387
- 品种：现金　货币：USD
- 持仓：1208.05　均价：1.0　总成本：1208.05
- 现价：1.0000　市值：1208.05　浮盈：+0.00（+0.0%）
- 备注：TWS自动拉取

## 招商银行（账户 0514/0611/1281/7975/8174）

### CASH 现金 0514
- 品种：现金　货币：CNY
- 持仓：15280.0　均价：1.0　总成本：15280.00
- 现价：1.0000　市值：15280.00　浮盈：+0.00（+0.0%）
"""


def with_fake_obsidian(fn):
    with tempfile.TemporaryDirectory() as tmp:
        finance_dir = Path(tmp) / "Finance"
        finance_dir.mkdir()
        (finance_dir / "portfolio_report_latest.md").write_text(FAKE_REPORT, encoding="utf-8")
        original = rf.OBSIDIAN
        rf.OBSIDIAN = Path(tmp)
        try:
            return fn()
        finally:
            rf.OBSIDIAN = original


def test_snapshot_lists_all_ib_us_holdings():
    snapshot = with_fake_obsidian(rf._get_portfolio_snapshot)
    assert "AMKR" in snapshot, f"AMKR missing from snapshot:\n{snapshot}"
    assert "INTC" in snapshot, f"INTC missing from snapshot:\n{snapshot}"
    assert "QQQM" in snapshot, f"QQQM missing from snapshot:\n{snapshot}"
    assert "CASH" not in snapshot.split("IB美股持仓")[-1], "CASH row should be excluded from holdings"
    assert "成本@32.617522" in snapshot, f"INTC cost basis missing:\n{snapshot}"
    print("PASS: test_snapshot_lists_all_ib_us_holdings")


def test_core_holding_tickers_excludes_beta_and_cash():
    tickers = with_fake_obsidian(rf._get_core_holding_tickers)
    assert "AMKR" in tickers, f"AMKR missing from tickers: {tickers}"
    assert "INTC" in tickers, f"INTC missing from tickers: {tickers}"
    assert "QQQM" not in tickers, f"QQQM (beta layer) should be excluded: {tickers}"
    assert "CASH" not in tickers, f"CASH should be excluded: {tickers}"
    print("PASS: test_core_holding_tickers_excludes_beta_and_cash")


def test_portfolio_weights_nonempty_and_plausible():
    weights = with_fake_obsidian(rf._get_portfolio_weights)
    assert weights, "weights dict should not be empty"
    assert "INTC" in weights, f"INTC missing from weights: {weights}"
    assert 0 < weights["INTC"] < 100, f"INTC weight implausible: {weights['INTC']}"
    print("PASS: test_portfolio_weights_nonempty_and_plausible")


if __name__ == "__main__":
    test_snapshot_lists_all_ib_us_holdings()
    test_core_holding_tickers_excludes_beta_and_cash()
    test_portfolio_weights_nonempty_and_plausible()
    print("\nAll tests passed.")

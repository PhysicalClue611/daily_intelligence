"""
SEC EDGAR helpers for issue #32 (quarterly SAS deep-dive) — insider transactions
(Form 4) and risk-factor language (10-K), via the free `edgartools` library.

Research findings (2026-07-09, see issue #32 comments for full detail):
  - Finnhub's institutional-ownership (13F) endpoint is paid-tier-gated on our key.
  - yfinance's option chain implied-volatility data is broken (bid/ask all zero,
    IV values follow an obvious placeholder pattern, not real market pricing).
  - `edgartools` gives clean per-ticker access to both Form 4 and 10-K without
    those problems — no API key needed, just a courtesy identity header per
    SEC's usage policy (https://www.sec.gov/os/accessing-edgar-data).

13F-by-security is NOT implemented here: SEC 13F filings are organized by
*filer* (each institution reports its whole portfolio), not by security —
answering "who holds ticker X" requires aggregating across many institutions'
filings, which is a standing-index-building project, not a quarterly-script
task. See issue #32 Non-goals.
"""
import logging
import os
from datetime import date, timedelta

logger = logging.getLogger(__name__)

# SEC requests a contact identity on every request (not an API key — no auth,
# just a courtesy header so they can reach out if a script misbehaves).
_EDGAR_CONTACT = os.getenv("FINANCE_FROM_ADDRESS", "fintel@physicalclue.us")
_identity_set = False

# Form 4 Table I/II transaction codes (SEC standard, see Form 4 instructions).
# 'P' = open-market or private purchase — the only code that represents a
# genuine discretionary buy. Everything else (M=option exercise, F=tax
# withholding, A=grant/award, G=gift, S=sale, etc.) is routine equity-comp
# machinery and does NOT indicate the insider chose to put money in.
_DISCRETIONARY_BUY_CODE = "P"


def _ensure_identity() -> None:
    global _identity_set
    if _identity_set:
        return
    import edgar
    edgar.set_identity(f"Daily Intelligence Research {_EDGAR_CONTACT}")
    _identity_set = True


def get_insider_buys(ticker: str, lookback_days: int = 120) -> list[dict]:
    """Genuine discretionary insider purchases (Form 4, code 'P', non-derivative
    common stock) for `ticker` in the last `lookback_days`. Fail-open: returns
    [] on any error (network, parse, ticker not found) rather than raising —
    this is a supplementary signal, not the fail-closed anchor."""
    try:
        _ensure_identity()
        import edgar
        company = edgar.Company(ticker)
        start = (date.today() - timedelta(days=lookback_days)).isoformat()
        end = date.today().isoformat()
        filings = company.get_filings(form="4", date=(start, end))
    except Exception as e:
        logger.warning(f"{ticker}: Form 4 filing list fetch failed (non-fatal): {e}")
        return []

    buys: list[dict] = []
    for f in filings:
        try:
            obj = f.obj()
            summary = obj.get_ownership_summary()
            for t in summary.transactions:
                if t.code != _DISCRETIONARY_BUY_CODE or t.security_type != "non-derivative":
                    continue
                if not t.shares:
                    continue
                buys.append({
                    "date": str(f.filing_date),
                    "insider": summary.insider_name,
                    "position": summary.position,
                    "shares": t.shares,
                    "price_per_share": t.price_per_share,
                    "value": t.value,
                })
        except Exception as e:
            logger.debug(f"{ticker}: Form 4 filing parse failed (non-fatal): {e}")
            continue
    return buys


def get_risk_factor_diff_input(ticker: str, max_chars: int = 6000) -> dict:
    """Latest two 10-K risk-factor sections for `ticker`, truncated for LLM
    injection. Language-change detection is inherently semantic (rephrasing,
    added/dropped risk categories, tone shift) — handed to the LLM as raw text
    pairs rather than diffed programmatically. Fail-open: returns {} if fewer
    than 2 10-Ks are available or any step fails."""
    try:
        _ensure_identity()
        import edgar
        company = edgar.Company(ticker)
        filings = company.get_filings(form="10-K").latest(2)
    except Exception as e:
        logger.warning(f"{ticker}: 10-K filing list fetch failed (non-fatal): {e}")
        return {}

    filings = list(filings)
    if len(filings) < 2:
        return {}

    out = {}
    labels = ["latest", "previous"]
    for label, f in zip(labels, filings):
        try:
            obj = f.obj()
            text = (obj.risk_factors or "")[:max_chars]
            if text:
                out[label] = {"period": str(f.filing_date), "text": text}
        except Exception as e:
            logger.debug(f"{ticker}: 10-K risk_factors parse failed for {label} (non-fatal): {e}")
    return out if len(out) == 2 else {}

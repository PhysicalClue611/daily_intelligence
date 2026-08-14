#!/usr/bin/env python3
"""
Price fetcher — yfinance on the host machine.
Runs outside Docker so fc.yahoo.com (cookie/crumb source) is reachable.
Finnhub is used as fallback for stocks/ETFs if yfinance fails (no pre-market,
no commodities/FX on free tier — those will be skipped with a warning).

NOTE: 429 errors during dev sessions are caused by manual testing volume.
      Production runs once/day at 5:30 AM and are well within rate limits.
"""
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import time as dtime
from zoneinfo import ZoneInfo

import httpx

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY", "")
FINNHUB_BASE    = "https://finnhub.io/api/v1"

# Tickers that Finnhub free tier can't handle (futures/FX/indices)
_FINNHUB_UNSUPPORTED = {"GC=F", "CL=F", "^TNX", "USDCNY=X", "USDJPY=X", "DX-Y.NYB"}

DISPLAY_NAMES = {
    "GC=F":     "黄金期货",
    "CL=F":     "WTI原油",
    "^TNX":     "美十年债收益率",
    "USDCNY=X": "美元/人民币",
    "USDJPY=X": "美元/日元",
    "DX-Y.NYB": "美元指数",
    "QQQM":     "QQQM(纳斯达克)",
    "VOO":      "VOO(标普500)",
    "EWJ":      "EWJ(日本ETF)",
    "SGOL":     "SGOL(黄金ETF)",
    "INTC":     "英特尔(INTC)",
    "NVDA":     "英伟达(NVDA)",
    "ORCL":     "甲骨文(ORCL)",
    "TSLA":     "特斯拉(TSLA)",
    "AMKR":     "AMKR",
}


@dataclass
class PriceRow:
    ticker: str
    display: str
    price: float
    prev_close: float
    change_pct: float        # day change % (premarket vs prev_close for AM; close vs prev_close for PM/daily)
    week_change_pct: float   # 5-day change %
    is_anomaly: bool
    unit: str                # "$", "%", ""
    afterhours_price: float = 0.0   # PM only: latest after-hours price (0 = unavailable)
    afterhours_pct: float = 0.0     # PM only: after-hours change % vs today's close
    session_change_pct: float = 0.0 # AM: prev day close vs open; PM: today close vs today open
    slot: str = "daily"             # "am", "pm", "daily"


def _yf_download(yf_mod, tickers, **kwargs):
    """yf.download with yfinance ERROR noise suppressed (issue #63 / #67)."""
    with _quiet_yfinance_logs():
        return yf_mod.download(tickers, **kwargs)


def _ohlc_series(data, field: str, ticker: str):
    """One ticker's Close/Open series from a group_by=column yf.download frame.

    A collapsed retry (yfinance dropped every symbol but one) must not treat
    the leftover column / flat Series as a different ticker. Same rule as
    `_closes_from_bulk` for n_tickers > 1: no name match → None.
    """
    if data is None or getattr(data, "empty", True):
        return None
    try:
        block = data[field]
        if hasattr(block, "columns"):
            if ticker in block.columns:
                return block[ticker]
            if block.shape[1] == 1 and str(block.columns[0]) == ticker:
                return block.iloc[:, 0]
            return None
        if getattr(block, "name", None) == ticker:
            return block
        return None
    except Exception:
        return None


def _close_usable(data, ticker: str) -> bool:
    closes = _ohlc_series(data, "Close", ticker)
    return closes is not None and len(closes.dropna()) >= 2


def _missing_close_tickers(data, tickers: list[str]) -> list[str]:
    """Tickers whose Close series has fewer than 2 non-NaN rows (or is absent)."""
    if not tickers:
        return []
    if data is None or getattr(data, "empty", True):
        return list(tickers)
    return [t for t in tickers if not _close_usable(data, t)]


def _merge_daily_ohlc(first, retry, tickers: list[str]):
    """Keep first-frame Yahoo rows; overlay retry only for tickers first missed.

    A worse non-throwing retry must not replace a usable first bulk (issue #67
    review). If first is empty and retry is not, take retry wholesale.
    """
    if first is None or getattr(first, "empty", True):
        return retry
    if retry is None or getattr(retry, "empty", True):
        return first
    first_miss = _missing_close_tickers(first, tickers)
    if not first_miss:
        return first
    out = first.copy()
    recovered: list[str] = []
    for ticker in first_miss:
        if not _close_usable(retry, ticker):
            continue
        for field in ("Close", "Open"):
            src = _ohlc_series(retry, field, ticker)
            if src is None:
                continue
            out[(field, ticker)] = src.reindex(out.index)
        if _close_usable(out, ticker):
            recovered.append(ticker)
    if recovered:
        logger.info(
            f"yfinance daily retry recovered {len(recovered)} ticker(s): {recovered}"
        )
    return out


def _fetch_intraday_data(all_tickers: list[str], yf):
    """Download 2-day 1-minute bars with pre/post market. Returns DataFrame or None."""
    try:
        data = _yf_download(
            yf,
            all_tickers,
            period="2d",
            interval="1m",
            prepost=True,
            auto_adjust=True,
            group_by="column",
            progress=False,
        )
        if data is None or data.empty:
            logger.warning("Intraday download returned empty data")
            return None
        logger.info(f"Intraday data fetched (prepost=True, {len(all_tickers)} tickers)")
        return data
    except Exception as e:
        logger.warning(f"Intraday download failed: {e}")
        return None


def _closes_1m(data, ticker: str, all_tickers: list[str]):
    """Extract 1m Close series for a ticker from the intraday DataFrame."""
    try:
        if len(all_tickers) == 1:
            s = data["Close"]
        else:
            s = data["Close"][ticker]
        return s.dropna()
    except Exception:
        return None


def _get_premarket_price(closes_1m, today_et_date) -> float | None:
    """Return latest pre-market bar price (before 09:30 ET today), or None."""
    if closes_1m is None or closes_1m.empty:
        return None
    try:
        idx_et = closes_1m.index.tz_convert(_ET)
        mask = (
            (idx_et.date == today_et_date) &
            (idx_et.time < dtime(9, 30))
        )
        filtered = closes_1m[mask]
        if filtered.empty:
            return None
        return float(filtered.iloc[-1])
    except Exception as e:
        logger.warning(f"_get_premarket_price error: {e}")
        return None


def _get_pm_prices(closes_1m, today_et_date) -> tuple[float | None, float | None]:
    """Return (today_close, afterhours_price) from intraday 1m data.

    today_close: last bar at or before 16:00 ET today (regular session close).
    afterhours_price: last bar after 16:00 ET today (no upper bound — AH runs until ~20:00 ET
    but some platforms extend further; also catches late prints).
    """
    if closes_1m is None or closes_1m.empty:
        return None, None
    try:
        idx_et = closes_1m.index.tz_convert(_ET)
        today_mask = idx_et.date == today_et_date
        today_bars = closes_1m[today_mask]

        if today_bars.empty:
            return None, None

        today_idx_et = today_bars.index.tz_convert(_ET)
        regular = today_bars[today_idx_et.time <= dtime(16, 0)]
        ah = today_bars[today_idx_et.time > dtime(16, 0)]

        close    = float(regular.iloc[-1]) if not regular.empty else None
        ah_price = float(ah.iloc[-1])      if not ah.empty      else None

        if ah_price is not None:
            last_ah_time = today_idx_et[today_idx_et.time > dtime(16, 0)][-1].strftime("%H:%M")
            logger.debug(f"AH bar found: {ah_price:.4f} at {last_ah_time} ET")

        return close, ah_price
    except Exception as e:
        logger.warning(f"_get_pm_prices error: {e}")
        return None, None


def _finnhub_single_ticker(
    ticker: str,
    stocks: list[str],
    fx: list[str],
    commodities: list[str],
    thresholds: dict,
    slot: str = "daily",
) -> "PriceRow | None":
    """Fetch one ticker from Finnhub. For AM slot also attempts yf.Ticker.info for premarket.
    Returns None if Finnhub lacks data or ticker is unsupported."""
    if ticker in _FINNHUB_UNSUPPORTED or not FINNHUB_API_KEY:
        return None
    stock_thresh     = float(thresholds.get("stock_pct", 3.0))
    commodity_thresh = float(thresholds.get("commodity_pct", 2.0))
    fx_thresh        = float(thresholds.get("fx_pct", 1.0))
    tnx_bps          = float(thresholds.get("tnx_bps", 10))
    try:
        r = httpx.get(
            f"{FINNHUB_BASE}/quote",
            params={"symbol": ticker, "token": FINNHUB_API_KEY},
            timeout=8,
        )
        d = r.json()
        price      = float(d.get("c") or 0)
        prev_close = float(d.get("pc") or 0)
        if price == 0 or prev_close == 0:
            return None
        change_pct = float(d.get("dp") or 0)

        # AM slot: try yf.Ticker.info for premarket price (different Yahoo endpoint — often
        # succeeds even when yf.download times out).
        if slot == "am":
            try:
                import yfinance as _yf
                info = _yf.Ticker(ticker).info
                pm_price = info.get("preMarketPrice")
                pc_yf    = info.get("regularMarketPreviousClose") or prev_close
                if pm_price and pc_yf:
                    price      = float(pm_price)
                    prev_close = float(pc_yf)
                    change_pct = (price - prev_close) / prev_close * 100
            except Exception as _e:
                logger.warning(f"{ticker} AM per-ticker: yf.Ticker.info failed: {_e}")

        if ticker == "^TNX":
            is_anomaly = abs(price - prev_close) * 100 >= tnx_bps
            unit = "%"
        elif ticker in fx:
            is_anomaly = abs(change_pct) >= fx_thresh
            unit = ""
        elif ticker in commodities:
            is_anomaly = abs(change_pct) >= commodity_thresh
            unit = "$"
        else:
            is_anomaly = abs(change_pct) >= stock_thresh
            unit = "$"

        logger.info(f"{ticker}: per-ticker Finnhub fallback succeeded "
                    f"price={price:.4f} prev={prev_close:.4f} chg={change_pct:+.2f}% slot={slot}")
        return PriceRow(
            ticker=ticker,
            display=DISPLAY_NAMES.get(ticker, ticker),
            price=price,
            prev_close=prev_close,
            change_pct=change_pct,
            week_change_pct=0.0,
            is_anomaly=is_anomaly,
            unit=unit,
            slot=slot,
        )
    except Exception as e:
        logger.warning(f"Finnhub per-ticker fallback {ticker}: {e}")
        return None


def _fetch_prices_finnhub(
    all_tickers: list[str],
    stocks: list[str],
    fx: list[str],
    commodities: list[str],
    thresholds: dict,
    slot: str = "daily",
) -> list["PriceRow"]:
    """Finnhub fallback for stocks/ETFs only. Commodities/FX are skipped.
    Finnhub free tier has no pre-market/after-hours data — always returns regular-session prices."""
    if slot in ("am", "pm"):
        logger.warning(f"Finnhub fallback: no pre/post-market data on free tier, using regular-session prices (slot={slot})")

    if not FINNHUB_API_KEY:
        logger.warning("FINNHUB_API_KEY not set, cannot use Finnhub fallback")
        return []
    stock_thresh     = float(thresholds.get("stock_pct", 3.0))
    commodity_thresh = float(thresholds.get("commodity_pct", 2.0))
    fx_thresh        = float(thresholds.get("fx_pct", 1.0))
    tnx_bps          = float(thresholds.get("tnx_bps", 10))
    rows: list[PriceRow] = []
    for ticker in all_tickers:
        if ticker in _FINNHUB_UNSUPPORTED:
            logger.warning(f"Finnhub fallback: skipping unsupported ticker {ticker}")
            continue
        try:
            r = httpx.get(
                f"{FINNHUB_BASE}/quote",
                params={"symbol": ticker, "token": FINNHUB_API_KEY},
                timeout=8,
            )
            d = r.json()
            price      = float(d.get("c") or 0)
            prev_close = float(d.get("pc") or 0)
            if price == 0 or prev_close == 0:
                logger.warning(f"Finnhub fallback: no data for {ticker}")
                continue
            change_pct = float(d.get("dp") or 0)
            # week_change_pct not available from /quote; set 0 (non-critical)
            week_change_pct = 0.0
            if ticker == "^TNX":
                is_anomaly = abs(price - prev_close) * 100 >= tnx_bps
                unit = "%"
            elif ticker in fx:
                is_anomaly = abs(change_pct) >= fx_thresh
                unit = ""
            elif ticker in commodities:
                is_anomaly = abs(change_pct) >= commodity_thresh
                unit = "$"
            else:
                is_anomaly = abs(change_pct) >= stock_thresh
                unit = "$"
            rows.append(PriceRow(
                ticker=ticker,
                display=DISPLAY_NAMES.get(ticker, ticker),
                price=price,
                prev_close=prev_close,
                change_pct=change_pct,
                week_change_pct=week_change_pct,
                is_anomaly=is_anomaly,
                unit=unit,
                slot="daily",  # Finnhub always regular-session
            ))
            time.sleep(0.05)  # ~20 req/s, well within 60 calls/min
        except Exception as e:
            logger.warning(f"Finnhub fallback {ticker}: {e}")
    logger.info(f"Finnhub fallback: {len(rows)}/{len(all_tickers)} tickers fetched "
                f"({len(_FINNHUB_UNSUPPORTED & set(all_tickers))} skipped as unsupported)")
    return rows


def fetch_prices(
    stocks: list[str],
    commodities: list[str],
    fx: list[str],
    thresholds: dict,
    slot: str = "daily",
    report_date=None,
    *,
    _sleep=time.sleep,
    _yf=None,
) -> list[PriceRow]:
    """Fetch prices for all watchlist tickers.

    slot="am"    → pre-market price vs prev close (requires intraday prepost data)
    slot="pm"    → today's close vs prev close + after-hours change
    slot="daily" → legacy: latest daily close vs previous close

    _sleep / _yf are test hooks (issue #67). Production callers omit them.
    """
    if _yf is None:
        try:
            import yfinance as yf
        except ImportError:
            logger.error("yfinance not installed in host venv")
            return []
    else:
        yf = _yf

    all_tickers = stocks + commodities + fx
    if not all_tickers:
        return []

    stock_thresh     = float(thresholds.get("stock_pct", 3.0))
    commodity_thresh = float(thresholds.get("commodity_pct", 2.0))
    fx_thresh        = float(thresholds.get("fx_pct", 1.0))
    tnx_bps          = float(thresholds.get("tnx_bps", 10))

    # ── Daily download: prev_close and 5-day history ──────────────────────────
    # Issue #67: quiet yfinance ERROR (false "possibly delisted"); retry once
    # for missing tickers and merge recovered columns onto the first frame.
    # Never replace a usable first bulk with a worse retry — Finnhub cannot
    # fill FX/commodities in _FINNHUB_UNSUPPORTED.
    _dl_kwargs = dict(
        period="8d",
        interval="1d",
        progress=False,
        auto_adjust=True,
        group_by="column",
    )
    try:
        data_daily = _yf_download(yf, all_tickers, **_dl_kwargs)
    except Exception as e:
        logger.error(f"yfinance daily download failed: {e} — trying Finnhub fallback")
        return _fetch_prices_finnhub(all_tickers, stocks, fx, commodities, thresholds, slot=slot)

    first_daily = data_daily
    missing = _missing_close_tickers(first_daily, all_tickers)
    if missing:
        logger.warning(
            f"yfinance daily bulk incomplete: {len(missing)}/{len(all_tickers)} "
            f"tickers missing {missing} — retrying once"
        )
        retry_daily = None
        try:
            _sleep(1.0)
            retry_daily = _yf_download(yf, all_tickers, **_dl_kwargs)
        except Exception as e:
            logger.warning(f"yfinance daily retry failed: {e} — keeping first response")
        data_daily = _merge_daily_ohlc(first_daily, retry_daily, all_tickers)
        missing = _missing_close_tickers(data_daily, all_tickers)
        if missing:
            logger.warning(
                f"yfinance daily bulk still incomplete after retry: "
                f"{len(missing)}/{len(all_tickers)} tickers missing {missing} "
                f"— per-ticker fallback will run"
            )

    # ── Intraday download for AM/PM enrichment (fail-open) ───────────────────
    data_intraday = None
    if slot in ("am", "pm"):
        data_intraday = _fetch_intraday_data(all_tickers, yf)

    from datetime import datetime as _dt
    # Use report_date if provided (handles FORCE_DATE re-runs and post-midnight scenarios).
    # Fallback to the most recent date present in the intraday data, then current ET date.
    if report_date is not None:
        today_et_date = report_date if hasattr(report_date, "year") else _dt.strptime(str(report_date), "%Y-%m-%d").date()
    else:
        today_et_date = _dt.now(_ET).date()

    rows: list[PriceRow] = []
    for ticker in all_tickers:
        try:
            # ── Daily closes ─────────────────────────────────────────────────
            if len(all_tickers) == 1:
                closes_daily = data_daily["Close"]
                opens_daily  = data_daily["Open"]
            else:
                closes_daily = data_daily["Close"][ticker]
                opens_daily  = data_daily["Open"][ticker]
            closes_daily = closes_daily.dropna()
            opens_daily  = opens_daily.dropna()

            if len(closes_daily) < 2:
                logger.warning(f"{ticker}: insufficient daily data ({len(closes_daily)} rows) — trying per-ticker Finnhub fallback")
                row = _finnhub_single_ticker(ticker, stocks, fx, commodities, thresholds, slot=slot)
                if row:
                    rows.append(row)
                continue

            week_start = float(closes_daily.iloc[-6]) if len(closes_daily) >= 6 else float(closes_daily.iloc[0])

            # ── Determine threshold / unit ────────────────────────────────────
            if ticker == "^TNX":
                unit = "%"
            elif ticker in fx:
                unit = ""
            elif ticker in commodities:
                unit = "$"
            else:
                unit = "$"

            # ── Slot-specific price logic ─────────────────────────────────────
            afterhours_price   = 0.0
            afterhours_pct     = 0.0
            session_change_pct = 0.0

            if slot == "am":
                # Use only sessions strictly before report_date so this is correct both at
                # 8:30 AM (market not yet open) and when testing/FORCE_DATE after market close.
                import pandas as _pd
                _report_ts  = _pd.Timestamp(today_et_date)
                _pre_mask   = closes_daily.index.normalize() < _report_ts
                _closes_pre = closes_daily[_pre_mask]
                _opens_pre  = opens_daily[_pre_mask]
                if len(_closes_pre) < 2:
                    logger.warning(f"{ticker} AM: insufficient pre-report daily data — trying per-ticker Finnhub fallback")
                    row = _finnhub_single_ticker(ticker, stocks, fx, commodities, thresholds, slot=slot)
                    if row:
                        rows.append(row)
                    continue
                # prev_close  = last completed session's official close
                # session_change_pct = full day return (close vs prior close) — matches Yahoo Finance
                # change_pct  = premarket price vs prev_close
                prev_close      = float(_closes_pre.iloc[-1])
                prev_prev_close = float(_closes_pre.iloc[-2])
                if prev_close == 0 or prev_prev_close == 0:
                    continue
                week_start      = float(_closes_pre.iloc[-6]) if len(_closes_pre) >= 6 else float(_closes_pre.iloc[0])
                week_change_pct = (prev_close - week_start) / week_start * 100
                # session_change_pct = close vs prior close (Yahoo Finance "day change" for yesterday)
                session_change_pct = (prev_close - prev_prev_close) / prev_prev_close * 100
                # premarket price
                closes_1m = _closes_1m(data_intraday, ticker, all_tickers) if data_intraday is not None else None
                pm_price = _get_premarket_price(closes_1m, today_et_date)
                if pm_price is not None:
                    price      = pm_price
                    change_pct = (price - prev_close) / prev_close * 100
                    logger.debug(f"{ticker} AM: premarket={price:.4f} prev_close={prev_close:.4f} "
                                 f"chg={change_pct:+.2f}% yest_full={session_change_pct:+.2f}%")
                else:
                    price      = prev_close
                    change_pct = 0.0
                    logger.debug(f"{ticker} AM: no premarket data")

            elif slot == "pm":
                # price     = DAILY official close (matches Yahoo Finance — NOT intraday 1m last bar)
                # prev_close = daily official previous close
                # change_pct = official close vs official prev close — matches Yahoo Finance "day change"
                # session_change_pct = close vs today's open (intraday session performance)
                # after-hours: from intraday 1m only
                #
                # Use date-aware indexing for robustness (handles FORCE_DATE / post-close testing)
                import pandas as _pd
                _report_ts   = _pd.Timestamp(today_et_date)
                _today_mask  = closes_daily.index.normalize() == _report_ts
                _prev_mask   = closes_daily.index.normalize() < _report_ts
                # today's official close from daily data
                _closes_today = closes_daily[_today_mask]
                _closes_prev  = closes_daily[_prev_mask]
                _opens_today  = opens_daily[_today_mask]
                if _closes_today.empty:
                    # market may not have closed yet or weekend — fall back to latest available
                    _closes_today = closes_daily.iloc[[-1]]
                    _opens_today  = opens_daily.iloc[[-1]]
                if _closes_prev.empty or _closes_today.empty:
                    logger.warning(f"{ticker} PM: insufficient daily data for report_date={today_et_date}")
                    continue
                price      = float(_closes_today.iloc[-1])   # official daily close
                prev_close = float(_closes_prev.iloc[-1])    # official previous close
                if prev_close == 0 or price == 0:
                    continue
                week_start      = float(closes_daily.iloc[-6]) if len(closes_daily) >= 6 else float(closes_daily.iloc[0])
                week_change_pct = (price - week_start) / week_start * 100
                change_pct      = (price - prev_close) / prev_close * 100
                # session: close vs open using daily data
                if not _opens_today.empty:
                    today_open = float(_opens_today.iloc[-1])
                    if today_open:
                        session_change_pct = (price - today_open) / today_open * 100
                # after-hours from intraday only (prev_close baseline = official daily close)
                closes_1m = _closes_1m(data_intraday, ticker, all_tickers) if data_intraday is not None else None
                _, ah_price = _get_pm_prices(closes_1m, today_et_date)
                if ah_price is not None and ah_price > 0 and price > 0:
                    afterhours_price = ah_price
                    afterhours_pct   = (ah_price - price) / price * 100
                    logger.debug(f"{ticker} PM: close={price:.4f} prev={prev_close:.4f} "
                                 f"session={session_change_pct:+.2f}% day={change_pct:+.2f}% "
                                 f"ah={ah_price:.4f} ah%={afterhours_pct:+.2f}%")

            else:  # daily
                prev_close = float(closes_daily.iloc[-2])
                if prev_close == 0:
                    continue
                week_change_pct = (float(closes_daily.iloc[-1]) - week_start) / week_start * 100
                price      = float(closes_daily.iloc[-1])
                change_pct = (price - prev_close) / prev_close * 100

            # ── Anomaly detection ─────────────────────────────────────────────
            if ticker == "^TNX":
                bps_change = abs(price - prev_close) * 100
                is_anomaly = bps_change >= tnx_bps
                if slot == "pm" and afterhours_price:
                    bps_ah = abs(afterhours_price - price) * 100
                    is_anomaly = is_anomaly or bps_ah >= tnx_bps
            elif ticker in fx:
                is_anomaly = abs(change_pct) >= fx_thresh
                if slot == "pm" and afterhours_pct:
                    is_anomaly = is_anomaly or abs(afterhours_pct) >= fx_thresh
            elif ticker in commodities:
                is_anomaly = abs(change_pct) >= commodity_thresh
                if slot == "pm" and afterhours_pct:
                    is_anomaly = is_anomaly or abs(afterhours_pct) >= commodity_thresh
            else:
                is_anomaly = abs(change_pct) >= stock_thresh
                if slot == "pm" and afterhours_pct:
                    is_anomaly = is_anomaly or abs(afterhours_pct) >= stock_thresh

            rows.append(PriceRow(
                ticker=ticker,
                display=DISPLAY_NAMES.get(ticker, ticker),
                price=price,
                prev_close=prev_close,
                change_pct=change_pct,
                week_change_pct=week_change_pct,
                is_anomaly=is_anomaly,
                unit=unit,
                afterhours_price=afterhours_price,
                afterhours_pct=afterhours_pct,
                session_change_pct=session_change_pct,
                slot=slot,
            ))
        except Exception as e:
            logger.warning(f"{ticker}: {e}")

    logger.info(f"Prices: {len(rows)}/{len(all_tickers)} tickers fetched (slot={slot})")
    if not rows:
        logger.warning("yfinance returned no rows — trying Finnhub fallback")
        return _fetch_prices_finnhub(all_tickers, stocks, fx, commodities, thresholds, slot=slot)
    return rows


def format_price_table(rows: list[PriceRow], slot: str = "daily") -> str:
    if not rows:
        return "_价格数据获取失败_"

    def _price_str(r: PriceRow, p: float) -> str:
        if r.ticker == "^TNX":
            return f"{p:.3f}%"
        elif r.unit == "$":
            return f"${p:,.2f}"
        elif r.unit == "%":
            return f"{p:.4f}%"
        else:
            return f"{p:.4f}"

    def _change_str(r: PriceRow, pct: float) -> str:
        """Format a percentage change, using bps for ^TNX based on price delta."""
        return f"{pct:+.2f}%"

    def _premarket_str(r: PriceRow) -> str:
        """AM: premarket change vs prev_close."""
        if r.ticker == "^TNX":
            bps = (r.price - r.prev_close) * 100
            return f"{bps:+.1f}bps"
        return f"{r.change_pct:+.2f}%"

    def _ah_str(r: PriceRow) -> str:
        """PM: after-hours change vs today's close."""
        if r.afterhours_price <= 0:
            return "—"
        if r.ticker == "^TNX":
            bps = (r.afterhours_price - r.price) * 100
            return f"{bps:+.1f}bps"
        return f"{r.afterhours_pct:+.2f}%"

    def _close_vs_prev_str(r: PriceRow) -> str:
        """PM: today's close vs yesterday's close."""
        if r.ticker == "^TNX":
            bps = (r.price - r.prev_close) * 100
            return f"{bps:+.1f}bps"
        return f"{r.change_pct:+.2f}%"

    if slot == "am":
        # col1 = 昨日全日↑↓（昨收 vs 前日收）= session_change_pct  ← Yahoo Finance "day change"
        # col2 = 盘前涨跌（盘前价 vs 昨收）= change_pct
        lines = [
            "| 标的 | 昨收价 | 昨日全日↑↓（vs前收）| 盘前↑↓（vs昨收）| 5日↑↓ | 异动 |",
            "|---|---|---|---|---|---|",
        ]
        for r in rows:
            flag = "[!]" if r.is_anomaly else "—"
            sess = _change_str(r, r.session_change_pct) if r.session_change_pct != 0 else "—"
            lines.append(
                f"| {r.display} | {_price_str(r, r.prev_close)} | {sess} "
                f"| {_premarket_str(r)} | {r.week_change_pct:+.2f}% | {flag} |"
            )
    elif slot == "pm":
        # col1 = vs前收（今收 vs 昨收）= change_pct  ← Yahoo Finance "day change" — primary
        # col2 = vs今开（今收 vs 今开）= session_change_pct — supplemental intraday
        # col3 = 盘后↑↓（盘后价 vs 今收）= afterhours_pct
        lines = [
            "| 标的 | 收盘价 | 日内↑↓（vs前收）| vs今开 | 盘后↑↓ | 5日↑↓ | 异动 |",
            "|---|---|---|---|---|---|---|",
        ]
        for r in rows:
            flag = "[!]" if r.is_anomaly else "—"
            sess = _change_str(r, r.session_change_pct) if r.session_change_pct != 0 else "—"
            lines.append(
                f"| {r.display} | {_price_str(r, r.price)} | {_close_vs_prev_str(r)} "
                f"| {sess} | {_ah_str(r)} | {r.week_change_pct:+.2f}% | {flag} |"
            )
    else:  # daily / fallback
        lines = [
            "| 标的 | 现价 | 日涨跌 | 5日涨跌 | 异动 |",
            "|---|---|---|---|---|",
        ]
        for r in rows:
            flag = "[!]" if r.is_anomaly else "—"
            lines.append(
                f"| {r.display} | {_price_str(r, r.price)} | {_close_vs_prev_str(r)} "
                f"| {r.week_change_pct:+.2f}% | {flag} |"
            )

    return "\n".join(lines)


@contextmanager
def _quiet_yfinance_logs():
    """Issue #63 / #67: demote yfinance logger during any yf.download / history.

    yfinance logs false 'possibly delisted; no price data found (period=...)' at
    ERROR. Homepage healthcheck finance logs_scan treats any ERROR line as a
    hit (threshold=1), so one transient miss becomes a WARN. Suppress while we
    pull; restore previous level on exit. Callers still emit our own WARNING.
    """
    yf_log = logging.getLogger("yfinance")
    previous = yf_log.level
    yf_log.setLevel(logging.CRITICAL)
    try:
        yield
    finally:
        yf_log.setLevel(previous)


def _compute_52week_from_closes(closes) -> dict | None:
    """Map a daily Close series → {range_percentile, pct_from_high}, or None.

    Requires ≥20 bars and a non-zero high/low range. Pure; no I/O.
    """
    try:
        closes = closes.dropna()
        if len(closes) < 20:
            return None
        current = float(closes.iloc[-1])
        hi = float(closes.max())
        lo = float(closes.min())
        if hi == lo:
            return None
        return {
            "range_percentile": round((current - lo) / (hi - lo) * 100, 1),
            "pct_from_high": round((current - hi) / hi * 100, 1),
        }
    except Exception:
        return None


def _closes_from_bulk(data, ticker: str, n_tickers: int):
    """Extract Close series for one ticker from yf.download multi/single frame."""
    if data is None:
        return None
    try:
        empty = getattr(data, "empty", True)
        if empty:
            return None
        if n_tickers == 1:
            closes = data["Close"]
            # Single-ticker download sometimes still has a ticker column level
            if hasattr(closes, "columns"):
                closes = closes.iloc[:, 0] if closes.shape[1] else closes
        else:
            closes = data["Close"][ticker]
        return closes
    except Exception:
        return None


def _history_closes(yf_mod, ticker: str, *, retries: int = 1, sleep_fn=time.sleep):
    """Issue #63 A: per-ticker Ticker.history with one delayed retry on empty.

    sleep_fn is injectable for unit tests (default time.sleep).
    """
    attempts = retries + 1
    for attempt in range(attempts):
        try:
            with _quiet_yfinance_logs():
                hist = yf_mod.Ticker(ticker).history(period="1y", auto_adjust=True)
            if hist is not None and not getattr(hist, "empty", True) and "Close" in hist:
                closes = hist["Close"]
                if len(closes.dropna()) >= 20:
                    return closes
        except Exception as e:
            logger.debug(f"{ticker}: 52-week history attempt {attempt + 1} failed: {e}")
        if attempt < retries:
            sleep_fn(1.0)
    return None


def fetch_52week_stats(
    tickers: list[str],
    *,
    _yf=None,
    _sleep=time.sleep,
) -> dict[str, dict]:
    """52-week range percentile + drawdown from high (issue #33 / #63).

    Pure computation on yfinance daily history — the "股价相对位置" signal from
    Investment Operating Manual 7.4 (Expectation Gap 市场共识类信号). No LLM,
    no search cost; meant to be injected as a computed fact so Pass 2 doesn't
    have to eyeball "high or low" from prose. Fail-open: a failure for one or
    all tickers returns an empty/partial dict, never raises.

    Resilience (issue #63):
      A — bulk miss / short series → Ticker.history per ticker, 1 retry after 1s
      B — quiet yfinance ERROR logs during pull (healthcheck noise)

    _yf / _sleep are test hooks; production callers omit them.
    """
    if not tickers:
        return {}
    if _yf is None:
        try:
            import yfinance as yf
        except ImportError:
            return {}
        yf_mod = yf
    else:
        yf_mod = _yf

    data = None
    try:
        with _quiet_yfinance_logs():
            data = yf_mod.download(
                tickers,
                period="1y",
                interval="1d",
                progress=False,
                auto_adjust=True,
                group_by="column",
            )
    except Exception as e:
        logger.warning(f"52-week stats bulk download failed: {e}")
        data = None

    out: dict[str, dict] = {}
    n = len(tickers)
    for ticker in tickers:
        closes = _closes_from_bulk(data, ticker, n)
        stats = _compute_52week_from_closes(closes) if closes is not None else None
        if stats is None:
            logger.info(f"{ticker}: 52-week bulk miss/short — per-ticker history retry")
            closes = _history_closes(yf_mod, ticker, retries=1, sleep_fn=_sleep)
            stats = _compute_52week_from_closes(closes) if closes is not None else None
        if stats is None:
            logger.warning(f"{ticker}: 52-week stats unavailable after retry")
            continue
        out[ticker] = stats
    return out


def get_anomalies(rows: list[PriceRow]) -> list[PriceRow]:
    return [r for r in rows if r.is_anomaly]

"""Shared report/replay publication windows for multi-session price moves."""
import logging
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


def _session_anchor(today_et: str, sessions_back: int):
    """Date of the close ``sessions_back`` NYSE sessions before today_et."""
    today = datetime.strptime(today_et, "%Y-%m-%d").date()
    try:
        import exchange_calendars as xcals
        nyse = xcals.get_calendar("XNYS")
        start = today - timedelta(days=sessions_back * 3 + 14)
        sessions = nyse.sessions_in_range(str(start), str(today - timedelta(days=1)))
        if len(sessions) >= sessions_back:
            return sessions[-sessions_back].date()
    except Exception as exc:
        logger.warning("NYSE session walk failed (%s); using a longer calendar fallback", exc)
    return today - timedelta(days=sessions_back + 4)


def _unexplained_publication_window(today_et: str, window_days: int, slot: str) -> tuple[str, str, int]:
    """Published-date window covering the move's first session plus 2 days."""
    sessions_back = window_days + (1 if slot == "am" else 0)
    anchor = _session_anchor(today_et, sessions_back)
    start = anchor - timedelta(days=2)
    end = datetime.strptime(today_et, "%Y-%m-%d").date()
    days = max((end - start).days, window_days + 2)
    return start.isoformat(), end.isoformat(), days

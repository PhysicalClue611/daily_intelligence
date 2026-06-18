"""
memory_context_finance.py — Personal knowledge base context for run_finance.py

Pulls two signal types before LLM generation:
  1. MemPalace — Gary's investment framework, macro views, ticker history (semantic search)
  2. Obsidian  — Daily Intelligence vault notes (full-text search)

Returns compact text block for prompt injection, or "" if bridge unreachable.
All calls are fail-open.
"""
import json
import logging
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

BRIDGE_URL = "http://localhost:8765"

# Per-section char budgets (sum < max_chars ceiling)
_MP_BUDGET   = 1200
_OBS_BUDGET  =  800


def _post(endpoint: str, payload: dict, bridge_url: str = BRIDGE_URL, timeout: int = 6) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        f"{bridge_url}{endpoint}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        logger.debug("bridge %s error: %s", endpoint, e)
        return {}


def _mempalace_search(
    query: str,
    wing: str = "paperview",
    room: str = "finance",
    n_results: int = 3,
    max_distance: float = 0.85,
    bridge_url: str = BRIDGE_URL,
) -> list[dict]:
    result = _post("/mempalace/search", {
        "query": query,
        "wing": wing,
        "room": room,
        "n_results": n_results,
        "max_distance": max_distance,
    }, bridge_url=bridge_url)
    return result.get("results", [])


def _obsidian_search(
    query: str,
    path: str = "Hermes/Daily Intelligence",
    max_results: int = 3,
    bridge_url: str = BRIDGE_URL,
) -> list[dict]:
    result = _post("/obsidian/search", {
        "query": query,
        "path": path,
        "max_results": max_results,
    }, bridge_url=bridge_url)
    return result.get("hits", [])


def _bridge_alive(bridge_url: str = BRIDGE_URL) -> bool:
    try:
        with urllib.request.urlopen(f"{bridge_url}/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def get_finance_context(
    anomaly_tickers: list[str] | None = None,
    geo_topics: list[str] | None = None,
    bridge_url: str = BRIDGE_URL,
    max_chars: int = 6000,
) -> str:
    """
    Build personal-knowledge-base context block for finance LLM prompts.

    Section budgets: MemPalace=1200, Obsidian=800 chars (independent truncation).
    Total ceiling: max_chars (default 6000, ~1500 tokens).
    """
    if not _bridge_alive(bridge_url):
        logger.info("KB bridge unreachable, skipping memory context")
        return ""

    anomaly_set = set(anomaly_tickers or [])

    sections: list[str] = []

    # --- Section 1: MemPalace semantic search ---
    macro_hits = _mempalace_search(
        "investment framework macro view portfolio strategy holdings",
        bridge_url=bridge_url,
        n_results=3,
    )
    mp_snippets: list[str] = []
    if macro_hits:
        for h in macro_hits:
            src = h.get("source_file", "?")
            sim = h.get("similarity", 0)
            text = h.get("text", "")[:250].replace("\n", " ")
            mp_snippets.append(f"[{src} sim={sim:.2f}] {text}")

    # also search per anomaly ticker in finance + hermes rooms
    for ticker in list(anomaly_set)[:4]:
        hits = _mempalace_search(
            f"{ticker} investment thesis position judgment",
            room="finance", bridge_url=bridge_url, n_results=2,
        ) + _mempalace_search(
            f"{ticker} price move news brief",
            room="hermes", bridge_url=bridge_url, n_results=1,
        )
        for h in hits:
            src = h.get("source_file", "?")
            text = h.get("text", "")[:200].replace("\n", " ")
            mp_snippets.append(f"[{ticker}/{src}] {text}")

    if mp_snippets:
        mp_body = "\n".join(mp_snippets)
        if len(mp_body) > _MP_BUDGET:
            mp_body = mp_body[:_MP_BUDGET] + "\n[...MP truncated]"
        sections.append("【投资框架/宏观观点】\n" + mp_body)

    # --- Section 2: Obsidian notes ---
    if geo_topics:
        topic_query = " ".join(list(geo_topics)[:3])
        geo_hits = _obsidian_search(topic_query, bridge_url=bridge_url, max_results=2)
        if geo_hits:
            obs_snippets = []
            for h in geo_hits:
                path = h.get("path", "?")
                snippet = h.get("snippet", "")[:200]
                obs_snippets.append(f"[{path}] {snippet}")
            obs_body = "\n".join(obs_snippets)
            if len(obs_body) > _OBS_BUDGET:
                obs_body = obs_body[:_OBS_BUDGET] + "\n[...Obs truncated]"
            sections.append("【相关Obsidian笔记】\n" + obs_body)

    if not sections:
        return ""

    header = "=== 个人知识库上下文（供参考）===\n"
    body = "\n\n".join(sections)
    result = header + body
    if len(result) > max_chars:
        result = result[:max_chars] + "\n[...context truncated]"
    logger.info(f"KB context: {len(result)} chars, {len(sections)} sections")
    return result

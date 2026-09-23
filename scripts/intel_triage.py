"""Issue #87 Pass 0 Gemma triage; schema validation keeps failures explicit."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import httpx

import llm_config

logger = logging.getLogger(__name__)
URL = "https://openrouter.ai/api/v1/chat/completions"
EVENT_TYPES = {"earnings", "guidance", "analyst", "partnership", "product", "regulatory",
               "capital", "legal", "sector", "macro", "other"}
MOVE_STATUSES = {"explained", "partial", "unexplained", "no_move"}
SYSTEM = """You are an intelligence sorter, not an investment analyst. Return one JSON object only.
Group headlines about the same event. Analyst ratings, partnerships, customers and equity offerings are company events.
Only label a price move unexplained when no item can reasonably explain it. A price rise itself is never a cause.
If peers moved strongly in the same direction and a sector headline supports it, classify the event as sector.
Use one of explained, partial, unexplained, no_move for move_status. For a missing cause, write a natural-language
gap_query with the company name and event words or 'Why is {company} stock up/down {date}'. Never construct a ticker-plus-percentage template.
Use only input item IDs. Mark seen_before when the same event was reported in recent ledger headlines.
Schema: {"events":[{"id":"e1","headline":"...","date":"YYYY-MM-DD","type":"earnings|guidance|analyst|partnership|product|regulatory|capital|legal|sector|macro|other","company_specific":true,"item_ids":["i1"],"seen_before":false,"need_fulltext":true}],"move_status":"explained|partial|unexplained|no_move","primary_event_ids":["e1"],"gap_query":"..."}"""
MACRO_SYSTEM = """You are an intelligence sorter. Return only JSON: {"events":[{"headline":"...","sources":["publisher domain"],"affected_assets":["ticker or asset"],"date":"YYYY-MM-DD"}]}. Keep only macro or geopolitical developments with a plausible transmission to the supplied holdings; discard the rest. Use only input items."""
_USAGE_LOCK = threading.Lock()


def _record_usage(row: dict) -> None:
    path = Path(os.environ.get("PASS0_USAGE_LOG", str(Path(__file__).resolve().parent.parent / "finance_pass0_usage.jsonl")))
    with _USAGE_LOCK:
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def call_llm(prompt: str, stage: str = "intel_triage", system: str = SYSTEM) -> dict:
    cfg = llm_config.stage(stage)
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise ValueError("OPENROUTER_API_KEY missing")
    payload = {"model": cfg["model"], "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
               "max_tokens": cfg["max_tokens"], "temperature": cfg["temperature"], "usage": {"include": True}}
    if cfg.get("providers"):
        payload["provider"] = cfg["providers"]
    last = None
    for attempt in range(2):
        try:
            response = httpx.post(URL, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                               "HTTP-Referer": "https://github.com/PhysicalClue611/daily_intelligence",
                                               "X-OpenRouter-Title": "DailyIntel"}, json=payload, timeout=35)
            response.raise_for_status()
            body = response.json()
            choice = body["choices"][0]
            usage = body.get("usage") or {}
            cost = usage.get("cost")
            try:
                _record_usage({"stage": stage, "id": body.get("id"), "model": cfg["model"],
                               "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                               "cost_usd": cost, "finish_reason": choice.get("finish_reason")})
            except OSError as exc:
                # A local audit-file error must not cause a second paid request.
                logger.warning("Pass0 usage audit write failed: %s", type(exc).__name__)
            logger.info("Pass0 %s model=%s prompt=%s completion=%s finish_reason=%s provider=%s cost=%s",
                        stage, cfg["model"], usage.get("prompt_tokens"), usage.get("completion_tokens"),
                        choice.get("finish_reason"), body.get("provider"), cost)
            content = choice["message"].get("content") or ""
            if choice.get("finish_reason") == "length" or not content.strip():
                raise ValueError("empty or truncated triage")
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
            parsed = json.loads(content)
            if not isinstance(parsed, dict):
                raise ValueError("triage returned non-object")
            parsed["_usage"] = {"prompt_tokens": usage.get("prompt_tokens"),
                                "completion_tokens": usage.get("completion_tokens"), "cost_usd": cost,
                                "model": cfg["model"], "provider": body.get("provider")}
            return parsed
        except (httpx.TransportError, httpx.HTTPStatusError, ValueError, KeyError, IndexError, json.JSONDecodeError) as exc:
            last = exc
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in (429, 500, 502, 503, 504):
                break
            if attempt == 0:
                time.sleep(1)
    raise last


def _valid_entity(result: dict, item_ids: set[str]) -> bool:
    if not isinstance(result, dict) or result.get("move_status") not in MOVE_STATUSES:
        return False
    events = result.get("events")
    if not isinstance(events, list) or not isinstance(result.get("primary_event_ids"), list) or not isinstance(result.get("gap_query"), str):
        return False
    event_ids = set()
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("id"), str) or event["id"] in event_ids:
            return False
        event_ids.add(event["id"])
        if (not isinstance(event.get("headline"), str) or not isinstance(event.get("date"), str)
                or event.get("type") not in EVENT_TYPES or not isinstance(event.get("company_specific"), bool)
                or not isinstance(event.get("seen_before"), bool) or not isinstance(event.get("need_fulltext"), bool)
                or not isinstance(event.get("item_ids"), list) or not all(i in item_ids for i in event["item_ids"])):
            return False
    return all(i in event_ids for i in result["primary_event_ids"])


def triage_entity(ledger: dict, seen_titles: list[str]) -> dict:
    if not ledger["items"] and not ledger.get("move", {}).get("flags"):
        return {**ledger, "move_status": "no_move", "triage": {"skipped": True}}
    prompt = json.dumps({"ticker": ledger["ticker"], "name": ledger["name"], "aliases": ledger.get("aliases", []),
                         "move": ledger.get("move", {}), "items": ledger["items"], "recent_reported_event_titles": seen_titles}, ensure_ascii=False)
    result = None
    try:
        result = call_llm(prompt)
        # Gemma sometimes uses JSON null for an inapplicable query even when
        # every substantive event/reference field is valid. Canonicalize to
        # the schema's empty string; a genuine missing cause still needs text.
        if result.get("gap_query") is None and result.get("move_status") in ("explained", "no_move"):
            result["gap_query"] = ""
        if not _valid_entity(result, {row["id"] for row in ledger["items"]}):
            raise ValueError("invalid triage schema or item reference")
        return {**ledger, "events": result["events"], "move_status": result["move_status"],
                "primary_event_ids": result["primary_event_ids"], "gap_query": result["gap_query"],
                "triage": result.get("_usage", {})}
    except Exception as exc:
        logger.warning("Pass0 triage %s failed: %s", ledger["ticker"], type(exc).__name__)
        audit = {"error": type(exc).__name__}
        if isinstance(result, dict):
            audit["invalid_result"] = result
        return {**ledger, "move_status": "triage_failed", "triage": audit}


def triage_macro(macro: dict, holdings: list[str]) -> dict:
    if not macro.get("items"):
        return {**macro, "triage": {"skipped": True}}
    prompt = json.dumps({"holdings": holdings, "items": macro["items"]}, ensure_ascii=False)
    try:
        result = call_llm(prompt, stage="macro_triage", system=MACRO_SYSTEM)
        events = result.get("events")
        if not isinstance(events, list) or any(not isinstance(e, dict) or not all(k in e for k in ("headline", "sources", "affected_assets", "date")) for e in events):
            raise ValueError("invalid macro triage schema")
        return {**macro, "events": events, "triage": result.get("_usage", {})}
    except Exception as exc:
        logger.warning("Pass0 macro triage failed: %s", type(exc).__name__)
        return {**macro, "events": [], "triage": {"error": type(exc).__name__}}


def triage_all(entities: list[dict], macro: dict, previous: dict[str, list[str]]) -> tuple[list[dict], dict]:
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(triage_entity, entity, previous.get(entity["ticker"], [])) for entity in entities]
        macro_future = pool.submit(triage_macro, macro, [e["ticker"] for e in entities if e["held"]])
        return [future.result() for future in futures], macro_future.result()

"""Offline #113 replay: archived inputs, no Tavily or report run."""
import copy
import json
import math
import sys
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from intel_deepen import deepen_intel_snapshot, clean_body, body_acceptable
from intel_render import render_intel_snapshot_context
from recent_coverage import recent_fresh_counts, recent_extracted_urls
from run_finance import load_watchlist, USER_PROMPT_TEMPLATE_P2

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "archives/202609/2026-09-25-pm-intel-snapshot.json").exists())
ARCHIVES = ROOT / "archives"
DATES = [("2026-09-24", "am"), ("2026-09-24", "pm"),
         ("2026-09-25", "am"), ("2026-09-25", "pm")]


def main():
    geo_keywords = load_watchlist()["geo_keywords"]
    print("date slot | archived_cr archived_urls | same_urls_packed_cr | grouped_cr grouped_urls skipped rejected")
    for day, slot in DATES:
        original = json.loads((ARCHIVES / day[:7].replace("-", "") /
                               f"{day}-{slot}-intel-snapshot.json").read_text())
        snap = copy.deepcopy(original)
        bodies = {chunk["url"]: chunk["text"] for entity in snap["entities"]
                  for chunk in entity.get("fulltext", [])}
        bodies.update({chunk["url"]: chunk["text"] for chunk in
                       (snap.get("macro_digest") or {}).get("fulltext", [])})
        redirects = {}
        for entity in snap["entities"]:
            by_id = {chunk.get("item_id"): chunk["url"] for chunk in entity.get("fulltext", [])}
            redirects.update({row["url"]: by_id[row["id"]] for row in entity.get("items", [])
                              if row.get("url_kind") == "finnhub_redirect" and row.get("id") in by_id})
        for entity in snap["entities"]:
            entity["fulltext"] = []
        snap.setdefault("macro_digest", {})["fulltext"] = []
        used = 0
        url_calls = []
        def search(*_):
            nonlocal used
            used += 1
            return []
        def extract(urls, query, chunks):
            nonlocal used
            assert chunks == 3 and query
            used += math.ceil(len(urls) / 5)
            url_calls.extend(urls)
            return [{"url": url, "raw_content": bodies[url]} for url in urls if url in bodies]
        with patch("intel_deepen.resolve_article_url", side_effect=lambda url: redirects.get(url)):
            deepen_intel_snapshot(snap, search=search, extract=extract,
                                  remaining=lambda: 25-used, slot=slot,
                                  geo_keywords=geo_keywords,
                                  history_counts=recent_fresh_counts(ARCHIVES, day, slot),
                                  skip_urls=recent_extracted_urls(ARCHIVES, day, slot),
                                  run_credit_cap=25)
        old_urls = len(url_calls) + snap["extract_dedup_skipped"]
        old_credits = snap["search_count"] + math.ceil(old_urls / 5)
        print(f"{day} {slot} | {original.get('run_credit_used', 0)} {original.get('extract_url_count', 0)} "
              f"| {old_credits} | {used} {len(url_calls)} "
              f"{snap['extract_dedup_skipped']} {snap['extract_rejected_count']}")
    pm = json.loads((ARCHIVES / "202609/2026-09-25-pm-intel-snapshot.json").read_text())
    rendered = render_intel_snapshot_context(pm, geo_keywords)
    prompt = USER_PROMPT_TEMPLATE_P2.format(date=pm["date"], now_str=pm.get("as_of", ""),
        pm_afterhours_note="", price_data_label="archived prices", price_table="",
        price_missing_note="", intel_snapshot_section=rendered, sonar_macro_section="",
        social_sentiment_section="", liquidity_section="", kb_section="",
        calibration_notes="", recent_coverage_section="", personal_context="",
        verifiable_signals_rule="")
    print("render chars", len(prompt), "estimated tokens", math.ceil(len(prompt)/2.5),
          "SPCX NASA", "NASA" in rendered.split("### SPCX")[1].split("### ")[0])
    rows = [(x["url"], x["text"]) for e in pm["entities"] for x in e.get("fulltext", [])]
    rows += [(x["url"], x["text"]) for x in pm["macro_digest"].get("fulltext", [])]
    archived_tokens = pm.get("pass2", {}).get("prompt_tokens", 0)
    max_extra_chars = sum(max(0, 2000 - len(body)) for _, body in rows)
    max_extra_chars += max(0, pm.get("extract_url_count", 0) - len(rows)) * 2000
    print("archived full prompt tokens", archived_tokens,
          "estimated upper with longer bodies", archived_tokens + math.ceil(max_extra_chars / 2.5))
    for url, body in rows:
        print("PASS" if body_acceptable(clean_body(url, body)) else "REJECT", url)


if __name__ == "__main__":
    main()

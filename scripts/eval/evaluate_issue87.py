#!/usr/bin/env python3
"""Run the 24 historical Pass 0 cases and enforce the collection recall gate.

Only free Finnhub, Google News, Guardian, and historical price calls are used. No LLM, Tavily, report, email,
Telegram or Obsidian writes. Results go to a local JSON file for audit.
"""
import argparse
import json
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from intel_pass0 import replay

CASES = Path(__file__).with_name("attribution_eval_2026-08_09.json")


def score_case(case: dict, intel_snapshot: dict) -> dict:
    entity = next((e for e in intel_snapshot["entities"] if e["ticker"] == case["ticker"]), None)
    regex = re.compile(case["pattern"], re.I)
    items_hit = bool(entity and any(regex.search(i["title"] + " " + i["summary"]) for i in entity["items"]))
    return {"date": case["date"], "slot": case["slot"], "ticker": case["ticker"],
            "driver": case["driver"], "collection_hit": items_hit,
            "coverage": entity["coverage"] if entity else {}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="/private/tmp/issue87_eval_results.json")
    args = parser.parse_args()
    cases = json.loads(CASES.read_text())
    if len(cases) != 24:
        raise SystemExit("expected 24 cases")
    results = []
    for index, case in enumerate(cases, 1):
        print(f"[{index}/24] {case['date']} {case['slot']} {case['ticker']}", flush=True)
        with tempfile.TemporaryDirectory(prefix="di87_eval_") as root:
            try:
                intel_snapshot, _ = replay(case["date"], case["slot"], tickers=[case["ticker"]], archive_root=Path(root))
                result = score_case(case, intel_snapshot)
            except Exception as exc:
                result = {"date": case["date"], "slot": case["slot"], "ticker": case["ticker"],
                          "collection_hit": False, "error": f"{type(exc).__name__}: {exc}"}
        results.append(result)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n")
        print(f"  collection={result['collection_hit']}", flush=True)
    collection = sum(r["collection_hit"] for r in results)
    print(f"R7.2 collection {collection}/24 (need >=21)")
    raise SystemExit(0 if collection >= 21 else 1)


if __name__ == "__main__":
    main()

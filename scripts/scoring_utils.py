"""
Daily Intelligence — text-scoring heuristics shared across modules
=====================================================================
Title-dedup (Jaccard token overlap) and source-confidence-tag helpers.
Extracted from run_finance.py (issue #42 follow-up, per PR #43 review
feedback, 2026-07-17): both run_finance.py's own score_and_filter() /
format_extract_results() AND intel_sources.py's fetch_brave_news() /
report_writers.py's write_extract_archive() need these — a true shared
leaf module (rather than a deferred `from run_finance import ...` inside
the two moved functions) avoids relying on run_finance.py having already
been imported as a *named* module. That assumption silently broke when
run_finance.py is launched directly (`python run_finance.py`, as launchd
does): Python registers the entrypoint as `__main__`, not `run_finance`,
so the old deferred import would trigger a second, distinct execution of
the entire ~1900-line module the first time it ran — wasteful and a latent
state-divergence risk for any future module-level mutable state. This
module has zero dependency on run_finance.py, eliminating the issue.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Cross-source title dedup (issue #14): exact-URL dedup alone rarely catches
# wire-service syndication (same AP/Reuters story picked up by many outlets
# under different URLs, often paraphrased headlines).
#
# Character-level similarity (difflib.SequenceMatcher) was tried first and
# empirically FAILED on real data: real headline pairs describing the same
# ASML earnings-guidance story ("raises 2026 forecast" vs "hikes sales
# forecast") scored 0.33-0.73 — well under any safe threshold — because
# outlets paraphrase verbs and reorder words. Token-overlap (Jaccard on
# significant words) tested far better on the same real pairs and is what's
# implemented below. Scoping comparisons to results that share an anomaly
# ticker/geo keyword (rather than comparing all pairs blindly) lets a lower,
# more forgiving threshold work without merging unrelated stories that
# happen to share generic financial vocabulary. Tuned against the
# 2026-07-15 AM archive where 9 of 10 Tavily Extract slots went to just 2
# real events — see issue #14 discussion for the worked example.
_TITLE_DEDUP_THRESHOLD            = 0.30  # when titles share an anomaly/geo keyword
_TITLE_DEDUP_THRESHOLD_NO_KEYWORD = 0.45  # neither title hit a tracked keyword — stricter bar
_TITLE_DEDUP_STRICT_NO_DATE_THRESHOLD = 0.6  # neither side has a parseable date — require near-exact wording
_TITLE_DEDUP_WINDOW_HOURS = 24
_DEDUP_STOPWORDS = {
    "a", "an", "the", "on", "in", "at", "to", "for", "of", "as", "is", "are",
    "with", "after", "its", "it", "and", "or", "this", "that", "from", "by", "new",
}
# Source-suffix stripper: only strips a SHORT trailing "- Reuters" / "| CNBC.com"
# style tag. Bounded to <=30 chars after the dash so it can't (as an earlier
# version did) greedily eat the substantive back half of a headline — e.g.
# "Iran tension escalates - Hormuz strait closure imminent" must NOT collapse
# to the same tokens as "... - Israel considers preemptive strike" (verified
# both reduced to an identical frozenset before this bound was added).
_TITLE_SUFFIX_RE = re.compile(r"\s*[-|–—]\s*[a-z0-9][a-z0-9 .]{0,29}$")


def _title_tokens_for_dedup(title: str) -> frozenset[str]:
    """Lowercase word tokens minus stopwords/short tokens, source suffix stripped
    (' - Reuters' / ' | CNBC') — used for Jaccard overlap, not character matching."""
    t = title.lower().strip()
    t = _TITLE_SUFFIX_RE.sub("", t)
    words = re.findall(r"[a-z0-9]+", t)
    return frozenset(w for w in words if len(w) > 1 and w not in _DEDUP_STOPWORDS)


def _title_keyword_hits(title: str, keywords: list[str]) -> frozenset[str]:
    """Which tracked anomaly-ticker/geo keywords appear in this title — used to
    scope title-dedup comparisons so unrelated stories sharing generic financial
    words never get compared at the forgiving in-topic threshold.

    Word-boundary matching, not raw substring: a plain `k in t` check let short
    keywords like "us" (split from the geo-topic label "US-Iran") match inside
    unrelated words ("focus", "trust"), which fed false keyword hits into the
    dedup gate (verified: 'Focus shifts to Fed rate decision' and 'Consumer
    trust index falls' both registered kw_hits={'us'})."""
    t = title.lower()
    return frozenset(
        k for k in keywords
        if k and re.search(r"\b" + re.escape(k) + r"\b", t)
    )


def _token_jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    union = a | b
    return len(a & b) / len(union) if union else 0.0
_TIMESTAMP_RE = re.compile(r"\b\d{1,2}:\d{2}\b")
_PHRASE_RE = re.compile(r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}\b")
_STRONG_TOKEN_RE = re.compile(r"\b\d{1,3}(?:\.\d+)?%|\$\d[\d,\.]*|\b\d{4}\b")
_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}


def build_keyword_set(anomaly_tickers: list[str], geo_keywords: dict[str, list[str]]) -> list[str]:
    """Lowercased anomaly-ticker + geo-keyword anchor list, shared by score_and_filter
    (title-dedup/scoring keyword bonus) and _extract_key_phrases (corroboration
    fingerprint, issue #19 follow-up). `geo_keywords` takes the curated topic→keyword
    dict from watchlist.md, not bare topic labels — splitting a label like "US-Iran"
    into ["us","iran"] both misses real synonym anchors and introduces short-token
    false positives ("us" matching inside "focus"). A multi-word curated keyword
    ("Strait of Hormuz") also gets its significant individual words anchored
    separately — real headlines often say just "Hormuz" alone (confirmed missed in
    the 2026-07-15 PM production run)."""
    keywords = [t.strip().lower() for t in anomaly_tickers if t and t.strip()]
    for kws in geo_keywords.values():
        for kw in kws:
            kw = kw.strip().lower()
            if not kw:
                continue
            keywords.append(kw)
            if " " in kw:
                keywords += [w for w in kw.split() if len(w) > 3 and w not in _DEDUP_STOPWORDS]
    return list(set(keywords))


def _extract_key_phrases(text: str, extra_keywords: list[str] | None = None) -> set[str]:
    """Cheap rule-based fact fingerprint: proper-noun phrases + weekday/date/number
    tokens. Used only for rule-based cross-source corroboration (issue #19) —
    no extra API/LLM cost. Expect false negatives on paraphrased claims; a miss
    here means 'no rule-based match found', not proof a claim is uncorroborated.

    `_PHRASE_RE` requires 2+ Title-Case words, so it structurally cannot match
    single-token entities ("Hormuz") or all-caps ones ("US") — confirmed as a
    real false-negative source in the 2026-07-17 PM archive (a CNBC piece was
    tagged single-source despite NYPost/World Oil/MarineLink/Bloomberg covering
    the same Iran/Hormuz story in the same batch). `extra_keywords` (pass
    build_keyword_set()'s output) plugs that gap with word-boundary matches
    against the caller's own tracked ticker/geo vocabulary — same anchoring
    approach score_and_filter already uses for title-dedup."""
    if not text:
        return set()
    phrases = set(_PHRASE_RE.findall(text))
    tokens = set(m.lower() for m in _STRONG_TOKEN_RE.findall(text))
    words = set(w.lower() for w in re.findall(r"[A-Za-z]+", text))
    tokens |= (words & _WEEKDAYS)
    if extra_keywords:
        tokens |= _title_keyword_hits(text, extra_keywords)
    return phrases | tokens


def _detect_low_structure(text: str) -> bool:
    """Heuristic for video-hub / caption-listing pages (issue #19): they pack many
    bare timestamp prefixes ('01:32 ...') with few real sentences, unlike article
    prose. Such pages often surface an isolated caption with no reliable date of
    its own, which an LLM can otherwise mistake for a fresh, dated claim."""
    if not text or len(text) < 40:
        return False
    ts_count = len(_TIMESTAMP_RE.findall(text))
    sentence_count = text.count(". ") + text.count("。")
    return ts_count >= 3 and ts_count > sentence_count


def _lookup_published_date(url: str, candidates: list[dict]) -> tuple[str, float | None]:
    """Resolve a published date for `url` from the pre-extract search result pool —
    Tavily's /extract response carries no date field, only /search results do."""
    for r in candidates:
        if r.get("url") == url:
            pub = r.get("published_date", "")
            if not pub:
                return "", None
            try:
                from dateutil import parser as _dp
                age_h = (datetime.now(ET) - _dp.parse(pub).astimezone(ET)).total_seconds() / 3600
                return pub, age_h
            except Exception:
                return pub, None
    return "", None


def _compute_corroboration(
    url: str, text: str, candidates: list[dict], extra_keywords: list[str] | None = None
) -> int:
    """Rule-based cross-source check (issue #19): count distinct OTHER domains in the
    broader candidate pool that share at least one key phrase with this result.
    Zero cost (no extra API/LLM call), but also a rough heuristic — 0 means 'no
    overlap found by this rule', not a confirmed single-source claim."""
    own_domain = url.split("/")[2] if "//" in url else url
    own_phrases = _extract_key_phrases(text, extra_keywords)
    if not own_phrases:
        return 0
    matched_domains: set[str] = set()
    for r in candidates:
        curl = r.get("url", "")
        cdomain = curl.split("/")[2] if "//" in curl else curl
        if not cdomain or cdomain == own_domain:
            continue
        ctext = (r.get("title", "") + " " + (r.get("content") or ""))
        if own_phrases & _extract_key_phrases(ctext, extra_keywords):
            matched_domains.add(cdomain)
    return len(matched_domains)


def _source_confidence_tags(
    url: str, full_text: str, candidates: list[dict], extra_keywords: list[str] | None = None
) -> str:
    """Build the '[信源类型/发布时间/交叉印证]' tag line for one Extract source.
    Shared by format_extract_results() (LLM-facing) and write_extract_archive()
    (audit trail) so both reflect the same confidence signals. See issue #19.

    `extra_keywords` (build_keyword_set() output) plugs the single-token/all-caps
    entity gap in the corroboration fingerprint — optional/backward-compatible,
    omitting it just falls back to the original phrase-only matching."""
    pub_date, age_h = _lookup_published_date(url, candidates)
    tags = []
    if _detect_low_structure(full_text):
        tags.append("信源类型: 视频/聚合页疑似caption堆叠-无独立时间戳，谨慎对待")
    if not pub_date:
        tags.append("发布时间: 未知")
    elif age_h is not None and age_h > 72:
        tags.append(f"发布时间: {pub_date}（约{age_h / 24:.0f}天前，警惕过时/已被后续事件覆盖）")
    else:
        tags.append(f"发布时间: {pub_date}")
    corroboration = _compute_corroboration(url, full_text, candidates, extra_keywords)
    tags.append(
        f"交叉印证: {corroboration}个独立域名有重叠信息佐证" if corroboration > 0
        else "交叉印证: 未发现其他信源佐证（单一信源）"
    )
    return " | ".join(tags)


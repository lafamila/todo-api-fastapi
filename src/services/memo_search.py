"""Project-scoped memo search with dependency-free BM25F-style ranking.

The database remains the source of the current document set.  This module only
scores rows already selected by the router; it does not know about historical
``memo_versions`` rows or authorization.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, deque
from dataclasses import dataclass
from heapq import heappop, heappush
from itertools import chain
from typing import Iterator


BM25_K1 = 1.5
BM25_B = 0.75
TITLE_WEIGHT = 2.8
MAX_SNIPPET_CHARS = 220
# Dense one-character queries can produce millions of hits in a project. BM25
# still uses the exact term frequency; only the secondary proximity/snippet
# pass is capped to keep request cost bounded.
MAX_PROXIMITY_OCCURRENCES = 512
MAX_QUERY_TERMS = 16
MAX_SNIPPET_ANCHORS = 32

_QUERY_TERM_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_BOUNDARY_RE = re.compile(r"[\n\r.!?。！？]")


@dataclass(frozen=True)
class _Document:
    memo_id: str
    title: str
    content: str
    normalized_title: str
    normalized_content: str
    title_length: int
    content_length: int


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _query_terms(query: str) -> list[str]:
    """Return unique Unicode word terms while preserving query order."""
    normalized = _normalize(query)
    seen: set[str] = set()
    terms: list[str] = []
    for match in _QUERY_TERM_RE.finditer(normalized):
        term = match.group(0)
        if term not in seen:
            seen.add(term)
            terms.append(term)
    return terms


def validate_search_query(query: str) -> list[str]:
    terms = _query_terms(query.strip())
    if len(terms) > MAX_QUERY_TERMS:
        raise ValueError(f"검색어는 단어 {MAX_QUERY_TERMS}개 이하로 입력해 주세요.")
    return terms


def _field_length(value: str) -> int:
    # Substring TF needs a character-based length denominator. A whitespace
    # token count would treat a ten-thousand-character Korean run as length 1.
    return max(1, sum(character.isalnum() for character in value))


def _count_occurrences(text: str, term: str) -> int:
    """Count non-overlapping substring occurrences for Korean partial terms."""
    return text.count(term) if term else 0


def _bm25_tf(term_frequency: int, length: int, average_length: float) -> float:
    if term_frequency <= 0:
        return 0.0
    normalized_length = length / max(average_length, 1.0)
    denominator = term_frequency + BM25_K1 * (
        1.0 - BM25_B + BM25_B * normalized_length
    )
    return term_frequency * (BM25_K1 + 1.0) / denominator


def _iter_occurrences(text: str, terms: list[str]) -> Iterator[tuple[int, int, int]]:
    """Merge per-term substring scans without retaining every occurrence."""
    pending: list[tuple[int, int]] = []
    for term_index, term in enumerate(terms):
        index = text.find(term)
        if index >= 0:
            heappush(pending, (index, term_index))
    while pending:
        index, term_index = heappop(pending)
        term = terms[term_index]
        yield index, index + len(term), term_index
        next_index = text.find(term, index + max(1, len(term)))
        if next_index >= 0:
            heappush(pending, (next_index, term_index))


def _minimum_covering_span(text: str, terms: list[str]) -> int | None:
    """Smallest character span containing every query term, if one exists."""
    if len(terms) == 1:
        return len(terms[0]) if terms[0] in text else None

    best = _minimum_covering_span_scan(
        text, terms, max_occurrences=MAX_PROXIMITY_OCCURRENCES
    )
    present_terms = [
        (text.count(term), term) for term in terms if term in text
    ]
    if len(present_terms) < len(terms):
        return best

    _count, rare_term = min(present_terms)
    anchor = text.find(rare_term)
    anchors_checked = 0
    while anchor >= 0 and anchors_checked < MAX_SNIPPET_ANCHORS:
        window_start = max(0, anchor - MAX_SNIPPET_CHARS)
        window_end = min(
            len(text), anchor + len(rare_term) + MAX_SNIPPET_CHARS
        )
        local_span = _minimum_covering_span_scan(
            text[window_start:window_end], terms
        )
        if local_span is not None:
            best = local_span if best is None else min(best, local_span)
        anchors_checked += 1
        anchor = text.find(rare_term, anchor + max(1, len(rare_term)))
    return best


def _minimum_covering_span_scan(
    text: str, terms: list[str], max_occurrences: int | None = None
) -> int | None:
    latest: dict[int, tuple[int, int]] = {}
    start_heap: list[tuple[int, int]] = []
    end_heap: list[tuple[int, int]] = []
    best: int | None = None
    for occurrence_index, (start, end, term_index) in enumerate(
        _iter_occurrences(text, terms)
    ):
        if max_occurrences is not None and occurrence_index >= max_occurrences:
            break
        latest[term_index] = (start, end)
        if len(latest) < len(terms):
            continue
        if not start_heap:
            start_heap = [
                (position[0], index) for index, position in latest.items()
            ]
            end_heap = [
                (-position[1], index) for index, position in latest.items()
            ]
            start_heap.sort()
            end_heap.sort()
        else:
            heappush(start_heap, (start, term_index))
            heappush(end_heap, (-end, term_index))
        while start_heap and latest[start_heap[0][1]][0] != start_heap[0][0]:
            heappop(start_heap)
        while end_heap and latest[end_heap[0][1]][1] != -end_heap[0][0]:
            heappop(end_heap)
        # Rebuild occasionally so repeated single-character terms cannot leave
        # an unbounded number of stale heap entries in a long document.
        if len(start_heap) > len(terms) * 4:
            start_heap = sorted(
                (position[0], index) for index, position in latest.items()
            )
        if len(end_heap) > len(terms) * 4:
            end_heap = sorted(
                (-position[1], index) for index, position in latest.items()
            )
        span = -end_heap[0][0] - start_heap[0][0]
        best = span if best is None else min(best, span)
    return best


def _score_documents(
    documents: list[_Document], terms: list[str], phrase: str
) -> list[tuple[float, _Document]]:
    if not documents or not terms:
        return []

    document_count = len(documents)
    average_title_length = sum(doc.title_length for doc in documents) / document_count
    average_content_length = sum(doc.content_length for doc in documents) / document_count
    document_frequencies = {
        term: sum(
            1
            for doc in documents
            if term in doc.normalized_title or term in doc.normalized_content
        )
        for term in terms
    }
    idfs = {
        term: math.log(
            1.0
            + (document_count - document_frequencies[term] + 0.5)
            / (document_frequencies[term] + 0.5)
        )
        for term in terms
    }
    total_idf = sum(idfs.values())

    scored: list[tuple[float, _Document]] = []
    for doc in documents:
        matched_terms = 0
        score = 0.0
        for term in terms:
            title_tf = _count_occurrences(doc.normalized_title, term)
            content_tf = _count_occurrences(doc.normalized_content, term)
            if title_tf or content_tf:
                matched_terms += 1
            score += idfs[term] * (
                TITLE_WEIGHT
                * _bm25_tf(title_tf, doc.title_length, average_title_length)
                + _bm25_tf(content_tf, doc.content_length, average_content_length)
            )

        if matched_terms == 0:
            continue

        coverage = matched_terms / len(terms)
        # Coverage is additive so a document with every query term outranks a
        # document that repeats only one common term many times.
        score += total_idf * 0.8 * coverage * coverage

        title_span = _minimum_covering_span(doc.normalized_title, terms)
        content_span = _minimum_covering_span(doc.normalized_content, terms)
        spans = [span for span in (title_span, content_span) if span is not None]
        if spans:
            expected = sum(len(term) for term in terms)
            proximity = expected / max(expected, min(spans))
            score += total_idf * 0.7 * proximity

        if phrase and len(terms) > 1:
            if phrase in doc.normalized_title:
                score += total_idf * 1.2
            elif phrase in doc.normalized_content:
                score += total_idf * 0.45

        scored.append((score, doc))

    scored.sort(
        key=lambda item: (
            -item[0],
            item[1].normalized_title,
            item[1].memo_id,
        )
    )
    return scored


def _normalization_units(value: str) -> Iterator[tuple[int, int, str]]:
    """Yield source units that are safe for the normalization used by search.

    Combining sequences and decomposed Hangul Jamo must be normalized together;
    normalizing one code point at a time would make ranking and highlighting
    disagree for canonically equivalent text.
    """
    if not value:
        return
    start = 0
    unit = value[0]
    for index in range(1, len(value)):
        following = value[index]
        normalized_unit = _normalize(unit)
        if _normalize(unit + following) == normalized_unit + _normalize(following):
            yield start, index, normalized_unit
            start = index
            unit = following
        else:
            unit += following
    yield start, len(value), _normalize(unit)


def _normalized_with_source_map(value: str) -> tuple[str, list[tuple[int, int]]]:
    """Normalize for matching while retaining source ranges for segments."""
    normalized_parts: list[str] = []
    source_ranges: list[tuple[int, int]] = []
    for source_start, source_end, normalized_unit in _normalization_units(value):
        normalized_parts.append(normalized_unit)
        source_ranges.extend([(source_start, source_end)] * len(normalized_unit))
    return "".join(normalized_parts), source_ranges


def _source_match_ranges(value: str, terms: list[str]) -> list[tuple[int, int, int]]:
    normalized, source_ranges = _normalized_with_source_map(value)
    if not normalized or not source_ranges:
        return []
    ranges: list[tuple[int, int, int]] = []
    for term_index, term in enumerate(terms):
        start = 0
        while True:
            index = normalized.find(term, start)
            if index < 0:
                break
            normalized_end = index + len(term) - 1
            ranges.append(
                (
                    source_ranges[index][0],
                    source_ranges[normalized_end][1],
                    term_index,
                )
            )
            start = index + max(1, len(term))
    ranges.sort()
    return ranges


def _segments_for_range(
    value: str,
    terms: list[str],
    start: int = 0,
    end: int | None = None,
) -> list[dict]:
    end = len(value) if end is None else end
    fragment = value[start:end]
    matches = [
        (match_start, match_end)
        for match_start, match_end, _term_index in _source_match_ranges(fragment, terms)
    ]
    merged: list[tuple[int, int]] = []
    for match_start, match_end in matches:
        if not merged or match_start > merged[-1][1]:
            merged.append((match_start, match_end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], match_end))

    segments: list[dict] = []
    cursor = 0
    for match_start, match_end in merged:
        if match_start > cursor:
            segments.append({"text": fragment[cursor:match_start], "match": False})
        segments.append({"text": fragment[match_start:match_end], "match": True})
        cursor = match_end
    if cursor < len(fragment):
        segments.append({"text": fragment[cursor:], "match": False})
    return segments


def _best_match_cluster(value: str, terms: list[str]) -> tuple[int, int] | None:
    """Find a dense bounded window, anchored by exact phrase or rare terms."""
    if len(terms) == 1:
        index = value.find(terms[0])
        return (index, index + len(terms[0])) if index >= 0 else None

    phrase = " ".join(terms)
    phrase_index = value.find(phrase)
    if phrase_index >= 0:
        return phrase_index, phrase_index + len(phrase)

    term_counts = [(value.count(term), index, term) for index, term in enumerate(terms)]
    present_terms = [item for item in term_counts if item[0] > 0]
    if not present_terms:
        return None
    _count, _rare_term_index, rare_term = min(present_terms)

    best: tuple[tuple[int, int, int], tuple[int, int]] | None = None
    anchor = value.find(rare_term)
    anchors_checked = 0
    while anchor >= 0 and anchors_checked < MAX_SNIPPET_ANCHORS:
        window_start = max(0, anchor - MAX_SNIPPET_CHARS)
        window_end = min(len(value), anchor + len(rare_term) + MAX_SNIPPET_CHARS)
        window_value = value[window_start:window_end]
        candidate = _best_match_cluster_scan(window_value, terms)
        if candidate is not None:
            candidate_start = window_start + candidate[0]
            candidate_end = window_start + candidate[1]
            matched_terms = sum(
                term in value[candidate_start:candidate_end] for term in terms
            )
            quality = (
                matched_terms,
                -max(0, candidate_end - candidate_start),
                -candidate_start,
            )
            if best is None or quality > best[0]:
                best = (quality, (candidate_start, candidate_end))
        anchors_checked += 1
        anchor = value.find(rare_term, anchor + max(1, len(rare_term)))
    return best[1] if best else None


def _best_match_cluster_scan(value: str, terms: list[str]) -> tuple[int, int] | None:
    """Find the densest <= snippet-size cluster within a bounded value."""
    window: deque[tuple[int, int, int]] = deque()
    counts: Counter[int] = Counter()
    best: tuple[tuple[int, int, int], tuple[int, int]] | None = None
    for start, end, term_index in _iter_occurrences(value, terms):
        window.append((start, end, term_index))
        counts[term_index] += 1
        while window and end - window[0][0] > MAX_SNIPPET_CHARS:
            _old_start, _old_end, old_term = window.popleft()
            counts[old_term] -= 1
            if counts[old_term] == 0:
                del counts[old_term]
        if not window:
            continue
        quality = (len(counts), len(window), -(end - window[0][0]))
        if best is None or quality > best[0]:
            best = (quality, (window[0][0], end))
    return best[1] if best else None


def _source_bounds_for_normalized(
    value: str, normalized_start: int, normalized_end: int
) -> tuple[int, int]:
    normalized_cursor = 0
    source_start: int | None = None
    source_end = len(value)
    for unit_start, unit_end, normalized_unit in _normalization_units(value):
        unit_normalized_end = normalized_cursor + len(normalized_unit)
        if source_start is None and unit_normalized_end > normalized_start:
            source_start = unit_start
        if unit_normalized_end >= normalized_end:
            source_end = unit_end
            break
        normalized_cursor = unit_normalized_end
    return source_start or 0, source_end


def _best_sentence_bounds(value: str, terms: list[str]) -> tuple[int, int] | None:
    """Choose a sentence by term coverage first, then bounded hit density."""
    best: tuple[tuple[int, int, int], tuple[int, int]] | None = None
    start = 0
    for boundary in chain(_SENTENCE_BOUNDARY_RE.finditer(value), [None]):
        end = boundary.end() if boundary is not None else len(value)
        sentence = value[start:end]
        normalized = _normalize(sentence)
        counts = [_count_occurrences(normalized, term) for term in terms]
        coverage = sum(count > 0 for count in counts)
        if coverage:
            # Repetition helps choose a representative sentence but is capped,
            # so one repeated term cannot beat a sentence covering more terms.
            bounded_hits = sum(min(count, 8) for count in counts)
            quality = (coverage, bounded_hits, -len(sentence))
            if best is None or quality > best[0]:
                best = (quality, (start, end))
        start = end
    return best[1] if best else None


def _snippet_bounds(value: str, terms: list[str]) -> tuple[int, int]:
    if len(value) <= MAX_SNIPPET_CHARS:
        return 0, len(value)

    sentence = _best_sentence_bounds(value, terms)
    if sentence is None:
        return 0, MAX_SNIPPET_CHARS
    sentence_start, sentence_end = sentence
    while sentence_start < sentence_end and value[sentence_start].isspace():
        sentence_start += 1
    while sentence_end > sentence_start and value[sentence_end - 1].isspace():
        sentence_end -= 1
    if sentence_end - sentence_start <= MAX_SNIPPET_CHARS:
        return sentence_start, sentence_end

    sentence_value = value[sentence_start:sentence_end]
    cluster = _best_match_cluster(_normalize(sentence_value), terms)
    if cluster is None:
        return sentence_start, sentence_start + MAX_SNIPPET_CHARS
    local_start, local_end = _source_bounds_for_normalized(sentence_value, *cluster)
    cluster_start = sentence_start + local_start
    cluster_end = sentence_start + local_end
    padding = MAX_SNIPPET_CHARS - (cluster_end - cluster_start)
    start = max(sentence_start, cluster_start - padding // 2)
    end = min(sentence_end, start + MAX_SNIPPET_CHARS)
    start = max(sentence_start, end - MAX_SNIPPET_CHARS)
    return start, end


def _snippet_segments(value: str, terms: list[str]) -> list[dict]:
    if not value:
        return []
    start, end = _snippet_bounds(value, terms)
    segments = _segments_for_range(value, terms, start, end)
    if start > 0:
        segments.insert(0, {"text": "…", "match": False})
    if end < len(value):
        segments.append({"text": "…", "match": False})
    return segments


def search_memos(rows: list[dict], query: str, limit: int) -> dict:
    """Rank all current memo rows and return a bounded presentation contract."""
    visible_query = query.strip()
    terms = validate_search_query(visible_query)
    if not terms:
        return {"query": visible_query, "total": 0, "results": []}

    documents = [
        _Document(
            memo_id=str(row["id"]),
            title=str(row.get("title") or ""),
            content=str(row.get("content") or ""),
            normalized_title=_normalize(str(row.get("title") or "")),
            normalized_content=_normalize(str(row.get("content") or "")),
            title_length=_field_length(_normalize(str(row.get("title") or ""))),
            content_length=_field_length(_normalize(str(row.get("content") or ""))),
        )
        for row in rows
    ]
    normalized_phrase = _normalize(visible_query)
    scored = _score_documents(documents, terms, normalized_phrase)
    return {
        "query": visible_query,
        "total": len(scored),
        "results": [
            {
                "memoId": document.memo_id,
                "title": document.title,
                "score": round(score, 6),
                "titleSegments": _segments_for_range(document.title, terms),
                "snippetSegments": _snippet_segments(document.content, terms),
            }
            for score, document in scored[:limit]
        ],
    }

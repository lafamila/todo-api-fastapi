"""Pure task-rollup parser, grouping, alias, and marker mutation contract."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.services.task_rollup import (
    RollupValidationError,
    apply_markers,
    build_rollup,
    find_group,
)


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "task_rollup.json"


def memo(memo_id: str, content: str, *, title: str | None = None, revision: str | None = None) -> dict:
    return {
        "id": memo_id,
        "title": title or memo_id,
        "content": content,
        "revision": revision or f"rev-{memo_id}",
    }


def semantic_nodes(nodes: list[dict]) -> list[dict]:
    return [
        {
            "kind": node["kind"],
            "text": node["text"],
            "checked": node["checked"],
            "actionable": node["actionable"],
            "markers": sorted(item["marker"] for item in node["occurrences"]),
            "children": semantic_nodes(node["children"]),
        }
        for node in nodes
    ]


def flatten(nodes: list[dict]) -> list[dict]:
    return [node for item in nodes for node in [item, *flatten(item["children"])]]


@pytest.mark.parametrize(
    "case",
    json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["cases"],
    ids=lambda case: case["name"],
)
def test_cross_language_golden_parser_cases(case: dict) -> None:
    assert semantic_nodes(build_rollup(case["memos"])["groups"]) == case["expected"]


def test_recognizes_only_ascii_space_exact_markers() -> None:
    result = build_rollup([memo("syntax", " -- valid\n  v-- checked\n\t-- tab\nx -- embedded\n --v legacy\n--- lookalike\n --missing-space")])
    assert [node["text"] for node in flatten(result["groups"])] == ["valid"]


def test_normalizes_nfc_and_ascii_spaces_only_while_preserving_display_text() -> None:
    decomposed = "Cafe\u0301   launch"
    composed = "Caf\u00e9 launch"
    tabs = "Caf\u00e9\tlaunch"
    result = build_rollup([
        memo("a", f"--  {decomposed}  "), memo("b", f"-- {composed}"), memo("c", f"-- {tabs}"),
    ])
    assert len(result["groups"]) == 2
    grouped = next(node for node in result["groups"] if len(node["occurrences"]) == 2)
    assert grouped["text"] in {f" {decomposed}  ", composed}
    assert sorted(item["text"] for item in grouped["occurrences"]) == sorted([f" {decomposed}  ", composed])


def test_same_leaf_text_under_different_ancestors_stays_separate() -> None:
    result = build_rollup([memo("paths", "-- Backend\n  -- Deploy\n-- Frontend\n  -- Deploy")])
    deploys = [node for node in flatten(result["groups"]) if node["text"] == "Deploy"]
    assert len(deploys) == 2
    assert deploys[0]["key"] != deploys[1]["key"]
    assert deploys[0]["occurrences"][0]["physicalPath"] != deploys[1]["occurrences"][0]["physicalPath"]


def test_group_keys_digest_and_union_are_stable_across_input_and_child_order() -> None:
    first = [memo("a", "-- Parent\n  -- Alpha\n  -- Beta"), memo("b", "-- Parent\n  -- Gamma\n  -- Alpha")]
    reordered_children = [memo("a", "-- Parent\n  -- Beta\n  -- Alpha"), memo("b", "-- Parent\n  -- Alpha\n  -- Gamma")]
    baseline = build_rollup(first)
    memo_permutation = build_rollup(list(reversed(first)))
    child_permutation = build_rollup(reordered_children)
    assert baseline == memo_permutation
    assert sorted(node["key"] for node in flatten(baseline["groups"])) == sorted(
        node["key"] for node in flatten(child_permutation["groups"])
    )
    assert baseline["corpusDigest"] == memo_permutation["corpusDigest"]
    assert baseline["corpusDigest"] != child_permutation["corpusDigest"]
    assert {child["text"] for child in baseline["groups"][0]["children"]} == {"Alpha", "Beta", "Gamma"}


def test_checked_leaf_without_actionable_descendant_is_omitted() -> None:
    assert build_rollup([memo("done", "v-- Finished")])["groups"] == []


def test_parent_selection_uses_nearest_strictly_smaller_indent() -> None:
    root = build_rollup([memo("indent", "-- root\n -- one\n   -- three\n -- sibling\n  -- two")])["groups"][0]
    assert [node["text"] for node in root["children"]] == ["one", "sibling"]
    assert [node["text"] for node in root["children"][0]["children"]] == ["three"]
    assert [node["text"] for node in root["children"][1]["children"]] == ["two"]


def test_tree_depth_guard_accepts_boundary_and_rejects_whole_overflow() -> None:
    depth = 127
    content = "\n".join(
        [f"{' ' * index}- context {index}" for index in range(depth)]
        + [f"{' ' * depth}-- leaf"]
    )

    nodes = build_rollup([memo("deep", content)])["groups"]
    observed_depth = 0
    while nodes:
        observed_depth += 1
        nodes = nodes[0]["children"]

    assert observed_depth == depth + 1

    overflow = content + f"\n{' ' * (depth + 1)}-- too deep"
    with pytest.raises(RollupValidationError) as caught:
        build_rollup([memo("too-deep", overflow)])
    assert caught.value.code == "treeDepthExceeded"
    assert caught.value.status_code == 413


def test_candidates_require_proper_ascii_token_prefix_and_do_not_auto_merge() -> None:
    result = build_rollup([
        memo("a", "-- Deploy API"), memo("b", "-- Deploy API today"),
        memo("c", "-- Deploy APIs"), memo("d", "-- deploy API safely"),
    ])
    assert len(result["groups"]) == 4
    assert len(result["candidates"]) == 1
    candidate = result["candidates"][0]
    seed = find_group(result, candidate["seedKey"])
    variants = [find_group(result, key) for key in candidate["variantKeys"]]
    assert seed is not None and seed["text"] == "Deploy API"
    assert [node["text"] for node in variants if node is not None] == ["Deploy API today"]


def test_alias_accepts_selected_candidate_subset_and_rejects_unknown_keys() -> None:
    memos = [memo("a", "-- Deploy API"), memo("b", "-- Deploy API today"), memo("c", "-- Deploy API safely")]
    candidate = build_rollup(memos)["candidates"][0]
    selected = {"seedKey": candidate["seedKey"], "variantKeys": candidate["variantKeys"][:1]}
    aliased = build_rollup(memos, [selected])
    assert aliased["aliases"] == [selected]
    assert sorted(len(node["occurrences"]) for node in aliased["groups"]) == [1, 2]
    with pytest.raises(RollupValidationError):
        build_rollup(memos, [{"seedKey": candidate["seedKey"], "variantKeys": ["0" * 64]}])
    with pytest.raises(RollupValidationError):
        build_rollup(memos, [{"seedKey": candidate["seedKey"], "variantKeys": []}])


def test_confirmed_parent_alias_unions_children_and_preserves_physical_paths() -> None:
    memos = [memo("a", "v-- Release\n  -- API"), memo("b", "v-- Release today\n  -- API\n  -- Web")]
    base = build_rollup(memos)
    candidate = base["candidates"][0]
    aliased = build_rollup(memos, [candidate])
    assert aliased["aliases"] == [{"seedKey": candidate["seedKey"], "variantKeys": candidate["variantKeys"]}]
    assert len(aliased["groups"]) == 1
    parent = aliased["groups"][0]
    assert len(parent["occurrences"]) == 2
    assert {child["text"] for child in parent["children"]} == {"API", "Web"}
    api = next(child for child in parent["children"] if child["text"] == "API")
    assert len(api["occurrences"]) == 2
    assert len({tuple(item["physicalPath"]) for item in api["occurrences"]}) == 2


def test_checked_completed_leaf_variants_do_not_create_candidate_noise() -> None:
    result = build_rollup([memo("a", "v-- Done"), memo("b", "v-- Done today")])

    assert result["groups"] == []
    assert result["candidates"] == []


def test_candidate_variant_limit_is_reported_without_truncating_or_aliasing() -> None:
    memos = [memo("seed", "-- Task"), *(memo(f"v{index}", f"-- Task variant {index}") for index in range(9))]
    result = build_rollup(memos)
    seed = next(node for node in result["groups"] if node["text"] == "Task")
    assert len(result["groups"]) == 10
    assert result["candidates"] == [{"seedKey": seed["key"], "variantKeys": [], "reason": "variantLimitExceeded"}]


def test_alias_keeps_all_occurrences_beyond_legacy_first_64_limit() -> None:
    memos = [*(memo(f"m{index:03}", "-- Task") for index in range(96)), memo("variant", "-- Task extended")]
    base = build_rollup(memos)
    aliased = build_rollup(memos, [base["candidates"][0]])
    assert len(aliased["groups"][0]["occurrences"]) == 97


def test_occurrence_offsets_are_utf8_bytes_and_marker_application_preserves_crlf() -> None:
    content = "한글\r\n  -- Caf\u00e9\r\n-- 끝"
    occurrences = [node["occurrences"][0] for node in build_rollup([memo("utf8", content, revision="r1")])["groups"]]
    assert [item["lineIndex"] for item in occurrences] == [1, 2]
    assert occurrences[0]["byteStart"] == len("한글\r\n  ".encode("utf-8"))
    assert occurrences[1]["byteStart"] == len("한글\r\n  -- Caf\u00e9\r\n".encode("utf-8"))
    changed = apply_markers(content, occurrences)
    assert changed == "한글\r\n  v-- Caf\u00e9\r\nv-- 끝"
    assert apply_markers(changed, occurrences, checked=False) == content


def test_inverse_translates_original_offsets_after_three_variable_width_marker_changes() -> None:
    content = "-- 첫째 😀\r\n  -- second\r\n-- 끝 Caf\u00e9"
    rollup = build_rollup([memo("three", content, revision="r-three")])
    occurrences = [
        occurrence
        for node in flatten(rollup["groups"])
        for occurrence in node["occurrences"]
    ]

    completed = apply_markers(content, occurrences)

    assert completed == "v-- 첫째 😀\r\n  v-- second\r\nv-- 끝 Caf\u00e9"
    assert apply_markers(completed, occurrences, checked=False) == content


def test_marker_application_leaves_prechecked_copies_unchanged_and_undo_rejects_them() -> None:
    mixed = build_rollup([memo("a", "-- same"), memo("b", "v-- same")])
    all_occurrences = mixed["groups"][0]["occurrences"]
    unchecked = next(item for item in all_occurrences if not item["checked"])
    checked = next(item for item in all_occurrences if item["checked"])
    assert apply_markers("-- same", [unchecked]) == "v-- same"
    assert apply_markers("v-- same", [checked]) == "v-- same"
    with pytest.raises(RollupValidationError):
        apply_markers("v-- same", [checked], checked=False)


@pytest.mark.parametrize("mutator", [
    lambda item: item.update(byteStart=item["byteStart"] + 1),
    lambda item: item.update(lineIndex=item["lineIndex"] + 1),
    lambda item: item.update(text=item["text"] + " changed"),
    lambda item: item.update(key="0" * 64),
])
def test_marker_application_rejects_stale_or_tampered_manifest(mutator) -> None:
    content = "-- task"
    occurrence = build_rollup([memo("a", content)])["groups"][0]["occurrences"][0]
    tampered = copy.deepcopy(occurrence)
    mutator(tampered)
    with pytest.raises(RollupValidationError):
        apply_markers(content, [tampered])


@pytest.mark.parametrize("memos", [
    [memo(str(index), "") for index in range(1025)],
    [memo("too-large", "x" * (16 * 1024 * 1024 + 1))],
    [memo("too-many-tasks", "\n".join("-- task" for _ in range(4097)))],
])
def test_corpus_limits_reject_instead_of_truncating(memos: list[dict]) -> None:
    with pytest.raises(RollupValidationError):
        build_rollup(memos)


def test_exact_byte_budget_survives_256_marker_completions_and_undo() -> None:
    byte_limit = 16 * 1024 * 1024
    task_prefix = "\n".join("-- task" for _ in range(256)) + "\n"
    content = task_prefix + ("x" * (byte_limit - len(task_prefix.encode("utf-8"))))
    rollup = build_rollup([memo("near-cap", content, revision="r1")])
    occurrences = rollup["groups"][0]["occurrences"]

    completed = apply_markers(content, occurrences)

    assert len(completed.encode("utf-8")) == byte_limit + 256
    assert build_rollup([memo("near-cap", completed, revision="r2")])["groups"] == []
    restored = apply_markers(completed, occurrences, checked=False)
    assert restored == content
    assert build_rollup([memo("near-cap", restored, revision="r3")])["groups"]


@pytest.mark.parametrize("prefix", ["plain\n", "```\nv-- fenced\n```\n"])
def test_over_budget_plain_or_fenced_checked_text_is_not_discounted(prefix: str) -> None:
    byte_limit = 16 * 1024 * 1024
    content = prefix + ("x" * (byte_limit + 1 - len(prefix.encode("utf-8"))))

    with pytest.raises(RollupValidationError) as caught:
        build_rollup([memo("over-cap", content)])

    assert caught.value.code == "corpusLimitExceeded"
    assert caught.value.status_code == 413


def test_maximum_prefix_heavy_task_corpus_is_processed_without_quadratic_scan() -> None:
    content = "\n".join(
        ["-- Task"]
        + [f"-- Task variant {index:04d}" for index in range(4095)]
    )

    result = build_rollup([memo("maximum", content)])

    assert len(result["groups"]) == 4096
    seed = next(node for node in result["groups"] if node["text"] == "Task")
    assert {"seedKey": seed["key"], "variantKeys": [], "reason": "variantLimitExceeded"} in result["candidates"]


@pytest.mark.parametrize("content", ["- context\n" * 8193, "\n" * 131072])
def test_context_and_blank_line_amplification_is_rejected_before_json_encoding(content: str) -> None:
    with pytest.raises(RollupValidationError) as caught:
        build_rollup([memo("resource-limit", content)])
    assert caught.value.status_code == 413


@pytest.mark.parametrize("bad_content", ["line\rbare", "bad surrogate \ud800"])
def test_invalid_source_encoding_or_line_endings_are_rejected(bad_content: str) -> None:
    with pytest.raises(RollupValidationError):
        build_rollup([memo("bad", bad_content)])


def test_duplicate_memo_ids_and_missing_revision_are_rejected() -> None:
    with pytest.raises(RollupValidationError):
        build_rollup([memo("same", "-- one"), memo("same", "-- two")])
    invalid = memo("missing-revision", "-- task")
    del invalid["revision"]
    with pytest.raises(RollupValidationError):
        build_rollup([invalid])


def test_find_group_returns_context_nodes_and_none_for_unknown_key() -> None:
    rollup = build_rollup([memo("tree", "v-- Parent\n  -- Child")])
    parent = rollup["groups"][0]
    assert find_group(rollup, parent["key"]) is parent
    assert find_group(rollup, "not-a-key") is None

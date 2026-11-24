"""Pure parser and grouping engine for project task rollups.

The service deliberately has no database, clock, or network dependencies.  It
works from saved memo snapshots and returns camelCase dictionaries that can be
validated again inside the API transaction.
"""

from __future__ import annotations

from bisect import bisect_left
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence


MAX_MEMOS = 256
MAX_CORPUS_BYTES = 16 * 1024 * 1024
MAX_TASK_OCCURRENCES = 4096
MAX_RECOGNIZED_NODES = 8192
MAX_SOURCE_LINES = 131072
MAX_ALIAS_VARIANTS = 8
MAX_TREE_DEPTH = 128

_TASK_UNCHECKED = "-- "
_TASK_CHECKED = "v-- "
_DESCRIPTION = "- "
_MARKER_PATTERN = re.compile(r"^( *)(v-- |-- |- )(.*)$")
_ASCII_SPACES = re.compile(r" +")
_LANGUAGE_TOKEN = r"[A-Za-z0-9_+#.-]+"
_SUFFIX_FENCE = re.compile(rf"^```\s*({_LANGUAGE_TOKEN})?\s*$")
_PREFIX_FENCE = re.compile(rf"^({_LANGUAGE_TOKEN})```$")

_CODE_LANGUAGES = {
    "typescript", "javascript", "python", "java", "kotlin", "swift", "go",
    "rust", "cpp", "csharp", "objective-c", "php", "ruby", "scala", "dart",
    "r", "perl", "lua", "sql", "mysql", "pgsql", "graphql", "html", "css",
    "scss", "less", "json", "yaml", "xml", "markdown", "ini", "dockerfile",
    "shell", "powershell", "bat", "protobuf", "plaintext",
}
_LANGUAGE_ALIASES = {
    "ts": "typescript", "tsx": "typescript", "mts": "typescript", "cts": "typescript",
    "js": "javascript", "jsx": "javascript", "mjs": "javascript", "cjs": "javascript",
    "node": "javascript", "py": "python", "python3": "python", "rb": "ruby",
    "rs": "rust", "kt": "kotlin", "kts": "kotlin", "c": "cpp", "c++": "cpp",
    "cxx": "cpp", "cc": "cpp", "hpp": "cpp", "c#": "csharp", "cs": "csharp",
    "objc": "objective-c", "obj-c": "objective-c", "golang": "go", "sh": "shell",
    "bash": "shell", "zsh": "shell", "console": "shell", "shellscript": "shell",
    "ps1": "powershell", "pwsh": "powershell", "yml": "yaml", "md": "markdown",
    "mdx": "markdown", "text": "plaintext", "txt": "plaintext", "plain": "plaintext",
    "none": "plaintext", "htm": "html", "postgres": "pgsql", "postgresql": "pgsql",
    "docker": "dockerfile", "proto": "protobuf", "cmd": "bat", "batch": "bat",
}


class RollupValidationError(ValueError):
    """A safe, content-free validation error suitable for an HTTP response."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "invalidRollup",
        status_code: int = 422,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass
class _Memo:
    memo_id: str
    title: str
    content: str
    revision: str
    byte_size: int


@dataclass
class _PhysicalNode:
    kind: str
    text: str
    normalized: str
    indent: int
    memo: _Memo
    line_index: int
    byte_start: int
    marker: str
    checked: bool
    children: list["_PhysicalNode"] = field(default_factory=list)
    physical_path: tuple[str, ...] = ()
    base_key: str = ""
    has_actionable_subtree: bool = False


@dataclass
class _BaseGroup:
    key: str
    kind: str
    normalized: str
    ancestor_path: tuple[str, ...]
    displays: list[tuple[tuple[str, int, str], str]] = field(default_factory=list)
    has_actionable_subtree: bool = False
    first_source: tuple[str, int] = ("", 0)

    @property
    def display(self) -> str:
        return min(self.displays, key=lambda item: item[0])[1]


@dataclass
class _Aggregate:
    key: str
    kind: str
    normalized: str
    first_source: tuple[str, int]
    displays: list[tuple[tuple[str, int, str], str]] = field(default_factory=list)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    children: dict[str, "_Aggregate"] = field(default_factory=dict)
    forced_text: str | None = None


def _validation(
    message: str,
    *,
    code: str = "invalidRollup",
    status_code: int = 422,
) -> RollupValidationError:
    return RollupValidationError(message, code=code, status_code=status_code)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _normalize(text: str) -> str:
    return _ASCII_SPACES.sub(" ", unicodedata.normalize("NFC", text).strip(" "))


def _component(kind: str, normalized: str) -> str:
    return f"{kind}:{normalized}"


def _node_key(kind: str, path: Sequence[str]) -> str:
    return _digest([kind, list(path)])


def _occurrence_key(occurrence: Mapping[str, Any]) -> str:
    return _digest([
        occurrence["memoId"],
        occurrence["revision"],
        occurrence["lineIndex"],
        occurrence["byteStart"],
        occurrence["marker"],
        occurrence["text"],
        occurrence["physicalPath"],
    ])


def _validate_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise _validation(f"{field_name} must be a string", code="invalidMemo")
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise _validation(f"{field_name} is not valid UTF-8", code="invalidSource") from exc
    return value


def _validate_source(content: Any) -> tuple[str, int]:
    source = _validate_text(content, "content")
    if re.search(r"\r(?!\n)", source):
        raise _validation("content contains a bare CR line ending", code="invalidSource")
    return source, len(source.encode("utf-8"))


def _read_memos(memos: Iterable[Mapping[str, Any]]) -> list[_Memo]:
    if isinstance(memos, (str, bytes, Mapping)):
        raise _validation("memos must be an array", code="invalidMemo")
    try:
        raw_memos = list(memos)
    except TypeError as exc:
        raise _validation("memos must be an array", code="invalidMemo") from exc
    if len(raw_memos) > MAX_MEMOS:
        raise _validation("memo limit exceeded", code="corpusLimitExceeded", status_code=413)

    result: list[_Memo] = []
    seen_ids: set[str] = set()
    total_bytes = 0
    total_lines = 0
    for raw in raw_memos:
        if not isinstance(raw, Mapping):
            raise _validation("every memo must be an object", code="invalidMemo")
        memo_id = _validate_text(raw.get("id"), "id")
        title = _validate_text(raw.get("title"), "title")
        revision = _validate_text(raw.get("revision"), "revision")
        content, byte_size = _validate_source(raw.get("content"))
        if not memo_id or not revision:
            raise _validation("memo id and revision must not be empty", code="invalidMemo")
        if memo_id in seen_ids:
            raise _validation("memo ids must be unique", code="invalidMemo")
        seen_ids.add(memo_id)
        total_bytes += byte_size
        total_lines += content.count("\n") + 1
        if total_bytes > MAX_CORPUS_BYTES + MAX_TASK_OCCURRENCES:
            raise _validation("corpus byte limit exceeded", code="corpusLimitExceeded", status_code=413)
        if total_lines > MAX_SOURCE_LINES:
            raise _validation("source line limit exceeded", code="corpusLimitExceeded", status_code=413)
        result.append(_Memo(memo_id, title, content, revision, byte_size))
    return sorted(result, key=lambda item: item.memo_id)


def _is_code_fence(line: str) -> bool:
    if _SUFFIX_FENCE.fullmatch(line):
        return True
    match = _PREFIX_FENCE.fullmatch(line)
    if match is None:
        return False
    token = match.group(1).strip().lower()
    return _LANGUAGE_ALIASES.get(token, token) in _CODE_LANGUAGES


def _boundary(line: str, start: str, junction: str, end: str) -> list[str] | None:
    if not line.startswith(start) or not line.endswith(end):
        return None
    segments = line[len(start) : len(line) - len(end)].split(junction)
    if not segments or any(not segment or set(segment) != {"─"} for segment in segments):
        return None
    return segments


def _prepared_table_line(line: str) -> tuple[str, str]:
    match = re.match(r"^[ \t]*", line)
    indent = match.group(0) if match else ""
    return indent, line[len(indent) :].rstrip()


def _table_end(lines: Sequence[str], start_index: int) -> int | None:
    opening_indent, opening_body = _prepared_table_line(lines[start_index])
    opening = _boundary(opening_body, "┌", "┬", "┐")
    if opening is None:
        return None
    columns = len(opening)
    pending_rows = 0
    completed_rows = 0

    for index in range(start_index + 1, len(lines)):
        indent, body = _prepared_table_line(lines[index])
        if indent != opening_indent:
            return None
        closing = _boundary(body, "└", "┴", "┘")
        if closing is not None:
            if len(closing) != columns or completed_rows == 0 and pending_rows == 0:
                return None
            return index + 1
        separator = _boundary(body, "├", "┼", "┤")
        if separator is not None:
            if len(separator) != columns or completed_rows == 0 and pending_rows == 0:
                return None
            if pending_rows:
                completed_rows += 1
                pending_rows = 0
            continue
        if "─" in body and re.fullmatch(r"[─├┼┤]+", body):
            continue
        if not body.startswith("│") or not body.endswith("│"):
            return None
        escaped = [offset for offset, char in enumerate(body) if char == "│" and not (offset > 0 and body[offset - 1] == "\\")]
        legacy = [offset for offset, char in enumerate(body) if char == "│"]
        valid = (
            len(escaped) == columns + 1 and escaped[0] == 0
        ) or (
            len(legacy) == columns + 1 and legacy[0] == 0
        )
        if not valid:
            return None
        pending_rows += 1
    return None


def _memo_lines(content: str) -> tuple[list[str], list[int]]:
    raw_lines = content.split("\n")
    lines: list[str] = []
    byte_starts: list[int] = []
    offset = 0
    for index, raw in enumerate(raw_lines):
        line = raw[:-1] if raw.endswith("\r") else raw
        lines.append(line)
        byte_starts.append(offset)
        offset += len(raw.encode("utf-8"))
        if index < len(raw_lines) - 1:
            offset += 1
    return lines, byte_starts


def _parse_memo(
    memo: _Memo, *, task_budget: int, node_budget: int,
) -> tuple[list[_PhysicalNode], int, int, int]:
    lines, line_byte_starts = _memo_lines(memo.content)
    roots: list[_PhysicalNode] = []
    stack: list[_PhysicalNode] = []
    task_count = 0
    checked_task_count = 0
    node_count = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        if _is_code_fence(line):
            stack.clear()
            index += 1
            while index < len(lines) and not _is_code_fence(lines[index]):
                index += 1
            if index < len(lines):
                index += 1
            continue

        table_end = _table_end(lines, index)
        if table_end is not None:
            stack.clear()
            index = table_end
            continue

        if line.strip() == "":
            index += 1
            continue

        match = _MARKER_PATTERN.fullmatch(line)
        if match is None:
            stack.clear()
            index += 1
            continue

        indent_text, marker, text = match.groups()
        kind = "description" if marker == _DESCRIPTION else "task"
        node_count += 1
        task_count += kind == "task"
        checked_task_count += kind == "task" and marker == _TASK_CHECKED
        if task_count > task_budget or node_count > node_budget:
            raise _validation("recognized task/context limit exceeded", code="corpusLimitExceeded", status_code=413)
        node = _PhysicalNode(
            kind=kind,
            text=text,
            normalized=_normalize(text),
            indent=len(indent_text),
            memo=memo,
            line_index=index,
            byte_start=line_byte_starts[index] + len(indent_text),
            marker=marker,
            checked=marker == _TASK_CHECKED,
        )
        while stack and stack[-1].indent >= node.indent:
            stack.pop()
        if len(stack) + 1 > MAX_TREE_DEPTH:
            raise _validation(
                "task tree depth limit exceeded",
                code="treeDepthExceeded",
                status_code=413,
            )
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)
        index += 1
    return roots, task_count, node_count, checked_task_count


def _assign_physical_identity(
    node: _PhysicalNode,
    ancestors: tuple[str, ...],
    base_groups: dict[str, _BaseGroup],
) -> None:
    pending = [(node, ancestors)]
    while pending:
        current, current_ancestors = pending.pop()
        component = _component(current.kind, current.normalized)
        current.physical_path = (*current_ancestors, component)
        current.base_key = _node_key(current.kind, current.physical_path)
        source = (current.memo.memo_id, current.line_index)
        group = base_groups.get(current.base_key)
        if group is None:
            group = _BaseGroup(
                key=current.base_key,
                kind=current.kind,
                normalized=current.normalized,
                ancestor_path=current_ancestors,
                first_source=source,
            )
            base_groups[current.base_key] = group
        group.displays.append(((current.memo.memo_id, current.line_index, current.text), current.text))
        group.first_source = min(group.first_source, source)
        pending.extend(
            (child, current.physical_path) for child in reversed(current.children)
        )

    postorder: list[tuple[_PhysicalNode, bool]] = [(node, False)]
    while postorder:
        current, visited = postorder.pop()
        if not visited:
            postorder.append((current, True))
            postorder.extend((child, False) for child in reversed(current.children))
            continue
        current.has_actionable_subtree = (
            current.kind == "task" and not current.checked
        ) or any(child.has_actionable_subtree for child in current.children)
        base_groups[current.base_key].has_actionable_subtree = (
            base_groups[current.base_key].has_actionable_subtree
            or current.has_actionable_subtree
        )


def _candidate_sets(base_groups: Mapping[str, _BaseGroup]) -> list[dict[str, Any]]:
    tasks = [group for group in base_groups.values() if group.kind == "task"]
    tasks.sort(key=lambda item: (item.first_source, item.key))
    by_ancestor: dict[tuple[str, ...], list[_BaseGroup]] = {}
    for task in tasks:
        by_ancestor.setdefault(task.ancestor_path, []).append(task)
    buckets: dict[
        tuple[str, ...],
        tuple[list[_BaseGroup], list[str], list[_BaseGroup], list[str]],
    ] = {}
    for ancestor_path, groups in by_ancestor.items():
        ordered = sorted(groups, key=lambda item: (item.normalized, item.key))
        actionable = [item for item in ordered if item.has_actionable_subtree]
        buckets[ancestor_path] = (
            ordered,
            [item.normalized for item in ordered],
            actionable,
            [item.normalized for item in actionable],
        )

    candidates: list[dict[str, Any]] = []
    for seed in tasks:
        ordered, labels, actionable, actionable_labels = buckets[seed.ancestor_path]
        search_groups, search_labels = (
            (ordered, labels)
            if seed.has_actionable_subtree
            else (actionable, actionable_labels)
        )
        prefix = f"{seed.normalized} "
        start = bisect_left(search_labels, prefix)
        end = bisect_left(search_labels, f"{seed.normalized}!")
        extension_count = end - start
        if extension_count == 0:
            continue
        if extension_count > MAX_ALIAS_VARIANTS:
            candidates.append({
                "seedKey": seed.key,
                "variantKeys": [],
                "reason": "variantLimitExceeded",
            })
        else:
            extensions = search_groups[start:end]
            candidates.append({
                "seedKey": seed.key,
                "variantKeys": [item.key for item in extensions],
                "reason": "properTokenPrefix",
            })
    return candidates


def _validate_aliases(
    aliases: Any,
    candidates: Sequence[Mapping[str, Any]],
    base_groups: Mapping[str, _BaseGroup],
) -> tuple[list[dict[str, Any]], dict[str, tuple[str, str]]]:
    if aliases is None:
        return [], {}
    if not isinstance(aliases, Sequence) or isinstance(aliases, (str, bytes)):
        raise _validation("aliases must be an array", code="invalidAlias")
    candidate_by_seed = {
        item["seedKey"]: item
        for item in candidates
        if item["reason"] == "properTokenPrefix"
    }
    canonical: list[dict[str, Any]] = []
    member_map: dict[str, tuple[str, str]] = {}
    for raw in aliases:
        if not isinstance(raw, Mapping):
            raise _validation("every alias must be an object", code="invalidAlias")
        seed_key = raw.get("seedKey")
        variant_keys = raw.get("variantKeys")
        if not isinstance(seed_key, str) or not isinstance(variant_keys, Sequence) or isinstance(variant_keys, (str, bytes)):
            raise _validation("alias keys must be strings and arrays", code="invalidAlias")
        if any(not isinstance(key, str) for key in variant_keys):
            raise _validation("alias variant keys must be strings", code="invalidAlias")
        variants = sorted(set(variant_keys))
        if not variants:
            raise _validation("alias must select at least one variant", code="invalidAlias")
        if len(variants) != len(variant_keys) or seed_key in variants:
            raise _validation("alias keys must be distinct", code="invalidAlias")
        candidate = candidate_by_seed.get(seed_key)
        offered = set(candidate["variantKeys"]) if candidate is not None else set()
        if candidate is None or not set(variants).issubset(offered):
            raise _validation("alias does not match the current candidate manifest", code="staleAlias", status_code=409)
        members = [seed_key, *variants]
        if len(variants) > MAX_ALIAS_VARIANTS:
            raise _validation("alias variant limit exceeded", code="aliasLimitExceeded", status_code=413)
        if any(key not in base_groups or key in member_map for key in members):
            raise _validation("alias members are missing or overlap", code="invalidAlias")
        alias_id = _digest(["alias", sorted(members)])
        seed_text = base_groups[seed_key].display
        for key in members:
            member_map[key] = (alias_id, seed_text)
        canonical.append({"seedKey": seed_key, "variantKeys": variants})
    canonical.sort(key=lambda item: item["seedKey"])
    return canonical, member_map


def _make_occurrence(node: _PhysicalNode) -> dict[str, Any]:
    occurrence = {
        "key": "",
        "memoId": node.memo.memo_id,
        "memoTitle": node.memo.title,
        "revision": node.memo.revision,
        "lineIndex": node.line_index,
        "byteStart": node.byte_start,
        "marker": node.marker,
        "checked": node.checked,
        "text": node.text,
        "physicalPath": list(node.physical_path),
    }
    occurrence["key"] = _occurrence_key(occurrence)
    return occurrence


def _merge_physical_node(
    node: _PhysicalNode,
    target: dict[str, _Aggregate],
    logical_ancestors: tuple[str, ...],
    alias_members: Mapping[str, tuple[str, str]],
) -> None:
    pending = [(node, target, logical_ancestors)]
    while pending:
        current, current_target, current_ancestors = pending.pop()
        alias = alias_members.get(current.base_key)
        logical_component = (
            f"task-alias:{alias[0]}"
            if alias
            else _component(current.kind, current.normalized)
        )
        logical_path = (*current_ancestors, logical_component)
        key = _node_key(current.kind, logical_path)
        source = (current.memo.memo_id, current.line_index)
        aggregate = current_target.get(key)
        if aggregate is None:
            aggregate = _Aggregate(
                key=key,
                kind=current.kind,
                normalized=current.normalized,
                first_source=source,
                forced_text=alias[1] if alias else None,
            )
            current_target[key] = aggregate
        aggregate.first_source = min(aggregate.first_source, source)
        aggregate.displays.append(
            ((current.memo.memo_id, current.line_index, current.text), current.text)
        )
        if current.kind == "task":
            aggregate.occurrences.append(_make_occurrence(current))
        pending.extend(
            (child, aggregate.children, logical_path)
            for child in reversed(current.children)
        )


def _render_aggregates(aggregates: Mapping[str, _Aggregate]) -> list[dict[str, Any]]:
    roots = list(aggregates.values())
    completed: dict[int, dict[str, Any] | None] = {}
    pending: list[tuple[_Aggregate, bool]] = [
        (aggregate, False) for aggregate in reversed(roots)
    ]
    while pending:
        aggregate, visited = pending.pop()
        if not visited:
            pending.append((aggregate, True))
            pending.extend(
                (child, False) for child in reversed(list(aggregate.children.values()))
            )
            continue
        children_with_order = [
            (child.first_source, child.key, completed[id(child)])
            for child in aggregate.children.values()
            if completed.get(id(child)) is not None
        ]
        children_with_order.sort(key=lambda item: (item[0], item[1]))
        children = [item[2] for item in children_with_order]
        occurrences = sorted(
            aggregate.occurrences,
            key=lambda item: (
                item["memoId"], item["revision"], item["lineIndex"], item["byteStart"], item["key"],
            ),
        )
        actionable = aggregate.kind == "task" and any(not item["checked"] for item in occurrences)
        if not actionable and not children:
            completed[id(aggregate)] = None
            continue
        text = aggregate.forced_text or min(aggregate.displays, key=lambda item: item[0])[1]
        completed[id(aggregate)] = {
            "key": aggregate.key,
            "kind": aggregate.kind,
            "text": text,
            "checked": aggregate.kind == "task" and bool(occurrences) and all(item["checked"] for item in occurrences),
            "actionable": actionable,
            "occurrences": occurrences,
            "children": children,
        }
    rendered = [
        (aggregate.first_source, aggregate.key, completed[id(aggregate)])
        for aggregate in roots
        if completed.get(id(aggregate)) is not None
    ]
    rendered.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in rendered]


def build_rollup(
    memos: Iterable[Mapping[str, Any]],
    aliases: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a deterministic rollup from authoritative saved memo snapshots."""

    parsed_memos = _read_memos(memos)
    physical_roots: list[_PhysicalNode] = []
    task_count = 0
    node_count = 0
    checked_task_count = 0
    for memo in parsed_memos:
        roots, memo_task_count, memo_node_count, memo_checked_task_count = _parse_memo(
            memo, task_budget=MAX_TASK_OCCURRENCES - task_count,
            node_budget=MAX_RECOGNIZED_NODES - node_count,
        )
        physical_roots.extend(roots)
        task_count += memo_task_count
        node_count += memo_node_count
        checked_task_count += memo_checked_task_count
        if task_count > MAX_TASK_OCCURRENCES:
            raise _validation("task occurrence limit exceeded", code="corpusLimitExceeded", status_code=413)
    logical_corpus_bytes = sum(memo.byte_size for memo in parsed_memos) - checked_task_count
    if logical_corpus_bytes > MAX_CORPUS_BYTES:
        raise _validation(
            "corpus byte limit exceeded",
            code="corpusLimitExceeded",
            status_code=413,
        )
    corpus_digest = _digest([
        {
            "id": memo.memo_id,
            "title": memo.title,
            "content": memo.content,
            "revision": memo.revision,
        }
        for memo in parsed_memos
    ])

    base_groups: dict[str, _BaseGroup] = {}
    for root in physical_roots:
        _assign_physical_identity(root, (), base_groups)
    candidates = _candidate_sets(base_groups)
    canonical_aliases, alias_members = _validate_aliases(aliases, candidates, base_groups)

    aggregate_roots: dict[str, _Aggregate] = {}
    for root in physical_roots:
        _merge_physical_node(root, aggregate_roots, (), alias_members)
    return {
        "corpusDigest": corpus_digest,
        "groups": _render_aggregates(aggregate_roots),
        "candidates": candidates,
        "aliases": canonical_aliases,
    }


def find_group(rollup: Mapping[str, Any], group_key: str) -> dict[str, Any] | None:
    """Find a task or context node recursively by its stable group key."""

    groups = rollup.get("groups", []) if isinstance(rollup, Mapping) else []
    if not isinstance(groups, Sequence):
        return None
    stack = list(reversed(groups))
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        if node.get("key") == group_key:
            return node
        children = node.get("children", [])
        if isinstance(children, Sequence) and not isinstance(children, (str, bytes)):
            stack.extend(reversed(children))
    return None


def _validated_occurrence(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise _validation("occurrence must be an object", code="invalidManifest")
    required_strings = ("key", "memoId", "memoTitle", "revision", "marker", "text")
    if any(not isinstance(raw.get(name), str) for name in required_strings):
        raise _validation("occurrence string field is invalid", code="invalidManifest")
    if raw["marker"] not in {_TASK_UNCHECKED, _TASK_CHECKED}:
        raise _validation("occurrence marker is invalid", code="invalidManifest")
    if type(raw.get("checked")) is not bool:
        raise _validation("occurrence checked field is invalid", code="invalidManifest")
    if raw["checked"] != (raw["marker"] == _TASK_CHECKED):
        raise _validation("occurrence marker state is inconsistent", code="invalidManifest")
    if type(raw.get("lineIndex")) is not int or raw["lineIndex"] < 0:
        raise _validation("occurrence line index is invalid", code="invalidManifest")
    if type(raw.get("byteStart")) is not int or raw["byteStart"] < 0:
        raise _validation("occurrence byte offset is invalid", code="invalidManifest")
    physical_path = raw.get("physicalPath")
    if not isinstance(physical_path, Sequence) or isinstance(physical_path, (str, bytes)) or any(not isinstance(item, str) for item in physical_path):
        raise _validation("occurrence physical path is invalid", code="invalidManifest")
    occurrence = dict(raw)
    occurrence["physicalPath"] = list(physical_path)
    if occurrence["key"] != _occurrence_key(occurrence):
        raise _validation("occurrence key does not match its manifest", code="staleManifest", status_code=409)
    return occurrence


def apply_markers(
    content: str,
    occurrences: Iterable[Mapping[str, Any]],
    checked: bool = True,
) -> str:
    """Apply exact marker-only changes using UTF-8 byte offsets.

    For completion, both unchecked and already checked group occurrences may be
    supplied; checked copies validate and remain unchanged.  For inverse use,
    supply only operation-changed occurrences (their recorded marker is ``-- ``)
    and ``checked=False``.
    """

    source, _ = _validate_source(content)
    if type(checked) is not bool:
        raise _validation("checked must be a boolean", code="invalidManifest")
    if isinstance(occurrences, (str, bytes, Mapping)):
        raise _validation("occurrences must be an array", code="invalidManifest")
    try:
        manifest = [_validated_occurrence(item) for item in occurrences]
    except TypeError as exc:
        raise _validation("occurrences must be an array", code="invalidManifest") from exc
    if len(manifest) > MAX_TASK_OCCURRENCES:
        raise _validation("occurrence limit exceeded", code="corpusLimitExceeded", status_code=413)

    source_bytes = source.encode("utf-8")
    changes: list[tuple[int, bytes, bytes]] = []
    seen_offsets: set[int] = set()
    inverse_shift: dict[int, int] = {}
    if not checked:
        original_offsets = sorted(item["byteStart"] for item in manifest)
        if len(original_offsets) != len(set(original_offsets)):
            raise _validation("occurrence offsets must be unique", code="invalidManifest")
        # Completion inserts one byte at every changed marker.  Receipts retain
        # the signed pre-operation offsets, so translate them without rewriting
        # either the manifest or its occurrence keys.
        inverse_shift = {offset: index for index, offset in enumerate(original_offsets)}
    for occurrence in manifest:
        original_offset = occurrence["byteStart"]
        if original_offset in seen_offsets:
            raise _validation("occurrence offsets must be unique", code="invalidManifest")
        seen_offsets.add(original_offset)
        if not checked and occurrence["marker"] != _TASK_UNCHECKED:
            raise _validation("only operation-changed occurrences may be unchecked", code="invalidManifest")
        offset = original_offset + inverse_shift.get(original_offset, 0)
        expected = occurrence["marker"] if checked else _TASK_CHECKED
        replacement = _TASK_CHECKED if checked else _TASK_UNCHECKED
        expected_bytes = expected.encode("ascii")
        if source_bytes[offset : offset + len(expected_bytes)] != expected_bytes:
            raise _validation("source marker no longer matches the manifest", code="staleManifest", status_code=409)

        line_start = source_bytes.rfind(b"\n", 0, offset) + 1
        if source_bytes[line_start:offset].strip(b" "):
            raise _validation("marker is not at an ASCII-indented line prefix", code="staleManifest", status_code=409)
        if source_bytes.count(b"\n", 0, line_start) != occurrence["lineIndex"]:
            raise _validation("source line no longer matches the manifest", code="staleManifest", status_code=409)
        text_start = offset + len(expected_bytes)
        line_end = source_bytes.find(b"\n", text_start)
        if line_end < 0:
            line_end = len(source_bytes)
        if line_end > text_start and source_bytes[line_end - 1 : line_end] == b"\r":
            line_end -= 1
        if source_bytes[text_start:line_end] != occurrence["text"].encode("utf-8"):
            raise _validation("source text no longer matches the manifest", code="staleManifest", status_code=409)
        if expected != replacement:
            changes.append((offset, expected_bytes, replacement.encode("ascii")))

    result = source_bytes
    for offset, expected, replacement in sorted(changes, reverse=True):
        result = result[:offset] + replacement + result[offset + len(expected) :]
    return result.decode("utf-8", "strict")


__all__ = [
    "RollupValidationError",
    "apply_markers",
    "build_rollup",
    "find_group",
]

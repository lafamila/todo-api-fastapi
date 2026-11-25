# Project backlog task rollup contract

Implementation contract for the reviewed root `.omx/plans/prd-todo-backlog-task-rollup.md` and companion test spec.
All names below are camelCase over HTTP; database fields are snake_case. Existing private backlog content is never a generated rollup store.

## HTTP

- `GET /api/projects/{projectId}/backlog/task-rollup`: default preview.
- `POST /api/projects/{projectId}/backlog/task-rollup/preview`: `{ aliases: Alias[] }`, for explicitly confirmed parent/text variants.
- `POST /api/projects/{projectId}/backlog/task-rollup/complete`: `CompleteRequest`.
- `POST /api/projects/{projectId}/backlog/task-rollup/{operationId}/undo`: `UndoRequest`.

```ts
type Alias = { seedKey: string; variantKeys: string[] }; // base/unaliased group keys
type Lease = { memoId: string; token: string };
type Occurrence = {
  key: string; memoId: string; memoTitle: string; revision: string;
  lineIndex: number; byteStart: number; marker: '-- ' | 'v-- ' | '- ';
  checked: boolean; text: string; physicalPath: string[];
};
type RollupNode = {
  key: string; kind: 'task' | 'description'; text: string;
  checked: boolean; actionable: boolean;
  occurrences: Occurrence[]; children: RollupNode[];
};
type Candidate = { seedKey: string; variantKeys: string[]; reason: string };
type AfterMemo = { memoId: string; revision: string; contentHash: string; updatedAtUtc: string };
type Completion = {
  operationId: string; projectId: string; groupKey: string;
  group: RollupNode; changedMemoIds: string[]; afterManifest: AfterMemo[];
  completedAtUtc: string; expiresAtUtc: string;
  status: 'ready' | 'pending' | 'superseded' | 'conflict' | 'undone'; canUndo: boolean;
  context?: { key: string; kind: 'task' | 'description'; text: string; checked: boolean }[];
  memoScope?: 'all' | 'recent30'; // immutable legacy receipt metadata only, not a product mode
};
type Preview = {
  projectId: string; corpusDigest: string;
  serverTimeUtc: string; nextBoundaryUtc: string; canComplete: boolean;
  groups: RollupNode[]; candidates: Candidate[]; aliases: Alias[];
  completions: Completion[];
};
type CompleteRequest = {
  operationId: string; corpusDigest: string; groupKey: string;
  aliases: Alias[]; occurrenceKeys: string[]; leases: Lease[];
};
type UndoRequest = { undoOperationId: string; corpusDigest: string; leases: Lease[] };
```

Every alias and physical occurrence is re-derived by the server. Task occurrences include checked copies in the selected group,
but leases are needed only for normal memos containing markers this operation will change. No child cascade.
Aliases are explicit request-scoped confirmation; the user may select any nonempty subset of offered variants, but each selected variant includes all physical occurrences. There is no automatic or persistent semantic equivalence.

## Project-wide corpus; recent 30 is a test fixture only

The product always aggregates the selected leaf project's entire saved normal-memo corpus. There is no all/recent30 selector, request option, or source-count split. Resource overflow rejects the whole request; no silent windowing or partial completion is allowed.

The user asked to TEST the large local `Novawork / Works` data with its latest 30 creation-date memos, not to add a product mode. Real originals are accessed only by the read-only export tool. Those exact 30 bodies may be copied into the fixed isolated QA database. Older real memos must not be read or checked as part of this validation; additional 295-cardinality sentinels are explicitly synthetic and prechecked, never copies of real old content.

Full eligible metadata/body range fencing still protects completion against a newly saved duplicate or phantom insert. The internal memo/affected-source ceiling is consistently 1,024, including batch leases and durable manifests, so 325-document projects do not fail at the former 256-row boundary. The 16 MiB, task-count and JSON guards remain whole-request bounds.

Older immutable receipts may retain a `memoScope` field from the removed implementation; that field is compatibility metadata only. Previously issued operation IDs/hashes must remain replayable without reinterpreting their original source manifests. Explicit undo still targets only the recorded operation's changed sources with current authorization, leases, and exact after proof. No automatic bulk check of old real Works memos is introduced.
Deprecated `memoScope` fields on old complete/undo bodies are accepted only for recorded-receipt compatibility, with exact stored scope/hash and operation-ID checks. A fresh completion carrying that field is rejected with `422`. New requests never send it; GET and preview have no scope option.
Description nodes are context only. Checked parent tasks remain context when unfinished children exist.
`occurrenceKeys` must cover ALL selected-group physical occurrences (not a client-selected subset).
Complete/undo return the durable `Completion` result; callers refetch preview after success/replay.

## Parsing service interface

`src/services/task_rollup.py` exports pure functions (implementation may add focused helpers):

- `build_rollup(memos, aliases=None) -> dict`: `{corpusDigest, groups, candidates, aliases}`. Each memo has
  `id`, `title`, `content`, and `revision`; service may accept `updated_at_utc` but never talks to DB.
- `find_group(rollup, group_key) -> dict | None`: recursive lookup, including context parents.
- `apply_markers(content, occurrences, checked=True) -> str`: exact source-marker replacement only, validates raw source,
  preserves LF/CRLF and all other bytes, supports inverse for operation-changed occurrences only.
- `RollupValidationError`: safe validation/limit/manifest failure, no raw document logging.

Stable comparison keys exclude checkbox state and child ordering. Occurrence keys include memo/revision/source position.
Use NFC/trim/ASCII-space collapse for comparison only; labels and original strings remain unchanged.
Blank lines are transparent, ordinary non-marker text and code/table groups close ancestry.
Corpus limits: 1,024 project/affected memos, 16 MiB logical UTF-8 source, 4096 task occurrences, 8192 recognized task/context nodes,
131072 source lines and 128 ancestry levels; at most 8 distinct confirmed variants per alias.
The logical byte budget excludes only the extra `v` byte of recognized checked task markers outside code/tables.
An absolute 16 MiB + 4096 raw-byte precheck bounds parsing. Marker-only completion/undo therefore cannot make
an accepted corpus unreadable; ordinary text or fenced `v--` receives no discount.
Any size/manifest overflow rejects the entire request; never truncate or split a completion.

## Durable completion row (sync schema v5)

`task_rollup_completions` uses:

```text
id VARCHAR(50) PK                   # complete operation UUID
project_id VARCHAR(50)
actor_id VARCHAR(50)
group_key VARCHAR(64)
payload_hash VARCHAR(64)
data_json LONGTEXT                  # immutable canonical JSON below
completed_at_utc DATETIME(3)
completion_date_kst DATE
undo_operation_id VARCHAR(50) NULL
undo_payload_hash VARCHAR(64) NULL
undo_data_json LONGTEXT NULL        # monotonic absent -> immutable result
updated_at_utc DATETIME(3)
deleted_at DATETIME(3) NULL          # tombstone, never implicit hard delete
```

`context` contains the noninteractive ancestor/description breadcrumb for the selected task; it does not wrap or expand the mutation target. The server derives it from the current confirmed-alias tree. Undo preserves it unchanged.

`data_json` is `{result: Completion, matchedOccurrences: Occurrence[], changedOccurrences: Occurrence[],
beforeRevisions: {[memoId]: string}, afterManifest: AfterMemo[]}`. Result is the response stored atomically with memo versions.
`undo_data_json` holds `{result: Completion, afterManifest: AfterMemo[]}` for the inverse operation.
Canonical JSON uses sorted keys, compact separators, UTF-8 text; no lease/reservation tokens, request IDs, or rotating secrets.
Actor/project/group/operation identity, payload hash, completion time and data_json are immutable.
Undo UUID/hash/data transition together from NULL to one immutable result; synced older rows cannot clear/rebind undo.
Tombstones are monotonic and do not resurrect source text. Metadata sync NEVER edits memos.

Read/feed/bootstrap/push/apply/dependencies/tombstones enforce exact authenticated actor plus live project scope.
Source references are snapshots: never hydrate inaccessible/moved current memo content to expose it.
Remote trails wait for source after clocks; later edits are superseded history, not permanent pending or suppression of fresh `--`.

## Batch lease adapter

Reservations protect an already-owned complete sorted lease set; they never acquire or steal editor leases.
Expose async `MemoRealtimeServer.reserve_task_rollup(user_id, operation_id, leases)` returning a reservation handle/context
whose async release runs AFTER actual SQL commit/rollback. Adapter validates REST-owned tokens (same dedicated socket owner),
delegates canonical reservation when online client, quarantines unknown outcomes, and maintains a renewal task.
Provide a synchronous `validate()` predicate on the handle for worker-side final pre-commit fencing; losing renewal invalidates it.
Intermediate checks use `validate_local()`; delegated batches make exactly one strict canonical validation RPC immediately before commit, not one per SQL statement.
Use short registry critical sections only, no global RLock across awaits/worker/SQL/network.
Concrete RPC/model names may be refined by the reservation owner; inform the API writer before integration.

## Transaction and UI invariants

- Live auth/project scope, then receipt lookup/hash replay BEFORE stale digest/lease/CAS validation.
- Fresh batch reserves leases before DB work, then uses root/leaf/member locking and full corpus indexed range/gap fencing.
- Repeat normal-memo write authorization in the transaction; private-backlog viewer exception is irrelevant.
- Source/version/receipt commit atomically. One version per changed memo; only selected marker prefixes differ.
- SQL/parser work uses bounded off-loop admission; Socket.IO heartbeats must continue.
- Completion today stays visible until server-provided next KST midnight, including local expiry if refresh fails.
- Rollup controls never write/focus/lease the private backlog body or steal its keyboard shortcuts.
- Unknown/lost response retries preserve the same operation UUID/payload; definitive stale-manifest conflicts require a new preview.

## Causality, privacy and limits

Only the selected leaf's latest **saved** normal memos are scanned, excluding history/private backlogs/code/tables. Unsaved drafts are never silently saved or overwritten. A parent checkbox never cascades into its children. Word variants require explicit confirmation; text alone cannot prove semantic equivalence.

Atomicity is one local database transaction, not distributed delivery or a global search of disconnected peers. An unseen offline copy or later reopened `--` is a fresh pending task on the next scan. Metadata sync never checks/unchecks sources. Both sync peers must run v5; push, pull and bootstrap fail closed across v4/v5 even when schema drift is enabled. There is no new env key or credential scope.

New synced receipts defer missing source dependencies. Later moved/deleted sources are accepted only as causally later historical references; no current text from an inaccessible project is hydrated. Completed and undo after-manifests gate presentation; a later edit is superseded history, not permanently pending or a suppression rule.

Overflow rejects the entire preview/batch; it never truncates a group. Besides corpus bounds above, HTTP/result manifests are <=1 MiB. A checked item is shown through its server-derived KST completion date and hidden at the next KST midnight, even if the boundary refresh fails. Original `v--` stays until an explicit edit or guarded undo.

## Operational events and metrics

The `uvicorn.error.task_rollup` logger emits content-free JSON with event `task_rollup`: UTC time, request/operation IDs, action/outcome, duration, source counts and safe cause/status. SQL rollback/replay, lease blocks, post-commit notification failures and incompatible sync schemas are separate events. Task text, snapshots, credentials, lease/reservation tokens and raw request bodies are never logged.

Use the included read-only stdlib reducer to inspect p50/p95 latency, conflict rate, lease blocks, rollbacks, replays, notification failures, schema rejections and alert conditions:

```bash
docker logs todo-api-fastapi 2>&1 | docker exec -i todo-api-fastapi python -m src.services.task_rollup_observability
# Native environment / saved Uvicorn log:
python3 scripts/task_rollup_metrics.py < /path/to/todo-api.log
```

`alerts > 0` marks an invariant/internal failure, notification failure, or schema mismatch for the operator's existing log/alert tooling. During a coordinated upgrade a transient v4/v5 mismatch is expected; persistent mismatch after both upgrades requires investigation. No metrics daemon or monitoring dependency is added.

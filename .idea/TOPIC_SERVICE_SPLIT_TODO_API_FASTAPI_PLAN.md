# TOPIC SERVICE SPLIT — todo-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TOPIC_SERVICE_SPLIT_PLAN.md`

## Repo Responsibility

`todo-api-fastapi` must stop owning topic storage, topic APIs, embeddings, and ML dependencies after `topic-api-fastapi` provides equivalent functionality.

## Inputs / Dependencies

- `topic-api-fastapi` must first expose compatible topic endpoints or the root plan must approve a contract change.
- Existing topic data preservation must be decided before deleting or ignoring old topic tables.
- Skill endpoint migration must be complete before removing todo topic routes.

## Work Items

1. Inventory current topic files and imports:
   - `src/routers/topics.py`
   - `src/models/topics.py`
   - `src/services/embedding.py`
   - router includes in `src/__main__.py`
   - topic DDL in `src/connectors/__init__.py`
   - topic dependencies in `requirements.txt`
2. After topic service parity is confirmed, remove topic router includes from `src/__main__.py`.
3. Remove topic models/router/services and any unused imports.
4. Remove topic DDL side effects from `init_db()`.
5. Remove `sentence-transformers` / PyTorch-related dependencies from todo requirements.
6. Update `CLAUDE.md` to state that topic functionality moved to `topic-api-fastapi`.

## Acceptance Criteria

- `todo-api-fastapi` starts without topic routes.
- Todo-owned endpoints for projects, memos, articles, and daily tasks still pass smoke tests.
- `rg "topic_" src requirements.txt` has no active todo runtime code matches, except intentional migration/handoff documentation.
- Todo dependency installation or Docker build no longer installs topic ML dependencies.

## Report Back To Orchestrator

- Whether existing topic tables/data are left in MySQL, migrated, or intentionally ignored.
- Any todo endpoint or frontend path that still depends on removed topic routes.
- Any dependency that cannot be removed because another todo feature uses it.

## Decision Escalation

사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.

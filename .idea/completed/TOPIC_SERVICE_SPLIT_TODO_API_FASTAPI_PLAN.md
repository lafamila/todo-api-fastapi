---
status: COMPLETED
summary: "todo-api-fastapi 에서 topic routes/schema/ML dependency 를 제거하고 topic data export 를 제공한다."
---

# TOPIC SERVICE SPLIT — todo-api-fastapi execution plan

Canonical orchestration plan:

`../.idea/TOPIC_SERVICE_SPLIT_PLAN.md`

## Repo Responsibility

`todo-api-fastapi` must stop owning topic storage, topic APIs, embeddings, and ML dependencies after `topic-api-fastapi` provides equivalent functionality.

## Inputs / Dependencies

- `topic-api-fastapi` must first expose compatible topic endpoints or the root plan must approve a contract change.
- Existing topic data preservation is decided: export current todo topic tables into migration artifacts, import them into the new topic DB, then remove todo runtime ownership.
- Skill endpoint migration must be complete before removing todo topic routes.
- Do not run automatic `DROP TABLE` or destructive DB cleanup in application startup.

## Work Items

1. Inventory current topic files and imports:
   - `src/routers/topics.py`
   - `src/models/topics.py`
   - `src/services/embedding.py`
   - router includes in `src/__main__.py`
   - topic DDL in `src/connectors/__init__.py`
   - topic dependencies in `requirements.txt`
2. Add a non-destructive export script for topic tables (`topic_sources`, `topics`, `topic_references`, `topic_hashtags`, `topic_questions`, `topic_answers`, `topic_blog_posts`, `topic_insight_exchanges`) so data can be inserted into the new topic DB.
3. After topic service parity and skill endpoint migration are confirmed, remove topic router includes from `src/__main__.py`.
4. Remove topic models/router/services and any unused imports.
5. Remove topic DDL side effects from `init_db()`. Do not drop existing DB tables automatically.
6. Remove `sentence-transformers` / PyTorch-related dependencies from todo requirements.
7. Update `CLAUDE.md` to state that topic functionality moved to `topic-api-fastapi`.

## Acceptance Criteria

- `todo-api-fastapi` starts without topic routes.
- Todo-owned endpoints for projects, memos, articles, and daily tasks still pass smoke tests.
- Topic data export script can produce a migration artifact without mutating the todo DB.
- `rg "topic_" src requirements.txt` has no active todo runtime code matches, except intentional migration/handoff documentation.
- Todo dependency installation or Docker build no longer installs topic ML dependencies.

## Report Back To Orchestrator

- Topic export artifact path and any assumptions required by topic import.
- Confirmation that existing topic tables are not automatically dropped by app startup.
- Any todo endpoint or frontend path that still depends on removed topic routes.
- Any dependency that cannot be removed because another todo feature uses it.

## Decision Escalation

사용자가 결정해야 하는 주요 사안은 임의로 판단하지 않는다. 작업을 중단하고 현재 orchestrator 에게 전달해 결정받은 뒤 진행한다. orchestrator 에 보고할 수 없으면 workspace root `.idea/` 에 handoff 문서를 남긴다.

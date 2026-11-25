from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


Id = Annotated[str, Field(min_length=1, max_length=50)]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
try:
    from ..services.task_rollup_limits import MAX_ROLLUP_MEMOS
except ImportError:  # pragma: no cover
    from services.task_rollup_limits import MAX_ROLLUP_MEMOS


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TaskRollupAlias(_StrictModel):
    seedKey: Digest
    variantKeys: list[Digest] = Field(default_factory=list, max_length=8)

    @field_validator("variantKeys")
    @classmethod
    def unique_variants(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("variantKeys must not contain duplicates")
        return value


class TaskRollupLease(_StrictModel):
    memoId: Id
    token: str = Field(min_length=1, max_length=2048)


class TaskRollupPreviewRequest(_StrictModel):
    aliases: list[TaskRollupAlias] = Field(default_factory=list, max_length=4096)


class TaskRollupCompleteRequest(_StrictModel):
    operationId: Id
    corpusDigest: Digest
    groupKey: Digest
    aliases: list[TaskRollupAlias] = Field(default_factory=list, max_length=4096)
    occurrenceKeys: list[Digest] = Field(min_length=1, max_length=4096)
    leases: list[TaskRollupLease] = Field(default_factory=list, max_length=MAX_ROLLUP_MEMOS)
    memoScope: Literal["all", "recent30"] | None = Field(
        default=None, deprecated=True
    )

    @field_validator("operationId")
    @classmethod
    def valid_operation_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("operationId must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError("operationId must use canonical UUID form")
        return str(parsed)

    @field_validator("occurrenceKeys")
    @classmethod
    def unique_occurrences(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("occurrenceKeys must not contain duplicates")
        return value

    @field_validator("leases")
    @classmethod
    def unique_leases(cls, value: list[TaskRollupLease]) -> list[TaskRollupLease]:
        memo_ids = [lease.memoId for lease in value]
        if len(memo_ids) != len(set(memo_ids)):
            raise ValueError("leases must contain one entry per memo")
        return value


class TaskRollupUndoRequest(_StrictModel):
    undoOperationId: Id
    corpusDigest: Digest
    leases: list[TaskRollupLease] = Field(default_factory=list, max_length=MAX_ROLLUP_MEMOS)
    memoScope: Literal["all", "recent30"] | None = Field(
        default=None, deprecated=True
    )

    @field_validator("undoOperationId")
    @classmethod
    def valid_operation_uuid(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("undoOperationId must be a UUID") from exc
        if str(parsed) != value.lower():
            raise ValueError("undoOperationId must use canonical UUID form")
        return str(parsed)

    @field_validator("leases")
    @classmethod
    def unique_leases(cls, value: list[TaskRollupLease]) -> list[TaskRollupLease]:
        memo_ids = [lease.memoId for lease in value]
        if len(memo_ids) != len(set(memo_ids)):
            raise ValueError("leases must contain one entry per memo")
        return value

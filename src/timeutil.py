"""시간대 명시 유틸 (워크스페이스 원칙 8).

이 레포는 과거 `datetime.now()` naive 값을 그대로 DB 에 넣었다. 동기화가 "누가 최신인가"를
timestamp 로 판정하므로 그 값은 판정 근거로 쓸 수 없다. 그래서 동기화 대상 테이블은
`updated_at_utc DATETIME(3)` 을 별도로 가지며 **항상 UTC** 를 담는다.

규칙:
    - DB 저장은 naive UTC (MySQL DATETIME 은 시간대를 저장하지 않는다).
    - JSON 노출은 항상 `...Z` 가 붙은 ISO-8601 문자열.
    - 사람이 읽는 표시(`memo_versions.note` 등)는 `Asia/Seoul` 로 변환한다.
    - 기존 naive 컬럼(`updated_at`, `created_at`, `invited_at`)은 `Asia/Seoul` 로 해석한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc
KST = timezone(timedelta(hours=9))
KST_OFFSET_SQL = "+09:00"


def _truncate_ms(value: datetime) -> datetime:
    """DATETIME(3) 정밀도에 맞춰 마이크로초를 밀리초로 절삭한다."""
    return value.replace(microsecond=(value.microsecond // 1000) * 1000)


def utcnow() -> datetime:
    """시간대 인식 현재 UTC (밀리초 정밀도)."""
    return _truncate_ms(datetime.now(UTC))


def utcnow_naive() -> datetime:
    """DB 저장용 naive UTC (밀리초 정밀도)."""
    return utcnow().replace(tzinfo=None)


def localnow_naive() -> datetime:
    """기존 표시용 naive 컬럼(`updated_at` 등)에 넣는 Asia/Seoul 벽시계 값."""
    return _truncate_ms(datetime.now(KST).replace(tzinfo=None))


def as_utc_naive(value: datetime | None, assume: timezone = KST) -> datetime | None:
    """어떤 datetime 이든 naive UTC 로 정규화한다. naive 입력은 `assume` 시간대로 해석."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=assume)
    return _truncate_ms(value.astimezone(UTC)).replace(tzinfo=None)


def iso_utc(value: datetime | None) -> str | None:
    """naive UTC(또는 aware) datetime → `2026-07-29T05:00:00.000Z`."""
    if value is None:
        return None
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return f"{_truncate_ms(value).isoformat(timespec='milliseconds')}Z"


def parse_iso_utc(raw: str | datetime | None) -> datetime | None:
    """ISO-8601 문자열(`Z`/오프셋/무오프셋) → naive UTC. 무오프셋은 UTC 로 해석한다."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return as_utc_naive(raw, assume=UTC)
    text = raw.strip()
    if not text:
        return None
    if text.endswith(("Z", "z")):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 datetime: {raw!r}") from exc
    return as_utc_naive(parsed, assume=UTC)


def kst_label(value: datetime | None) -> str:
    """naive UTC → `07-29 14:02` (충돌 버전 note 표시용)."""
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(KST).strftime("%m-%d %H:%M")

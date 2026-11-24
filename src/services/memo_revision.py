from __future__ import annotations

import hashlib
import json

try:
    from ..timeutil import iso_utc
except ImportError:  # pragma: no cover
    from timeutil import iso_utc


def memo_revision(memo: dict) -> str:
    """Return the opaque revision shared by normal memo writes and rollups."""
    canonical = json.dumps(
        {
            "content": memo["content"],
            "id": memo["id"],
            "updatedAtUtc": iso_utc(memo.get("updated_at_utc")),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

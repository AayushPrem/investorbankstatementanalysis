"""Turn analysis dataclasses / Pydantic models into JSON-safe payloads.

Decimal → str (exact; money is never turned into float here), date → ISO
string, Enum → value, NaN/inf → None (Postgres JSONB rejects them).
"""
from __future__ import annotations

import dataclasses
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel


def to_jsonable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, Decimal):
        return str(obj) if obj.is_finite() else None
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return to_jsonable(obj.model_dump())
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        # A field marked metadata={"omit_if_none": True} is left out while it is None,
        # so optional additions (e.g. Phase 6 churn_risk) don't change existing payloads.
        return {f.name: to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)
                if not (f.metadata.get("omit_if_none") and getattr(obj, f.name) is None)}
    if isinstance(obj, dict):
        return {str(to_jsonable(k)): to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        items = sorted(obj, key=repr) if isinstance(obj, (set, frozenset)) else obj
        return [to_jsonable(v) for v in items]
    return str(obj)

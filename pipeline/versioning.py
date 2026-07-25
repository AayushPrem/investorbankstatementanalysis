"""Versioning, persistence, and replay (Step 3.5).

Every pipeline run is tagged with the frozen schema version and the semver
of every agent involved. Results are persisted to .pipeline_state/ keyed by
run_id, with an index from (input document hash) -> run_ids so historical
runs for the same input are never overwritten when an agent is upgraded —
each version combination gets its own record.

replay() re-runs a past input's original file through the *current* code and
diff_results() shows exactly what changed between two runs.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_STATE_ROOT = Path(".pipeline_state")
_CHANGELOG_PATH = Path(__file__).resolve().parent.parent / "schema" / "CHANGELOG.md"

# Semver registry for every agent that can affect an AnalysisResult. Bump the
# entry when that agent's *behaviour* changes materially (prompt rewrite,
# threshold change, new detector) — not for pure refactors.
AGENT_VERSIONS: dict[str, str] = {
    "adapter.digital_pdf": "1.0.0",
    "adapter.csv": "1.0.0",
    "adapter.excel": "1.0.0",
    "adapter.scanned_pdf": "1.0.0",
    "adapter.image": "1.0.0",
    "normaliser": "1.0.0",
    "validator": "1.0.0",
    "categoriser": "2.0.0",  # bumped Sprint 2 Step 2.6 — prompt refinement for accuracy
    "related_party": "1.0.0",
    "customer_identity": "1.0.0",
    "financial_analyst": "1.0.0",
    "risk": "1.0.0",
    "customer_analytics": "1.0.0",
    "financial_health_alerts": "1.0.0",
    "compliance": "1.0.0",
    "reconciliation": "1.0.0",
}

JURISDICTION_MODULE_VERSION = "india-1.0.0"


def schema_version() -> str:
    """Read the frozen schema version tag (e.g. "v1") from schema/CHANGELOG.md."""
    text = _CHANGELOG_PATH.read_text(encoding="utf-8")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            return stripped.removeprefix("## ").split()[0]
    raise ValueError(f"No version heading found in {_CHANGELOG_PATH}")


def document_hash(input_bytes: bytes) -> str:
    """Stable content hash used to group all runs of the same input file."""
    return hashlib.sha256(input_bytes).hexdigest()[:16]


# ─────────────────────────────────────────────────────────────────────────────
# Version tag
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class VersionTag:
    run_id: str
    timestamp: str
    schema_version: str
    agent_versions: dict[str, str]
    jurisdiction_module_version: str

    @classmethod
    def current(cls) -> VersionTag:
        return cls(
            run_id=str(uuid.uuid4()),
            timestamp=datetime.now(UTC).isoformat(),
            schema_version=schema_version(),
            agent_versions=dict(AGENT_VERSIONS),
            jurisdiction_module_version=JURISDICTION_MODULE_VERSION,
        )

    def as_version_tuple(self) -> tuple[str, ...]:
        """An order-independent fingerprint of every code version involved,
        usable as (document_hash, version_tuple) storage/lookup key.
        """
        parts = [self.schema_version, self.jurisdiction_module_version]
        parts += [f"{k}={v}" for k, v in sorted(self.agent_versions.items())]
        return tuple(parts)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VersionTag:
        return cls(**data)


# ─────────────────────────────────────────────────────────────────────────────
# Stored run
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class StoredRun:
    version_tag: VersionTag
    input_path: str
    input_hash: str
    result_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version_tag": self.version_tag.to_dict(),
            "input_path": self.input_path,
            "input_hash": self.input_hash,
            "result_summary": self.result_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StoredRun:
        return cls(
            version_tag=VersionTag.from_dict(data["version_tag"]),
            input_path=data["input_path"],
            input_hash=data["input_hash"],
            result_summary=data["result_summary"],
        )


class ResultStore:
    """Persists run metadata to .pipeline_state/{run_id}/run.json.

    Keyed conceptually by (document_hash, version_tuple): re-running the same
    input under a new agent version produces a new run_id rather than
    overwriting the historical record, so nothing is ever silently lost.
    """

    def __init__(self, root: Path | str = _STATE_ROOT) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def save(
        self,
        input_path: str,
        input_bytes: bytes,
        result_summary: dict[str, Any],
        version_tag: VersionTag | None = None,
    ) -> StoredRun:
        tag = version_tag or VersionTag.current()
        stored = StoredRun(
            version_tag=tag,
            input_path=input_path,
            input_hash=document_hash(input_bytes),
            result_summary=result_summary,
        )
        run_dir = self._run_dir(tag.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "run.json").write_text(
            json.dumps(stored.to_dict(), indent=2, default=str), encoding="utf-8"
        )
        return stored

    def load(self, run_id: str) -> StoredRun:
        run_json = self._run_dir(run_id) / "run.json"
        if not run_json.exists():
            raise FileNotFoundError(f"No stored run for run_id={run_id!r}")
        return StoredRun.from_dict(json.loads(run_json.read_text(encoding="utf-8")))

    def list_runs_for_document(self, input_hash: str) -> list[StoredRun]:
        """All historical runs (across every agent-version combination) for
        the input whose content hashes to *input_hash*, oldest first.
        """
        runs: list[StoredRun] = []
        if not self.root.exists():
            return runs
        for run_dir in sorted(self.root.iterdir()):
            run_json = run_dir / "run.json"
            if not run_json.exists():
                continue
            stored = StoredRun.from_dict(json.loads(run_json.read_text(encoding="utf-8")))
            if stored.input_hash == input_hash:
                runs.append(stored)
        return sorted(runs, key=lambda r: r.version_tag.timestamp)


# ─────────────────────────────────────────────────────────────────────────────
# Replay
# ─────────────────────────────────────────────────────────────────────────────

def summarise_result(demo_result: Any) -> dict[str, Any]:
    """Reduce a full pipeline result to the flat set of investor-relevant
    numbers that are meaningful to diff across code versions. Full
    StatementDocument/report bytes are not stored here — they're
    reproducible from the original input file via replay().
    """
    m = demo_result.metrics
    rr = demo_result.risk_report
    ca = demo_result.customer_analytics
    return {
        "transaction_count": len(demo_result.doc.transactions),
        "total_revenue": str(m.total_revenue),
        "total_burn": str(m.total_burn),
        "runway_months": str(m.runway_months) if m.runway_months is not None else None,
        "risk_score": rr.composite_score,
        "risk_flag_count": len(rr.flags),
        "compliance_exception_count": len(demo_result.compliance_report.exceptions),
        "reconciliation_finding_count": len(demo_result.reconciliation_report.findings),
        "active_customers_last_month": (
            max(ca.monthly_active_customers.values()) if ca.monthly_active_customers else 0
        ),
        "churn_event_count": len(ca.churn_events),
    }


def replay(store: ResultStore, run_id: str) -> StoredRun:
    """Re-run the original input for *run_id* through the CURRENT pipeline
    code, tagged with today's agent versions, and persist it as a new run.
    """
    old = store.load(run_id)
    input_path = Path(old.input_path)
    if not input_path.exists():
        raise FileNotFoundError(
            f"Original input for run_id={run_id!r} no longer exists at {input_path}"
        )

    from demo.pipeline_runner import run_pipeline_on_bytes  # local import: heavy pipeline deps

    pdf_bytes = input_path.read_bytes()
    demo_result = run_pipeline_on_bytes(pdf_bytes, input_path.name)
    return store.save(str(input_path), pdf_bytes, summarise_result(demo_result))


# ─────────────────────────────────────────────────────────────────────────────
# Diff
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FieldDiff:
    field: str
    old_value: Any
    new_value: Any


@dataclass(frozen=True)
class ResultDiff:
    run_id_old: str
    run_id_new: str
    changed: list[FieldDiff]

    @property
    def has_changes(self) -> bool:
        return bool(self.changed)


def diff_results(old: StoredRun, new: StoredRun) -> ResultDiff:
    """Field-by-field diff of two runs' result summaries."""
    changed = []
    for key in sorted(set(old.result_summary) | set(new.result_summary)):
        old_value = old.result_summary.get(key)
        new_value = new.result_summary.get(key)
        if old_value != new_value:
            changed.append(FieldDiff(field=key, old_value=old_value, new_value=new_value))
    return ResultDiff(
        run_id_old=old.version_tag.run_id,
        run_id_new=new.version_tag.run_id,
        changed=changed,
    )

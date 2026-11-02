"""Local model registry: ``models/<name>/<version>/{model.joblib, metadata.json}``.

* ``metadata.json`` records git sha, corpus version + hash, splits hash, split,
  params, metrics, library versions, created_at and the sha256 of model.joblib.
* :func:`load` verifies that sha256 before unpickling. joblib uses pickle, so
  only artifacts written by this repo's own training script, inside the local
  registry directory, are ever loaded (docs/adr/0004-categoriser-model-registry.md).

Artifacts are gitignored; metadata.json files are small and committed.
"""
from __future__ import annotations

import hashlib
import json
import platform
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib

REPO_ROOT = Path(__file__).resolve().parent.parent
MODELS_DIR = REPO_ROOT / "models"
MODEL_FILE = "model.joblib"
META_FILE = "metadata.json"


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class LoadedModel:
    model: Any
    metadata: dict[str, Any]
    path: Path

    @property
    def version(self) -> str:
        return str(self.metadata["model_version"])


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True,
            text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def git_dirty() -> bool | None:
    """True if tracked files differ from HEAD (the sha alone doesn't pin the code)."""
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=10, check=True,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def library_versions() -> dict[str, str]:
    import numpy
    import sklearn
    return {
        "python": platform.python_version(),
        "sklearn": sklearn.__version__,
        "numpy": numpy.__version__,
        "joblib": joblib.__version__,
    }


def _safe_part(s: str) -> str:
    if not s or any(c in s for c in "/\\:") or s in {".", ".."}:
        raise RegistryError(f"invalid registry path component: {s!r}")
    return s


def model_dir(name: str, version: str, root: Path | None = None) -> Path:
    return (root or MODELS_DIR) / _safe_part(name) / _safe_part(version)


def save(
    model: Any, *, name: str, version: str, metadata: dict[str, Any], root: Path | None = None,
) -> Path:
    """Write model.joblib + metadata.json. Overwrites the same version."""
    d = model_dir(name, version, root)
    d.mkdir(parents=True, exist_ok=True)
    mpath = d / MODEL_FILE
    joblib.dump(model, mpath, compress=3)
    meta = {
        "name": name,
        "model_version": version,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "git_sha": git_sha(),
        "git_dirty": git_dirty(),
        "libraries": library_versions(),
        **metadata,
        "artifact_sha256": sha256_file(mpath),
        "artifact_bytes": mpath.stat().st_size,
    }
    (d / META_FILE).write_text(json.dumps(meta, indent=2, sort_keys=True, default=str) + "\n",
                               encoding="utf-8", newline="\n")
    return d


def read_metadata(name: str, version: str, root: Path | None = None) -> dict[str, Any]:
    p = model_dir(name, version, root) / META_FILE
    if not p.exists():
        raise RegistryError(f"no metadata for {name}/{version}")
    meta: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return meta


def load(name: str, version: str, root: Path | None = None) -> LoadedModel:
    """Load a registered model after checking its sha256 against metadata."""
    d = model_dir(name, version, root)
    meta = read_metadata(name, version, root)
    mpath = d / MODEL_FILE
    if not mpath.exists():
        raise RegistryError(f"artifact missing for {name}/{version}")
    actual = sha256_file(mpath)
    if actual != meta.get("artifact_sha256"):
        raise RegistryError(f"artifact sha256 mismatch for {name}/{version}; refusing to load")
    return LoadedModel(model=joblib.load(mpath), metadata=meta, path=d)


def exists(name: str, version: str, root: Path | None = None) -> bool:
    d = model_dir(name, version, root)
    return (d / MODEL_FILE).exists() and (d / META_FILE).exists()

"""Nonsecret, nonidentifying local provenance collection."""

from __future__ import annotations

import hashlib
import importlib.metadata
import platform
import subprocess
from pathlib import Path

from agent_runtime import __version__
from agent_runtime.domain import ContentDigests, Provenance


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def collect_provenance(root: Path, digests: ContentDigests) -> Provenance:
    commit, dirty = _git_state(root)
    lock_path = root / "uv.lock"
    lock_digest = sha256_file(lock_path) if lock_path.is_file() else sha256_bytes(b"")
    return Provenance(
        package_version=__version__,
        python_version=platform.python_version(),
        platform_system=platform.system(),
        machine_architecture=platform.machine(),
        langgraph_version=importlib.metadata.version("langgraph"),
        dependency_lock_sha256=lock_digest,
        source_commit=commit,
        git_dirty=dirty,
        task_sha256=digests.task_sha256,
        config_sha256=digests.config_sha256,
        fixture_sha256=digests.fixture_sha256,
    )


def _git_state(root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=root,
                check=True,
                capture_output=True,
                text=True,
                timeout=2,
            ).stdout.strip()
        )
        return commit, dirty
    except (OSError, subprocess.SubprocessError):
        return None, None

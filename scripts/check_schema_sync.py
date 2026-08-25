"""Fail when the checked-in artifact schema differs from Pydantic generation."""

from __future__ import annotations

import json
from pathlib import Path

from agent_runtime.artifacts import generated_schema


def main() -> int:
    path = Path("schemas/run-artifact-v0.1.0.json")
    expected = generated_schema()
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read checked-in schema: {error}") from error
    if observed != expected:
        raise SystemExit("checked-in artifact schema differs from RunArtifact.model_json_schema()")
    print("schema synchronized: schemas/run-artifact-v0.1.0.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

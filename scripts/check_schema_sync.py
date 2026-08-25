"""Fail when the checked-in artifact schema differs from Pydantic generation."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from agent_runtime.artifacts import generated_schema
from agent_runtime.versions import ARTIFACT_SCHEMA_VERSION


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="regenerate the current checked schema before checking it",
    )
    args = parser.parse_args(argv)
    path = Path(f"schemas/run-artifact-v{ARTIFACT_SCHEMA_VERSION}.json")
    expected = generated_schema()
    if args.write:
        path.write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    try:
        observed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"could not read checked-in schema: {error}") from error
    if observed != expected:
        raise SystemExit("checked-in artifact schema differs from RunArtifact.model_json_schema()")
    print(f"schema synchronized: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Generate a deterministic example artifact at an explicit or checked-in path."""

from __future__ import annotations

import argparse
import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from agent_runtime.runtime import execute_loaded, load_inputs


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        default="examples/artifacts/retail-return-v1.run.json",
    )
    args = parser.parse_args(argv)
    root = Path.cwd().resolve()
    loaded = load_inputs(
        Path("examples/tasks/retail-return-v1.json"),
        Path("examples/configs/deterministic-v1.json"),
        root=root,
    )
    artifact = asyncio.run(execute_loaded(loaded, output_path=Path(args.output)))
    print(
        json.dumps(
            {
                "output": args.output,
                "run_id": artifact.run_id,
                "semantic_fingerprint": artifact.semantic_fingerprint,
                "status": artifact.status.value,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

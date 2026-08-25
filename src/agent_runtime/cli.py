"""Command-line interface for deterministic runs and artifact validation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from agent_runtime.artifacts import read_artifact
from agent_runtime.domain import RunArtifact, RunStatus
from agent_runtime.errors import ClassifiedError, sanitize_message
from agent_runtime.runtime import execute_loaded, load_inputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="agent-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="execute one local agent run")
    run.add_argument("--task", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--output", required=True)
    validate = commands.add_parser("validate-artifact", help="validate one run artifact")
    validate.add_argument("artifact")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "run":
            return _run_command(Path(args.task), Path(args.config), Path(args.output))
        return _validate_command(Path(args.artifact))
    except KeyboardInterrupt:
        _print_json({"status": "interrupted", "code": "keyboard_interrupt"})
        return 130


def _run_command(task_path: Path, config_path: Path, output_path: Path) -> int:
    try:
        root = Path.cwd().resolve()
        loaded = load_inputs(task_path, config_path, root=root)
        artifact = asyncio.run(execute_loaded(loaded, output_path=output_path))
    except ClassifiedError as error:
        _print_json({"status": "error", "code": error.code, "message": sanitize_message(error)})
        return 1
    except Exception as error:
        _print_json(
            {
                "status": "error",
                "code": "unexpected_internal_error",
                "message": sanitize_message(error),
            }
        )
        return 1
    _print_json(_run_summary(artifact, output_path))
    return 0 if artifact.status == RunStatus.SUCCEEDED else 3


def _validate_command(path: Path) -> int:
    try:
        artifact = read_artifact(path)
    except ClassifiedError as error:
        _print_json({"valid": False, "code": error.code, "message": sanitize_message(error)})
        return 1
    except Exception as error:
        _print_json(
            {
                "valid": False,
                "code": "unexpected_internal_error",
                "message": sanitize_message(error),
            }
        )
        return 1
    _print_json(
        {
            "valid": True,
            "run_id": artifact.run_id,
            "schema_version": artifact.schema_version,
            "status": artifact.status.value,
            "content_sha256": artifact.content_sha256,
            "semantic_fingerprint": artifact.semantic_fingerprint,
        }
    )
    return 0


def _run_summary(artifact: RunArtifact, output_path: Path) -> dict[str, object]:
    try:
        locator = os.path.relpath(output_path.resolve(), Path.cwd().resolve())
    except ValueError:
        locator = str(output_path)
    return {
        "status": artifact.status.value,
        "run_id": artifact.run_id,
        "artifact": locator,
        "semantic_fingerprint": artifact.semantic_fingerprint,
        "accounting": {
            "logical_model_turns": artifact.accounting.logical_model_turns,
            "model_attempts": artifact.accounting.model_attempts,
            "logical_tool_calls": artifact.accounting.logical_tool_calls,
            "tool_attempts": artifact.accounting.tool_attempts,
            "external_model_calls": artifact.accounting.external_model_calls,
            "cost_usd": artifact.accounting.cost_usd,
        },
    }


def _print_json(value: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")

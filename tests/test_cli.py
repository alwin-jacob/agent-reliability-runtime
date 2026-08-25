from __future__ import annotations

import json
from pathlib import Path
from typing import Any, NoReturn, cast

import pytest
from conftest import make_loaded, success_script

from agent_runtime.cli import main


def _responses(script: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return cast(dict[str, list[dict[str, Any]]], script["responses"])


def test_cli_success_validation_and_usage_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_loaded(tmp_path)
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "run",
                "--task",
                "task.json",
                "--config",
                "config.json",
                "--output",
                "run.json",
            ]
        )
        == 0
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "succeeded"
    assert summary["accounting"]["model_attempts"] == 4
    assert main(["validate-artifact", "run.json"]) == 0
    validation = json.loads(capsys.readouterr().out)
    assert validation["valid"] is True
    with pytest.raises(SystemExit) as caught:
        main(["run"])
    assert caught.value.code == 2


def test_cli_normalized_runtime_failure_returns_three(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    script = success_script()
    _responses(script)["supervisor_plan"] = [
        {"kind": "permanent_failure", "code": "provider_disabled"}
    ]
    make_loaded(tmp_path, script=script)
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "run",
                "--task",
                "task.json",
                "--config",
                "config.json",
                "--output",
                "failed.json",
            ]
        )
        == 3
    )
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "failed"
    assert (tmp_path / "failed.json").is_file()


def test_cli_configuration_error_returns_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "run",
                "--task",
                "missing.json",
                "--config",
                "missing-config.json",
                "--output",
                "run.json",
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_cli_rejects_fixture_path_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    make_loaded(tmp_path)
    config_path = tmp_path / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["model_fixture"] = "../outside.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert (
        main(
            [
                "run",
                "--task",
                "task.json",
                "--config",
                "config.json",
                "--output",
                "run.json",
            ]
        )
        == 1
    )
    result = json.loads(capsys.readouterr().out)
    assert result["code"] == "path_escape"


def test_cli_normalizes_unexpected_validation_exception(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail(_: Path) -> NoReturn:
        raise KeyError("scripted validator defect")

    monkeypatch.setattr("agent_runtime.cli.read_artifact", fail)
    assert main(["validate-artifact", "run.json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is False
    assert result["code"] == "unexpected_internal_error"

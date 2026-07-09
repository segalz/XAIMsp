import inspect
import json
import subprocess
import tempfile
from pathlib import Path

import pytest

import server


def test_normalize_workspace_accepts_existing_dir(tmp_path: Path) -> None:
    assert server._normalize_workspace(str(tmp_path)) == str(tmp_path.resolve())


def test_normalize_workspace_rejects_missing_dir(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="workspace does not exist"):
        server._normalize_workspace(str(tmp_path / "missing"))


def test_last_json_object_tolerates_leading_logs() -> None:
    data = server._last_json_object('log line\n{"text": "hello"}\n')
    assert data == {"text": "hello"}


def test_last_json_object_prefers_extractable_text_over_nested_metadata() -> None:
    data = server._last_json_object('log\n{"text": "answer", "meta": {"tokens": 3}}')
    assert data == {"text": "answer", "meta": {"tokens": 3}}


def test_last_json_object_does_not_select_nested_text_object() -> None:
    data = server._last_json_object('{"content": "CORRECT", "meta": {"text": "WRONG"}}')
    assert server._extract_text_from_json(data or {}) == "CORRECT"


def test_last_json_object_prefers_first_text_object_over_later_status() -> None:
    data = server._last_json_object('{"response":"REAL"}\n{"message":"completed"}')
    assert server._extract_text_from_json(data or {}) == "REAL"


def test_last_json_object_skips_leading_log_message() -> None:
    data = server._last_json_object('{"level":"info","message":"starting"}\n{"text":"final"}')
    assert server._extract_text_from_json(data or {}) == "final"


def test_last_json_object_handles_top_level_array() -> None:
    data = server._last_json_object('[{"role":"assistant","content":"hello"}]')
    assert server._extract_text_from_json(data or {}) == "hello"


def test_last_json_object_returns_none_for_metadata_only_scan() -> None:
    assert server._last_json_object('final answer\n{"status":"ok","timestamp":"t"}') is None


def test_last_json_object_handles_message_only_array() -> None:
    data = server._last_json_object('[{"level":"info","message":"hello"}]')
    assert server._extract_text_from_json(data or {}) == "hello"


def test_extract_text_from_common_json_shapes() -> None:
    assert server._extract_text_from_json({"text": "hello"}) == "hello"
    assert server._extract_text_from_json({"result": {"content": "nested"}}) == "nested"
    assert server._extract_text_from_json({"result": "result text"}) == "result text"
    assert server._extract_text_from_json({"assistant": "assistant text"}) == "assistant text"
    assert server._extract_text_from_json({"content": [{"type": "text", "text": "part"}]}) == "part"
    assert (
        server._extract_text_from_json(
            {"messages": [{"role": "assistant", "content": [{"text": "chunk"}]}]}
        )
        == "chunk"
    )
    assert (
        server._extract_text_from_json(
            {"messages": [{"content": "user prompt"}, {"role": "assistant", "content": "reply"}]}
        )
        == "reply"
    )


def test_resolve_grok_command_uses_env_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    grok = tmp_path / "grok"
    grok.write_text("#!/bin/sh\n", encoding="utf-8")
    grok.chmod(0o755)
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, str(grok))

    assert server._resolve_grok_command() == str(grok)


def test_resolve_grok_command_uses_path_for_bare_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    local_grok = tmp_path / "grok"
    local_grok.write_text("not executable", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    path_grok = bin_dir / "grok"
    path_grok.write_text("#!/bin/sh\n", encoding="utf-8")
    path_grok.chmod(0o755)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, "grok")
    monkeypatch.setenv("PATH", str(bin_dir))

    assert server._resolve_grok_command() == str(path_grok)


def test_resolve_grok_command_rejects_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, str(tmp_path))

    with pytest.raises(RuntimeError, match="executable file"):
        server._resolve_grok_command()


def test_resolve_grok_command_rejects_non_executable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    grok = tmp_path / "grok"
    grok.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, str(grok))

    with pytest.raises(RuntimeError, match="executable file"):
        server._resolve_grok_command()


def test_resolve_grok_command_rejects_missing_env_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, "/missing/grok")

    with pytest.raises(RuntimeError, match="GROK_CLI_PATH"):
        server._resolve_grok_command()


def test_run_grok_builds_safe_default_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask("say ok", str(tmp_path), 10)

    assert result == "ok"
    assert seen["args"][:2] == ["grok", "--no-auto-update"]
    assert "--prompt-file" in seen["args"]
    assert "-p" not in seen["args"]
    assert "--always-approve" not in seen["args"]
    assert seen["args"][seen["args"].index("--model") + 1] == "grok-4.5"
    assert "--output-format" in seen["args"]
    assert seen["kwargs"]["cwd"] == str(tmp_path)
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["errors"] == "replace"


def test_mcp_tools_do_not_expose_always_approve() -> None:
    assert "always_approve" not in inspect.signature(server.grok_ask).parameters
    assert "always_approve" not in inspect.signature(server.grok_continue).parameters
    assert "always_approve" not in inspect.signature(server.grok_code_review).parameters


def test_timeout_has_upper_bound() -> None:
    with pytest.raises(ValueError, match="must not exceed"):
        server._coerce_timeout(server.MAX_TIMEOUT_S + 1)


def test_empty_resume_uses_continue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    server.grok_continue("say ok", str(tmp_path), 10, resume="")

    assert "--continue" in seen["args"]
    assert "--resume" not in seen["args"]


def test_code_review_uses_strict_review_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["prompt"] = Path(args[args.index("--prompt-file") + 1]).read_text(
            encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, stdout="finding", stderr="")

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_code_review(
        "def f(): pass",
        primary_analysis="No findings from CodeHelper.",
        workspace=str(tmp_path),
        timeout_s=10,
    )

    assert result == "finding"
    assert "--rules" not in seen["args"]
    assert "--disable-web-search" in seen["args"]
    assert seen["args"][seen["args"].index("--output-format") + 1] == "plain"
    assert "--permission-mode" not in seen["args"]
    prompt = seen["prompt"]
    assert "Do not inspect the workspace" in prompt
    assert "Primary analysis to challenge" in prompt
    assert "def f(): pass" in prompt


def test_code_review_validates_input(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="code_or_diff must not be empty"):
        server.grok_code_review("", workspace=str(tmp_path), timeout_s=10)
    with pytest.raises(ValueError, match="max_findings"):
        server.grok_code_review("x", workspace=str(tmp_path), timeout_s=10, max_findings=11)


def test_prompt_uses_prompt_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        prompt_file = Path(args[args.index("--prompt-file") + 1])
        assert prompt_file.exists()
        assert "short prompt" in prompt_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    assert server._run_grok("short prompt", str(tmp_path), 10)["text"] == "ok"
    assert "--prompt-file" in seen["args"]
    assert "-p" not in seen["args"]
    assert not Path(seen["args"][seen["args"].index("--prompt-file") + 1]).exists()


def test_long_prompt_validation_does_not_leave_prompt_file(tmp_path: Path) -> None:
    before = set(Path(tempfile.gettempdir()).glob("xai-mcp-prompt-*.md"))
    with pytest.raises(ValueError, match="timeout_s"):
        server._run_grok("x" * 10_000, str(tmp_path), 0)
    after = set(Path(tempfile.gettempdir()).glob("xai-mcp-prompt-*.md"))
    assert after == before


def test_json_without_text_raises_parse_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"session_id": "abc", "status": "ok"}), stderr=""
        )

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="without extractable response text"):
        server._run_grok("prompt", str(tmp_path), 10)


def test_code_review_trims_preamble(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="I will review now.- Severity: P2\n- Classification: design risk",
            stderr="",
        )

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_code_review("x", workspace=str(tmp_path), timeout_s=10)

    assert result.startswith("- Severity: P2")


def test_raw_output_returns_debug_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok"}), stderr="warn"
        )

    monkeypatch.setattr(server, "_resolve_grok_command", lambda: "grok")
    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10, raw_output=True)

    assert result["text"] == "ok"
    assert result["stderr"] == "warn"
    assert result["parsed"] == {"text": "ok"}

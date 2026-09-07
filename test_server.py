import inspect
import json
import subprocess
import time
import tempfile
from pathlib import Path

import pytest

import server


def host_prompt_path(args):
    value = args[args.index("--prompt-file") + 1]
    if value.startswith("/mnt/"):
        value = value[5].upper() + ":/" + value[7:]
    return Path(value)


@pytest.fixture(autouse=True)
def isolate_bridge_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(server.ENV_GROK_WSL_DISTRO, "Ubuntu")
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, "/home/segal/.grok/bin/grok")


@pytest.mark.parametrize("source,expected", [
    (r"C:\Develop\My Project", "/mnt/c/Develop/My Project"),
    ("D:/Temp/prompt.md", "/mnt/d/Temp/prompt.md"),
    ("/home/segal/project", "/home/segal/project"),
])
def test_to_wsl_path(source: str, expected: str) -> None:
    assert server._to_wsl_path(source) == expected


@pytest.mark.parametrize("configured", ["", "grok", "./grok", "~/bin/grok", r"C:\grok.exe"])
def test_wsl_requires_absolute_linux_executable(monkeypatch, configured) -> None:
    monkeypatch.setenv(server.ENV_GROK_WSL_DISTRO, "Ubuntu")
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, configured)
    with pytest.raises(RuntimeError, match="absolute Linux path"):
        server._grok_argv_prefix()


@pytest.mark.parametrize("fails", [False, True])
def test_wsl_run_converts_paths_and_cleans_prompt(monkeypatch, tmp_path, fails) -> None:
    monkeypatch.setenv(server.ENV_GROK_WSL_DISTRO, "Ubuntu")
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, "/home/segal/.grok/bin/grok")
    real_named_temp = tempfile.NamedTemporaryFile
    created = []

    def named_temp(*args, **kwargs):
        result = real_named_temp(*args, dir=tmp_path, **kwargs)
        created.append(Path(result.name))
        return result

    def fake_run(args, **kwargs):
        assert args[:5] == ["wsl.exe", "-d", "Ubuntu", "--", "/home/segal/.grok/bin/grok"]
        assert args[args.index("--cwd") + 1] == "/mnt/c/Develop/My Project"
        assert kwargs["cwd"] == r"C:\Develop\My Project"
        assert args[args.index("--prompt-file") + 1] == server._to_wsl_path(str(created[0]))
        assert created[0].read_text(encoding="utf-8") == "prompt text"
        if fails:
            raise subprocess.TimeoutExpired(args, 10)
        return subprocess.CompletedProcess(args, 0, stdout='{"text":"ok"}', stderr="")

    monkeypatch.setattr(server.tempfile, "NamedTemporaryFile", named_temp)
    monkeypatch.setattr(server.subprocess, "run", fake_run)
    if fails:
        with pytest.raises(RuntimeError, match="timed out"):
            server._run_grok("prompt text", r"C:\Develop\My Project", 10)
    else:
        assert server._run_grok("prompt text", r"C:\Develop\My Project", 10)["text"] == "ok"
    assert created and not created[0].exists()


def test_wsl_version_uses_same_prefix(monkeypatch) -> None:
    monkeypatch.setenv(server.ENV_GROK_WSL_DISTRO, "Ubuntu")
    monkeypatch.setenv(server.ENV_GROK_CLI_PATH, "/home/segal/.grok/bin/grok")

    def fake_run(args, **kwargs):
        assert args == [
            "wsl.exe", "-d", "Ubuntu", "--", "/home/segal/.grok/bin/grok", "--version"
        ]
        return subprocess.CompletedProcess(args, 0, stdout="1.0.13\n", stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    assert server.grok_version() == "1.0.13"


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


@pytest.mark.parametrize("distro", [None, "", "  "])
def test_missing_distro_never_launches_process(monkeypatch, distro):
    if distro is None:
        monkeypatch.delenv(server.ENV_GROK_WSL_DISTRO)
    else:
        monkeypatch.setenv(server.ENV_GROK_WSL_DISTRO, distro)
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: pytest.fail("launched"))
    with pytest.raises(RuntimeError, match="WSL only"):
        server.grok_version()
    with pytest.raises(RuntimeError, match="WSL only"):
        server._run_grok("prompt", "C:/Develop", 10)


def test_run_grok_builds_safe_default_command(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask("say ok", str(tmp_path), 10)

    assert result == "ok"
    assert seen["args"][:6] == [
        "wsl.exe", "-d", "Ubuntu", "--", "/home/segal/.grok/bin/grok", "--no-auto-update"
    ]
    assert "--prompt-file" in seen["args"]
    assert "-p" not in seen["args"]
    assert "--always-approve" not in seen["args"]
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "auto"
    assert seen["args"][seen["args"].index("--model") + 1] == "grok-4.6"
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

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    server.grok_continue("say ok", str(tmp_path), 10, resume="")

    assert "--continue" in seen["args"]
    assert "--resume" not in seen["args"]


def test_grok_ask_accept_edits_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask(
        "say ok",
        workspace=str(tmp_path),
        timeout_s=10,
        permission_mode="acceptEdits",
        raw_output=True,
    )

    idx = seen["args"].index("--permission-mode")
    assert seen["args"][idx + 1] == "auto"
    assert result["permission_mode"] == "acceptEdits"
    assert result["permission_mode_requested"] == "acceptEdits"
    assert result["permission_mode_effective"] == "auto"


def test_grok_ask_auto_permission_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask(
        "say ok",
        workspace=str(tmp_path),
        timeout_s=10,
        permission_mode="auto",
        raw_output=True,
    )

    idx = seen["args"].index("--permission-mode")
    assert seen["args"][idx + 1] == "auto"
    assert result["permission_mode"] == "auto"
    assert result["permission_mode_requested"] == "auto"
    assert result["permission_mode_effective"] == "auto"


def test_grok_ask_rejects_invalid_permission_mode(tmp_path: Path) -> None:
    for invalid_mode in ("bypassPermissions", "dontAsk", "default", "  "):
        with pytest.raises(ValueError, match="unsupported permission_mode"):
            server.grok_ask(
                "say ok",
                workspace=str(tmp_path),
                timeout_s=10,
                permission_mode=invalid_mode,
            )


def test_grok_continue_also_carries_the_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    server.grok_continue("say ok", str(tmp_path), 10)

    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "auto"


def test_code_review_uses_strict_review_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["prompt"] = host_prompt_path(args).read_text(
            encoding="utf-8"
        )
        return subprocess.CompletedProcess(args, 0, stdout="finding", stderr="")

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
    # JSON, so a review that stopped early carries a stopReason and can be
    # labelled. Plain output has none, and silence then reads as approval.
    assert seen["args"][seen["args"].index("--output-format") + 1] == "json"
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "auto"
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
        prompt_file = host_prompt_path(args)
        assert prompt_file.exists()
        assert "short prompt" in prompt_file.read_text(encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"text": "ok"}), stderr="")

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    assert server._run_grok("short prompt", str(tmp_path), 10)["text"] == "ok"
    assert "--prompt-file" in seen["args"]
    assert "-p" not in seen["args"]
    assert not host_prompt_path(seen["args"]).exists()


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

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_code_review("x", workspace=str(tmp_path), timeout_s=10)

    assert result.startswith("- Severity: P2")


def test_raw_output_returns_debug_payload(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok"}), stderr="warn"
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    result = server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10, raw_output=True)

    assert result["text"] == "ok"
    assert result["stderr"] == "warn"
    assert result["parsed"] == {"text": "ok"}


def _completed(payload: dict) -> object:
    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps(payload), stderr="")
    return fake_run


def test_self_cancelled_run_is_labelled_incomplete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The real shape of the defect: grok exits 0, having stopped itself well
    # inside the turn budget, leaving narration instead of an answer.
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "I'll read the files, then count them.",
        "stopReason": "cancelled",
        "num_turns": 4,
    }))

    answer = server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    assert answer.startswith("[INCOMPLETE:")
    assert "cancelled" in answer
    assert "after 4 turn(s)" in answer
    # The partial text is kept -- the caller is warned, not deprived.
    assert "I'll read the files, then count them." in answer


def test_self_cancelled_run_flags_raw_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "narration only",
        "stopReason": "cancelled",
        "num_turns": 4,
    }))

    result = server.grok_ask(
        "prompt", workspace=str(tmp_path), timeout_s=10, raw_output=True
    )

    assert result["incomplete"] is True
    assert result["stop_reason"] == "cancelled"


def test_completed_run_carries_no_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "the actual answer",
        "stopReason": "end_turn",
        "num_turns": 2,
    }))

    result = server.grok_ask(
        "prompt", workspace=str(tmp_path), timeout_s=10, raw_output=True
    )

    assert result["text"] == "the actual answer"
    assert "incomplete" not in result
    assert result["stop_reason"] == "end_turn"


def test_incomplete_code_review_keeps_its_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The preamble trim exists to drop narration before the first finding. On an
    # incomplete review the preamble is the warning, and trimming it would hand
    # back a tidy-looking findings list with nothing saying more were coming.
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "I'll check the inputs first.- Severity: P2\n- Classification: design risk",
        "stopReason": "cancelled",
        "num_turns": 1,
    }))

    result = server.grok_code_review("x", workspace=str(tmp_path), timeout_s=10)

    assert result.startswith("[INCOMPLETE:")
    assert "- Severity: P2" in result


def test_complete_code_review_still_trims_preamble(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "I will review now.- Severity: P2\n- Classification: design risk",
        "stopReason": "end_turn",
        "num_turns": 2,
    }))

    result = server.grok_code_review("x", workspace=str(tmp_path), timeout_s=10)

    assert result.startswith("- Severity: P2")


def test_timeout_reports_what_grok_had_written(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A bare "timed out after Ns" throws away the only evidence of how far it got.
    def fake_run(args, **kwargs):
        raise subprocess.TimeoutExpired(
            args, 10, output="partial work so far", stderr=b"a warning"
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError) as exc:
        server._run_grok("prompt", str(tmp_path), 10)

    message = str(exc.value)
    assert "timed out after 10s" in message
    assert "partial work so far" in message
    assert "a warning" in message


def test_cancelled_warning_names_the_likely_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # "cancelled" on its own is a mystery. In headless it almost always means a
    # tool needed approval and nobody was there, which the caller can act on.
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "I'll create the file.",
        "stopReason": "cancelled",
        "num_turns": 1,
    }))

    answer = server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    assert "needed approval" in answer
    assert "permission_mode" in answer


def test_turn_limit_warning_names_its_own_cause(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(server.subprocess, "run", _completed({
        "text": "partial",
        "stopReason": "max_turn_requests",
        "num_turns": 8,
    }))

    answer = server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    assert "raise max_turns" in answer
    assert "needed approval" not in answer


def test_every_call_carries_the_approval_grant(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Without it, headless grok cancels the moment a tool needs approval.
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "auto"


def test_an_unsupported_permission_mode_is_still_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unsupported permission_mode"):
        server.grok_ask(
            "prompt", workspace=str(tmp_path), timeout_s=10, permission_mode="yolo"
        )


def test_read_only_withholds_the_approval_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The gate, not the wording, is what stops a write.
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.grok_ask(
        "prompt", workspace=str(tmp_path), timeout_s=10, permission_mode="readOnly"
    )

    assert "--permission-mode" not in seen["args"]


def test_read_only_also_tells_grok_so(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # So it does not spend turns reaching for tools the gate will refuse.
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.grok_ask(
        "prompt", workspace=str(tmp_path), timeout_s=10, permission_mode="readOnly"
    )

    rules = seen["args"][seen["args"].index("--rules") + 1]
    assert "read-only analysis request" in rules


def test_read_only_keeps_the_caller_own_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.grok_ask(
        "prompt", workspace=str(tmp_path), timeout_s=10,
        permission_mode="readOnly", rules="Answer in one paragraph.",
    )

    rules = seen["args"][seen["args"].index("--rules") + 1]
    assert "read-only analysis request" in rules
    assert "Answer in one paragraph." in rules


def test_a_normal_call_carries_no_read_only_rules(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    assert "--rules" not in seen["args"]
    assert seen["args"][seen["args"].index("--permission-mode") + 1] == "auto"


def _seen_args(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = args
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    return seen


def test_a_turn_ceiling_is_always_sent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Otherwise the ceiling is whatever the CLI happens to default to that
    # release, which nothing here pins or notices changing.
    seen = _seen_args(monkeypatch)
    server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10)

    idx = seen["args"].index("--max-turns")
    assert seen["args"][idx + 1] == str(server.DEFAULT_MAX_TURNS)


def test_an_explicit_turn_budget_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _seen_args(monkeypatch)
    server.grok_ask("prompt", workspace=str(tmp_path), timeout_s=10, max_turns=200)

    assert seen["args"][seen["args"].index("--max-turns") + 1] == "200"


def test_the_default_ceiling_clears_real_work(monkeypatch: pytest.MonkeyPatch) -> None:
    # The heaviest task measured against this bridge finished in 5 turns. A
    # default that could cut off ordinary work would be worse than none.
    assert server.DEFAULT_MAX_TURNS >= 25


def test_calls_are_not_serialised(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # The bridge used to hold one mutex across every invocation, so a second
    # caller waited for the first even though grok sessions are independent
    # processes.
    import threading

    live = 0
    peak = 0
    guard = threading.Lock()
    entered = threading.Event()

    def fake_run(args, **kwargs):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        # Hold the slot long enough that a serialising bridge could not overlap.
        entered.set()
        time.sleep(0.15)
        with guard:
            live -= 1
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    threads = [
        threading.Thread(
            target=server.grok_ask, args=("prompt", str(tmp_path), 10)
        )
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert entered.is_set()
    assert peak > 1, f"calls still ran one at a time (peak concurrency {peak})"


def test_concurrency_is_bounded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Unbounded would let a caller start an arbitrary number of ~166MB processes.
    import threading

    live = 0
    peak = 0
    guard = threading.Lock()

    def fake_run(args, **kwargs):
        nonlocal live, peak
        with guard:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with guard:
            live -= 1
        return subprocess.CompletedProcess(
            args, 0, stdout=json.dumps({"text": "ok", "stopReason": "end_turn"}), stderr=""
        )

    monkeypatch.setattr(server.subprocess, "run", fake_run)

    threads = [
        threading.Thread(target=server.grok_ask, args=("prompt", str(tmp_path), 10))
        for _ in range(server.MAX_CONCURRENT_GROK + 4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert peak <= server.MAX_CONCURRENT_GROK
    # Asserting against the constant alone cannot notice the constant itself
    # being raised, so pin the range too: enough to be worth parallelising,
    # few enough that a burst does not start dozens of ~166MB processes.
    assert 2 <= server.MAX_CONCURRENT_GROK <= 8

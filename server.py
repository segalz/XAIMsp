"""x.ai Grok CLI bridge - FastMCP server.

Exposes the local `grok` CLI as MCP tools so an MCP host can ask Grok for a
second opinion inside a chosen workspace.

This bridge deliberately uses Grok's documented headless CLI surface instead
of reading private session files:

    grok --no-auto-update -p "prompt" --cwd /path --output-format json

Auth is handled by the Grok CLI itself. Run `grok login` first, or set the
environment expected by the CLI (for example XAI_API_KEY where supported).

Security: Grok is an agentic CLI. This bridge does not expose
`--always-approve` through MCP tools. The workspace is a working directory,
not a security boundary.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from fastmcp import FastMCP

mcp = FastMCP("xai")

log = logging.getLogger("xai_grok_bridge")
_GROK_LOCK = threading.Lock()

DEFAULT_TIMEOUT_S = 300
DEFAULT_MODEL = "grok-4.6"
MAX_TIMEOUT_S = 600
ENV_GROK_CLI_PATH = "GROK_CLI_PATH"
# Windows Smart App Control refuses to run the unsigned grok.exe, so the CLI can
# only be reached through a WSL distribution. Setting GROK_WSL_DISTRO to the
# distro name routes every invocation through `wsl.exe -d <distro> --`, and
# GROK_CLI_PATH is then read as a path inside that distro rather than on Windows.
ENV_GROK_WSL_DISTRO = "GROK_WSL_DISTRO"
_WINDOWS_DRIVE_PATH = re.compile(r"^([A-Za-z]):[\\/](.*)$")
# The only stopReason that means grok finished on its own terms. Anything else
# -- "cancelled" is the one seen in practice -- means the reply is partial.
_STOP_REASON_COMPLETE = "end_turn"
# xAI documents end.stopReason as one of end_turn, max_tokens,
# max_turn_requests, refusal, cancelled. "cancelled" fires when a turn ends
# early on interrupt, permission rejection, or the turn limit -- and in headless
# mode the usual one is permission: the agent reached for a tool that needs
# approval and there is nobody to give it. Verified by experiment: a task that
# must write a file cancels on turn 1 with no permission mode and completes with
# one, while read-only work (read_file, grep, git log) is never gated.
_STOP_REASON_CAUSES = {
    "cancelled": (
        " Usually this means grok needed approval for a tool -- writing a file, "
        "running a command that is not read-only -- and headless has nobody to "
        "approve it. Pass permission_mode, or keep the task read-only."
    ),
    "max_turn_requests": " It ran out of agentic turns; raise max_turns.",
    "max_tokens": " It hit the output token ceiling.",
    "refusal": " The model declined to answer.",
}
_PERMISSION_MODE_ALLOWED = frozenset({"acceptEdits", "auto", "readOnly"})
_PERMISSION_MODE_EFFECTIVE = "auto"
_PERMISSION_MODE_READ_ONLY = "readOnly"
# Sent with a read-only call so Grok does not spend turns reaching for tools the
# gate will refuse. It is a request, not the boundary -- withholding the approval
# flag is what actually stops a write. Both, because each does a different job.
READ_ONLY_RULES = (
    "This is a read-only analysis request. Do not create, edit, delete or move "
    "any file, and do not run commands that change anything. Reading, searching "
    "and inspecting are what you have. If the task appears to need a change, say "
    "what you would change and why, and stop there."
)
SECOND_REVIEW_RULES = (
    "You are a strict second code reviewer. Return findings only. "
    "Do not announce that you will review. Do not repeat primary-analysis findings "
    "unless you materially sharpen the trigger path or proof test. Prefer concrete "
    "runtime bugs, security risks, and regression risks over style feedback. "
    "If there are no high-confidence findings, say exactly: "
    "no additional high-confidence findings."
)


def _debug_enabled() -> bool:
    return os.environ.get("XAI_MCP_DEBUG", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[xai-grok-bridge] %(levelname)s: %(message)s"))
    log.handlers[:] = [handler]
    log.setLevel(logging.DEBUG if _debug_enabled() else logging.WARNING)
    log.propagate = False


def _normalize_workspace(workspace: Optional[str]) -> str:
    path = Path(workspace).expanduser() if workspace else Path.cwd()
    resolved = path.resolve()
    if not resolved.exists():
        raise ValueError(f"workspace does not exist: {resolved}")
    if not resolved.is_dir():
        raise ValueError(f"workspace is not a directory: {resolved}")
    return str(resolved)


def _coerce_timeout(timeout_s: int) -> int:
    if timeout_s < 1:
        raise ValueError("timeout_s must be at least 1")
    if timeout_s > MAX_TIMEOUT_S:
        raise ValueError(f"timeout_s must not exceed {MAX_TIMEOUT_S}")
    return timeout_s


def _normalize_permission_mode(permission_mode: Optional[str]) -> Optional[str]:
    if permission_mode is None:
        # Headless grok has nobody to approve a tool call, so anything needing a
        # write or a non-read-only command used to end the turn as stopReason
        # "cancelled" with narration in place of an answer. Approving by default
        # is what makes those runs finish. It is a real grant: grok can write
        # files and run commands in the workspace without asking, and the
        # workspace is a working directory, not a security boundary.
        return _PERMISSION_MODE_EFFECTIVE
    requested = permission_mode.strip()
    if requested not in _PERMISSION_MODE_ALLOWED:
        raise ValueError(
            "unsupported permission_mode; allowed values are 'acceptEdits', "
            "'auto' and 'readOnly'"
        )
    if requested == _PERMISSION_MODE_READ_ONLY:
        # Send no approval flag, so the CLI's own gate stands between Grok and
        # any change. A run that then reaches for a write ends as "cancelled",
        # which the caller now sees said plainly rather than silently.
        return None
    return _PERMISSION_MODE_EFFECTIVE


def _wsl_distro() -> str:
    """Require a WSL distro; native CLI execution is not supported."""
    distro = os.environ.get(ENV_GROK_WSL_DISTRO, "").strip()
    if not distro:
        raise RuntimeError(f"{ENV_GROK_WSL_DISTRO} is required; this bridge uses WSL only")
    return distro


def _to_wsl_path(value: str) -> str:
    """Translate a Windows path into the form the distro sees under /mnt.

    Anything that is not drive-qualified is passed through with separators
    normalised, so a path already written in Linux form survives untouched.
    """
    match = _WINDOWS_DRIVE_PATH.match(value)
    if match:
        drive, rest = match.groups()
        return f"/mnt/{drive.lower()}/{rest.replace(chr(92), '/')}"
    return value.replace("\\", "/")


def _grok_argv_prefix() -> list[str]:
    """Start the Linux CLI through WSL only."""
    distro = _wsl_distro()

    # WSL does not load login-shell PATH settings. Require an explicit Linux
    # path, but leave executable existence/permissions to the distro.
    configured = os.environ.get(ENV_GROK_CLI_PATH, "").strip()
    if not configured.startswith("/") or configured.startswith("//") or "\\" in configured:
        raise RuntimeError(
            f"{ENV_GROK_CLI_PATH} must be an absolute Linux path when "
            f"{ENV_GROK_WSL_DISTRO} is set (for example /home/segal/.grok/bin/grok)"
        )
    return ["wsl.exe", "-d", distro, "--", configured]


def _last_json_object(text: str) -> Optional[dict[str, Any]]:
    """Return the best JSON object found in text, tolerating leading logs.

    Prefer objects that contain extractable assistant text over trailing metadata
    objects or nested dictionaries discovered while scanning.
    """
    stripped = text.strip()
    if not stripped:
        return None

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            objects = [item for item in parsed if isinstance(item, dict)]
            return _best_json_object(objects)
        return None
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    idx = 0
    while idx < len(stripped):
        match = re.search(r"{", stripped[idx:])
        if not match:
            break
        start = idx + match.start()
        try:
            parsed, end = decoder.raw_decode(stripped[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue
        if isinstance(parsed, dict):
            objects.append(parsed)
        idx = start + end

    best = _best_json_object(objects)
    if best:
        return best
    return None


def _best_json_object(objects: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    scored = [
        (idx, _json_object_score(obj), obj)
        for idx, obj in enumerate(objects)
        if _extract_text_from_json(obj)
    ]
    if not scored:
        return None
    _, score, obj = max(scored, key=lambda item: (item[1], item[0]))
    return obj


def _json_object_score(obj: dict[str, Any]) -> int:
    # Prefer actual assistant payload shapes over log/status metadata. `message`
    # is intentionally low-confidence because logs often use that key.
    score = 0
    for key in ("text", "response", "output", "content", "assistant", "result", "messages"):
        if key in obj and _extract_content_text(obj.get(key)):
            score += 10
    if "message" in obj and _extract_content_text(obj.get("message")):
        score += 2
    if any(key in obj for key in ("level", "severity", "timestamp", "tokens", "status")):
        score -= 5
    return score


def _extract_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        nested = _extract_text_from_json(value)
        if nested:
            return nested
        text = value.get("text")
        return text.strip() if isinstance(text, str) else ""
    if isinstance(value, list):
        chunks = [_extract_content_text(item) for item in value]
        return "\n".join(chunk for chunk in chunks if chunk)
    return ""


def _extract_text_from_json(data: dict[str, Any]) -> str:
    """Extract assistant text from common Grok headless JSON shapes."""
    for key in ("text", "response", "output", "content", "result", "message"):
        text = _extract_content_text(data.get(key))
        if text:
            return text

    assistant = data.get("assistant")
    text = _extract_content_text(assistant)
    if text:
        return text

    messages = data.get("messages")
    if isinstance(messages, list):
        chunks: list[str] = []
        for item in messages:
            if not isinstance(item, dict):
                continue
            role = item.get("role")
            if role not in {"assistant", "model"}:
                continue
            content = item.get("content")
            text = _extract_content_text(content)
            if text:
                chunks.append(text)
        joined = "\n".join(chunk.strip() for chunk in chunks if chunk.strip())
        if joined:
            return joined

    return ""


def _run_grok(
    prompt: str,
    workspace: str,
    timeout_s: int,
    *,
    model: Optional[str] = None,
    session_id: Optional[str] = None,
    resume: Optional[str] = None,
    continue_session: bool = False,
    max_turns: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    rules: Optional[str] = None,
    disable_web_search: bool = False,
    permission_mode: Optional[str] = None,
    check: bool = False,
    output_format: str = "json",
) -> dict[str, Any]:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    timeout_s = _coerce_timeout(timeout_s)
    if max_turns is not None and max_turns < 1:
        raise ValueError("max_turns must be at least 1")
    effective_permission_mode = _normalize_permission_mode(permission_mode)

    # Tell Grok as well as the gate. The gate is what stops a write; saying so
    # in the rules keeps it from burning turns on attempts that will be refused.
    if (permission_mode or "").strip() == _PERMISSION_MODE_READ_ONLY:
        rules = f"{READ_ONLY_RULES}\n\n{rules}" if rules else READ_ONLY_RULES

    # Every path handed to the CLI has to be spelled the way the CLI's own
    # filesystem spells it. Under WSL that is /mnt/c/..., not C:\...
    for_cli = _to_wsl_path

    args = [
        *_grok_argv_prefix(),
        "--no-auto-update",
        "--cwd",
        for_cli(workspace),
        "--output-format",
        output_format,
        "--no-alt-screen",
    ]
    prompt_file: Optional[str] = None
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".md", prefix="xai-mcp-prompt-", delete=False
    ) as tmp:
        tmp.write(prompt)
        prompt_file = tmp.name
    args.extend(["--prompt-file", for_cli(prompt_file)])
    if model:
        args.extend(["--model", model])
    if session_id:
        args.extend(["--session-id", session_id])
    if resume:
        args.extend(["--resume", resume])
    if continue_session:
        args.append("--continue")
    if max_turns is not None:
        args.extend(["--max-turns", str(max_turns)])
    if reasoning_effort:
        args.extend(["--reasoning-effort", reasoning_effort])
    if rules:
        args.extend(["--rules", rules])
    if disable_web_search:
        args.append("--disable-web-search")
    if effective_permission_mode:
        args.extend(["--permission-mode", effective_permission_mode])
    if check:
        args.append("--check")

    try:
        with _GROK_LOCK:
            log.debug("running grok in %s with timeout=%ss", workspace, timeout_s)
            try:
                proc = subprocess.run(
                    args,
                    cwd=workspace,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=timeout_s,
                )
            except FileNotFoundError as exc:
                raise RuntimeError("wsl.exe not found; install WSL2") from exc
            except OSError as exc:
                raise RuntimeError(f"failed to run grok: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                # Whatever grok had written before the clock ran out is the only
                # evidence of how far it got. Discarding it turns a diagnosable
                # timeout into a bare sentence.
                raise RuntimeError(
                    f"grok timed out after {timeout_s}s\n"
                    f"partial stdout: {_decode_stream(exc.stdout)[-2000:]}\n"
                    f"partial stderr: {_decode_stream(exc.stderr)[-1000:]}"
                ) from exc
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                log.debug("failed to remove prompt file: %s", prompt_file)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    base_result: dict[str, Any] = {
        "stdout": stdout,
        "stderr": stderr,
        "returncode": proc.returncode,
        "model": model,
        "output_format": output_format,
        "permission_mode": permission_mode,
        "permission_mode_requested": permission_mode,
        "permission_mode_effective": effective_permission_mode,
    }
    if proc.returncode != 0:
        raise RuntimeError(
            f"grok exited {proc.returncode}\n"
            f"stderr: {stderr[-2000:]}\n"
            f"stdout: {stdout[-1000:]}"
        )

    if output_format == "plain":
        fallback = stdout.strip()
        if fallback:
            return {**base_result, "text": fallback}
        raise RuntimeError("grok completed without stdout text")

    data = _last_json_object(stdout)
    if data:
        text = _extract_text_from_json(data)
        if text:
            stop_reason = data.get("stopReason")
            result = {**base_result, "text": text, "parsed": data, "stop_reason": stop_reason}
            if stop_reason is not None and stop_reason != _STOP_REASON_COMPLETE:
                result["incomplete"] = True
                result["text"] = _incomplete_banner(stop_reason, data) + text
            return result
        raise RuntimeError("grok completed without extractable response text")

    # Keep the bridge useful if the CLI changes its JSON shape or falls back to
    # plain output despite the flag. Stderr is diagnostic noise on success.
    fallback = stdout.strip()
    if fallback:
        return {**base_result, "text": fallback}
    raise RuntimeError("grok completed without stdout text")


def _decode_stream(value: Any) -> str:
    """Best-effort text for a stream captured on a timeout.

    subprocess.run with text=True normally hands back str, but TimeoutExpired
    can carry bytes depending on where the timeout landed, and this runs while
    already reporting a failure -- it must not raise a second one.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _incomplete_banner(stop_reason: Any, data: dict[str, Any]) -> str:
    """Say plainly that the reply below is not a finished answer.

    grok's `text` accumulates narration across turns, so a run that ends before
    the work is done leaves narration alone -- "I'll read the files, then count
    them" -- with no result in it. Exhausting the turn budget exits non-zero and
    already raises, but the CLI can also stop itself, exiting 0 with
    stopReason "cancelled" and a turn count below the budget it was given. That
    reads as success, and a partial analysis that looks complete is worse than
    an error, because nothing prompts the caller to run it again.
    """
    turns = data.get("num_turns")
    turns_note = f" after {turns} turn(s)" if turns is not None else ""
    cause = _STOP_REASON_CAUSES.get(str(stop_reason), "")
    return (
        f"[INCOMPLETE: grok stopped with stopReason={stop_reason!r}{turns_note}, "
        "so what follows is whatever it had produced by then -- often narration "
        f"rather than a result. Do not treat it as a finished answer.{cause}]\n\n"
    )


def _result_payload(result: dict[str, Any], raw_output: bool) -> str | dict[str, Any]:
    if raw_output:
        return result
    return str(result["text"])


@mcp.tool()
def grok_ask(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: Optional[str] = DEFAULT_MODEL,
    session_id: Optional[str] = None,
    max_turns: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    rules: Optional[str] = None,
    permission_mode: Optional[str] = None,
    raw_output: bool = False,
) -> str | dict[str, Any]:
    """Ask Grok a prompt in a new headless CLI session.

    Args:
        prompt: The question or task for Grok.
        workspace: Working directory for Grok. Defaults to this server's cwd.
        timeout_s: Maximum seconds to wait. Default 300, capped at 600.
        model: Optional Grok model id passed to `--model`.
        session_id: Optional UUID for a new named headless session.
        max_turns: Optional limit for agent turns.
        reasoning_effort: Optional reasoning effort string passed through.
        rules: Optional run-scoped rules appended to Grok's system prompt.
        permission_mode: Opt-in 'acceptEdits' or 'auto' for headless edits;
            omit for the CLI default.
        raw_output: Return text plus raw stdout/stderr and parsed JSON when true.
    """
    ws = _normalize_workspace(workspace)
    return _result_payload(
        _run_grok(
            prompt,
            ws,
            timeout_s,
            model=model,
            session_id=session_id,
            max_turns=max_turns,
            reasoning_effort=reasoning_effort,
            rules=rules,
            permission_mode=permission_mode,
        ),
        raw_output,
    )


@mcp.tool()
def grok_continue(
    prompt: str,
    workspace: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: Optional[str] = DEFAULT_MODEL,
    resume: Optional[str] = None,
    max_turns: Optional[int] = None,
    reasoning_effort: Optional[str] = None,
    rules: Optional[str] = None,
    permission_mode: Optional[str] = None,
    raw_output: bool = False,
) -> str | dict[str, Any]:
    """Continue a Grok headless session.

    If `resume` is provided, resumes that session id. Otherwise passes
    `--continue`, which continues the most recent session in the workspace.
    """
    ws = _normalize_workspace(workspace)
    resume_id = resume.strip() if isinstance(resume, str) else resume
    return _result_payload(
        _run_grok(
            prompt,
            ws,
            timeout_s,
            model=model,
            resume=resume_id,
            continue_session=not resume_id,
            max_turns=max_turns,
            reasoning_effort=reasoning_effort,
            rules=rules,
            permission_mode=permission_mode,
        ),
        raw_output,
    )


@mcp.tool()
def grok_code_review(
    code_or_diff: str,
    question: str = "Find concrete correctness, security, and regression risks.",
    primary_analysis: Optional[str] = None,
    workspace: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
    model: Optional[str] = DEFAULT_MODEL,
    max_findings: int = 5,
    reasoning_effort: Optional[str] = "high",
    self_check: bool = False,
    raw_output: bool = False,
) -> str | dict[str, Any]:
    """Ask Grok for a strict second-opinion code review.

    This tool is intended to run after CodeHelper or manual analysis. It sends
    strict run-scoped review rules, disables web search, and passes large
    prompts via `--prompt-file`.

    Args:
        code_or_diff: Code, diff, or focused snippets to review.
        question: Review focus.
        primary_analysis: Optional CodeHelper/manual findings to challenge.
        workspace: Working directory for Grok. Defaults to this server's cwd.
        timeout_s: Maximum seconds to wait. Default 300, capped at 600.
        model: Optional Grok model id passed to `--model`.
        max_findings: Maximum findings to request. Must be 1-10.
        reasoning_effort: Optional reasoning effort string passed through.
        self_check: Pass `--check` for an extra verification loop. Costs more time/quota.
        raw_output: Return text plus raw stdout/stderr when true.
    """
    if not code_or_diff.strip():
        raise ValueError("code_or_diff must not be empty")
    if max_findings < 1 or max_findings > 10:
        raise ValueError("max_findings must be between 1 and 10")

    primary_section = (
        f"\nPrimary analysis to challenge:\n{primary_analysis.strip()}\n"
        if primary_analysis and primary_analysis.strip()
        else "\nPrimary analysis to challenge:\n(none provided)\n"
    )
    prompt = f"""You are doing an offline code review.
Do not inspect the workspace. Do not use tools. Analyze only the code/diff pasted below.
{SECOND_REVIEW_RULES}

Return ONLY findings, no preamble, no progress statement.

Review focus:
{question.strip()}
{primary_section}
For each finding use this exact format:
- Severity: P0/P1/P2/P3
- Classification: definite bug / likely bug / design risk / acceptable tradeoff
- Trigger path: exact function and parameter/runtime condition
- Why it matters: one concise paragraph
- Proof test: one focused test or runtime check

Find at most {max_findings} issues. If there are fewer real issues, return fewer.
Do not invent style issues.

Code or diff under review:
```text
{code_or_diff}
```"""
    ws = _normalize_workspace(workspace)
    result = _run_grok(
        prompt,
        ws,
        timeout_s,
        model=model,
        max_turns=None,
        reasoning_effort=reasoning_effort,
        disable_web_search=True,
        check=self_check,
        # JSON, not plain, so a review that stopped early is visible. Plain
        # output carries no stopReason, and a cancelled review then reads as
        # "no additional high-confidence findings" -- indistinguishable from a
        # clean one. Silence that looks like approval is the worst failure a
        # review tool can have.
        output_format="json",
    )
    text = str(result["text"])
    # Trimming the preamble is a convenience for a finished review. On an
    # incomplete one the preamble IS the warning, so leave it in place.
    if not result.get("incomplete"):
        first_finding = text.find("- Severity:")
        if first_finding > 0:
            result = {**result, "text": text[first_finding:].strip()}
    return _result_payload(result, raw_output)


@mcp.tool()
def grok_version() -> str:
    """Return the installed Grok CLI version."""
    try:
        proc = subprocess.run(
            [*_grok_argv_prefix(), "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("wsl.exe not found; install WSL2") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"grok --version failed: {(proc.stderr or proc.stdout).strip()}")
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


def main() -> None:
    _configure_logging()
    mcp.run()


if __name__ == "__main__":
    main()

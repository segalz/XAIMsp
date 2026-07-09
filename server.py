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
DEFAULT_MODEL = "grok-4.5"
MAX_TIMEOUT_S = 600
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
) -> str:
    if not prompt.strip():
        raise ValueError("prompt must not be empty")
    timeout_s = _coerce_timeout(timeout_s)
    if max_turns is not None and max_turns < 1:
        raise ValueError("max_turns must be at least 1")

    args = [
        "grok",
        "--no-auto-update",
        "--cwd",
        workspace,
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
    args.extend(["--prompt-file", prompt_file])
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
    if permission_mode:
        args.extend(["--permission-mode", permission_mode])
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
                raise RuntimeError("grok CLI not found on PATH") from exc
            except OSError as exc:
                raise RuntimeError(f"failed to run grok: {exc}") from exc
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"grok timed out after {timeout_s}s") from exc
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except OSError:
                log.debug("failed to remove prompt file: %s", prompt_file)

    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        raise RuntimeError(
            f"grok exited {proc.returncode}\n"
            f"stderr: {stderr[-2000:]}\n"
            f"stdout: {stdout[-1000:]}"
        )

    if output_format == "plain":
        fallback = stdout.strip()
        if fallback:
            return fallback
        raise RuntimeError("grok completed without stdout text")

    data = _last_json_object(stdout)
    if data:
        text = _extract_text_from_json(data)
        if text:
            return text
        raise RuntimeError("grok completed without extractable response text")

    # Keep the bridge useful if the CLI changes its JSON shape or falls back to
    # plain output despite the flag. Stderr is diagnostic noise on success.
    fallback = stdout.strip()
    if fallback:
        return fallback
    raise RuntimeError("grok completed without stdout text")


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
) -> str:
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
    """
    ws = _normalize_workspace(workspace)
    return _run_grok(
        prompt,
        ws,
        timeout_s,
        model=model,
        session_id=session_id,
        max_turns=max_turns,
        reasoning_effort=reasoning_effort,
        rules=rules,
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
) -> str:
    """Continue a Grok headless session.

    If `resume` is provided, resumes that session id. Otherwise passes
    `--continue`, which continues the most recent session in the workspace.
    """
    ws = _normalize_workspace(workspace)
    resume_id = resume.strip() if isinstance(resume, str) else resume
    return _run_grok(
        prompt,
        ws,
        timeout_s,
        model=model,
        resume=resume_id,
        continue_session=not resume_id,
        max_turns=max_turns,
        reasoning_effort=reasoning_effort,
        rules=rules,
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
) -> str:
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
        output_format="plain",
    )
    first_finding = result.find("- Severity:")
    if first_finding > 0:
        return result[first_finding:].strip()
    return result


@mcp.tool()
def grok_version() -> str:
    """Return the installed Grok CLI version."""
    try:
        proc = subprocess.run(
            ["grok", "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("grok CLI not found on PATH") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"grok --version failed: {(proc.stderr or proc.stdout).strip()}")
    return ((proc.stdout or "") + (proc.stderr or "")).strip()


if __name__ == "__main__":
    _configure_logging()
    mcp.run()

# x.ai Grok CLI MCP Bridge

Use the local `grok` CLI as an MCP sub-agent from Claude Code or any MCP host.

This server wraps the documented x.ai headless scripting mode. The generic tools use JSON output,
while `grok_code_review` uses plain output because it produced better review results in practice.

```bash
grok --no-auto-update --prompt-file /tmp/prompt.md --cwd /path/to/project --output-format json
```

## Requirements

- Windows host with Python 3.10+ and WSL2
- Grok installed and authenticated inside the selected Linux distro
- Required `GROK_WSL_DISTRO` (for example `Ubuntu`)
- Required `GROK_CLI_PATH`: absolute Linux executable path inside that distro

The bridge uses WSL only. Native Windows Grok execution and native PATH lookup have been removed.
Missing WSL configuration fails before any CLI process is started.

### Windows with WSL2

xAI ships `grok.exe` unsigned. Windows Smart App Control starts only signed, reputable binaries, so
on a machine where it is enabled every invocation dies with `[WinError 4551] An Application Control
policy has blocked this file`. The CLI is not broken; Windows refuses to launch it. Smart App
Control has no per-app allowlist, and turning it off cannot be undone without reinstalling Windows,
so run the Linux CLI inside WSL2 instead and keep the protection on.

Install and authenticate Grok inside the selected distro. Windows CLI login state is not used by
this route.

```powershell
wsl --install -d Ubuntu
wsl -d Ubuntu -- bash -lc "curl -fsSL https://x.ai/cli/install.sh | bash"
wsl -d Ubuntu -- bash -lc "~/.grok/bin/grok login"
```

Example MCP host configuration for this checkout:

```json
{
  "mcpServers": {
    "xai": {
      "command": "C:\\Develop\\XAIMsp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Develop\\XAIMsp\\server.py"],
      "env": {
        "GROK_WSL_DISTRO": "Ubuntu",
        "GROK_CLI_PATH": "/home/segal/.grok/bin/grok"
      }
    }
  }
}
```

Adjust the checkout, distro, and Linux user paths for your installation. With
`GROK_WSL_DISTRO` set, every call (including `grok_version`) uses
`wsl.exe -d <distro> -- <absolute Linux executable>`. `GROK_CLI_PATH` is required
and must be an absolute Linux path: WSL does not load login-shell PATH settings.
Windows-side executable validation is skipped; WSL reports missing or non-executable binaries.
The distro setting is required; there is no fallback to Windows `grok.exe`.

Pass `workspace` as a host path such as `C:\Develop\XAIMsp`. The bridge converts
drive paths for `--cwd` and its temporary `--prompt-file` to `/mnt/c/...`, assuming
standard WSL drive mounts. Custom mount roots and UNC workspaces are not supported
by this conversion. Prompt files are removed on the host after the call, including failures.

Restart the MCP server after changing its configuration or code. Check `grok_version`
first, then make a small `grok_ask` call to verify authentication and model access.
A version check alone does not verify a model response.

## Install

```powershell
cd C:\Develop\XAIMsp
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Use the MCP host configuration in [Windows with WSL2](#windows-with-wsl2).
The Python MCP server runs on Windows; all Grok invocations run inside WSL.

## Tools

- `grok_ask(prompt, workspace?, timeout_s?, model?, session_id?, max_turns?, reasoning_effort?, rules?, permission_mode?, raw_output?)`
- `grok_continue(prompt, workspace?, timeout_s?, model?, resume?, max_turns?, reasoning_effort?, rules?, permission_mode?, raw_output?)`
- `grok_code_review(code_or_diff, question?, primary_analysis?, workspace?, timeout_s?, model?, max_findings?, reasoning_effort?, self_check?, raw_output?)`
- `grok_version()`

`workspace` defaults to the MCP server's current directory. Pass the project path explicitly when
you want Grok to inspect a specific repo.

Use `grok_code_review` as a second-opinion reviewer after the primary or manual analysis. It embeds
strict offline-review rules in the prompt, disables web search, and uses `--prompt-file`.

Advanced parameters:

- `model`: Defaults to `grok-4.6` for ask, continue, and review. An explicit model overrides it.
- `permission_mode`: Ask/continue accept `acceptEdits`, `auto` or `readOnly`.
  `acceptEdits` and `auto` both send `--permission-mode auto`, and that is also what
  a call sends when it passes nothing. `readOnly` sends no approval flag, so the
  CLI's own gate stands between Grok and any change, and it appends a rule telling
  Grok as much so it does not spend turns reaching for tools that will be refused.
  The gate is the boundary; the rule only saves effort. Other values are rejected.
  Raw output includes the requested and effective modes.

  Approval is granted by default because headless grok has nobody to ask. Without
  it, the first tool call needing a write or a non-read-only command ends the turn
  as `stopReason: cancelled`, returning narration instead of an answer. The grant
  is real: Grok can write files and run commands in the workspace without asking,
  and the workspace is a working directory, not a security boundary. Point it at
  a repository whose changes you can see and revert.

- `max_turns`: Defaults to 50. Far above ordinary work, low enough that a stuck
  loop stops spending quota. Pass a larger value for a genuinely long task.
- `self_check=true`: Passes `--check` for an extra Grok verification loop. Use sparingly because it
  costs more time and quota.
- `raw_output=true`: Returns a debug payload with extracted text, stdout, stderr, return code, and
  parsed JSON when available.
- `session_id` and `resume`: Useful for explicit Grok session control. `session_id` should be a
  valid UUID for new sessions.
- `rules`: Available on generic ask/continue calls. Prefer `grok_code_review` for second-opinion
  code review because it already uses the tuned offline-review prompt.

Set `XAI_MCP_DEBUG=true` only when diagnosing bridge startup or CLI invocation issues.

See [CLAUDE_CODE_USAGE.md](CLAUDE_CODE_USAGE.md) for the recommended Claude Code workflow.

## Security

Grok is an agentic CLI. `workspace` is a working directory, not a security boundary. The bridge
does not expose `--always-approve` through MCP tools. Use `grok_code_review` with focused snippets
or diffs, and verify findings before editing code.

## Smoke Test

This makes a real Grok call and may use quota:

```bash
python test_smoke.py
```

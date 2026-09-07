# x.ai Grok CLI MCP Bridge

Use the local `grok` CLI as an MCP sub-agent from Claude Code or any MCP host.

This server wraps the documented x.ai headless scripting mode. The generic tools use JSON output,
while `grok_code_review` uses plain output because it produced better review results in practice.

```bash
grok --no-auto-update --prompt-file /tmp/prompt.md --cwd /path/to/project --output-format json
```

## Requirements

- Python 3.10+
- `grok` on `PATH`
- Auth already configured with `grok login`, or an environment supported by the CLI such as `XAI_API_KEY`
- Optional: `GROK_CLI_PATH` if `grok` is not on `PATH`
- On Windows with Smart App Control enabled: a WSL distribution, see below

Without WSL routing, `GROK_CLI_PATH` is strict: bare commands resolve through `PATH`, while path values must point to an
executable file. Directories and non-executable files are rejected.

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
Without the distro setting, the normal native invocation remains in use.

Pass `workspace` as a host path such as `C:\Develop\XAIMsp`. The bridge converts
drive paths for `--cwd` and its temporary `--prompt-file` to `/mnt/c/...`, assuming
standard WSL drive mounts. Custom mount roots and UNC workspaces are not supported
by this conversion. Prompt files are removed on the host after the call, including failures.

Restart the MCP server after changing its configuration or code. Check `grok_version`
first, then make a small `grok_ask` call to verify authentication and model access.
A version check alone does not verify a model response.

## Install

```bash
cd /Users/zvisegal/devlope/XAIMsp
/opt/homebrew/bin/python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
pytest -q -p no:cacheprovider
ruff check --no-cache .
```

## MCP Host Config

Add this server to the MCP host config. Prefer the project venv Python:

```json
{
  "mcpServers": {
    "xai": {
      "command": "/Users/zvisegal/devlope/XAIMsp/.venv/bin/python",
      "args": ["/Users/zvisegal/devlope/XAIMsp/server.py"]
    }
  }
}
```

On Windows the CLI has to be reached through WSL; see
[Windows with WSL2](#windows-with-wsl2) for the config that route needs.

## Tools

- `grok_ask(prompt, workspace?, timeout_s?, model?, session_id?, max_turns?, reasoning_effort?, rules?, permission_mode?, raw_output?)`
- `grok_continue(prompt, workspace?, timeout_s?, model?, resume?, max_turns?, reasoning_effort?, rules?, permission_mode?, raw_output?)`
- `grok_code_review(code_or_diff, question?, primary_analysis?, workspace?, timeout_s?, model?, max_findings?, reasoning_effort?, self_check?, raw_output?)`
- `grok_version()`

`workspace` defaults to the MCP server's current directory. Pass the project path explicitly when
you want Grok to inspect a specific repo.

Use `grok_code_review` as a second-opinion reviewer after CodeHelper or manual analysis. It embeds
strict offline-review rules in the prompt, disables web search, and uses `--prompt-file`.

Advanced parameters:

- `model`: Defaults to `grok-4.6` for ask, continue, and review. An explicit model overrides it.
- `permission_mode`: Ask/continue accept `acceptEdits` or `auto`; both send
  `--permission-mode auto`. Omission sends no permission flag and preserves the CLI default.
  Other values are rejected. This is an explicit opt-in to agent edits, not a workspace sandbox.
  Raw output includes the requested and effective modes. Code review exposes no edit-mode option.

- `self_check=true`: Passes `--check` for an extra Grok verification loop. Use sparingly because it
  costs more time and quota.
- `raw_output=true`: Returns a debug payload with extracted text, stdout, stderr, return code, and
  parsed JSON when available.
- `session_id` and `resume`: Useful for explicit Grok session control. `session_id` should be a
  valid UUID for new sessions.
- `rules`: Available on generic ask/continue calls. Prefer `grok_code_review` for second-opinion
  code review because it already uses the tuned offline-review prompt.

Set `XAI_MCP_DEBUG=true` only when diagnosing bridge startup or CLI invocation issues.

See [CLAUDE_CODE_USAGE.md](CLAUDE_CODE_USAGE.md) for the recommended Claude Code workflow and
[CLAUDE_CODE_UPDATE_GROK_PATH.md](CLAUDE_CODE_UPDATE_GROK_PATH.md) for the latest path-handling
update.

## Security

Grok is an agentic CLI. `workspace` is a working directory, not a security boundary. The bridge
does not expose `--always-approve` through MCP tools. Use `grok_code_review` with focused snippets
or diffs, and verify findings before editing code.

## Smoke Test

This makes a real Grok call and may use quota:

```bash
python test_smoke.py
```

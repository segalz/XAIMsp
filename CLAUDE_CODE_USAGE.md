# Claude Code Usage Notes

This project exposes the local x.ai Grok CLI as an MCP server.

Project path:

```text
C:\Develop\XAIMsp
```

## Setup

The bridge runs the Linux Grok CLI inside WSL; there is no native Windows path.
Both environment variables are required, and a missing one fails before any
process starts. See [README.md](README.md) for why.

```json
{
  "mcpServers": {
    "xai": {
      "command": "C:\\Develop\\XAIMsp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Develop\\XAIMsp\\server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "GROK_WSL_DISTRO": "Ubuntu",
        "GROK_CLI_PATH": "/home/segal/.grok/bin/grok"
      }
    }
  }
}
```

`GROK_CLI_PATH` names a path *inside* the distro and must be absolute:
`wsl.exe -- <cmd>` runs no login shell, so a `PATH` entry the installer wrote
into `.bashrc` has not been applied.

If the venv is missing:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Verify:

```powershell
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
.venv\Scripts\python.exe -c "import server; print(server.grok_version())"
```

A version check only proves the CLI starts. Follow it with a small `grok_ask`
to prove authentication and model access.

## Auth

Auth lives inside the distro, not on Windows. A Windows `grok login` does not
count for this route.

```powershell
wsl -d Ubuntu -- bash -lc "~/.grok/bin/grok models"
```

If needed:

```powershell
wsl -d Ubuntu -- bash -lc "~/.grok/bin/grok login"
```

Failures can still happen when logged in, especially `402 Payment Required` or `429 Too Many Requests`.

## Workflow

Use this policy:

```text
Primary analysis first.
Grok second for risky/complex changes.
```

Do the repo navigation, file discovery, flow analysis and ordinary code
questions with whatever your primary analysis backend is.

Use Grok only as a second reviewer for:

- risky save/auth/session/API changes
- shared components
- parser/subprocess/security-sensitive code
- regression-sensitive diffs
- “what did we miss?” checks after the primary pass

Prefer the `grok_code_review` MCP tool. Send it focused snippets or diffs plus a concise summary of the primary analysis. Do not use Grok as the primary code search tool.

Good prompt shape:

```text
Primary analysis found:
...

Review this focused snippet/diff as a second reviewer.
Find only concrete missed bugs.
For each finding include severity, trigger path, why it matters, and a proof test.
```

## Notes

`grok_code_review` is optimized for offline pasted-code review:

- Uses `grok-4.6` by default.
- Disables web search.
- Passes prompts through `--prompt-file`.
- Asks Grok not to inspect the workspace or use tools.
- Returns findings-only style output when Grok follows the prompt.
- Supports `raw_output=true` for debugging stdout/stderr/parser behavior.

Still verify Grok findings against real files and tests before editing.

## Debugging

Enable verbose debug logging by setting `XAI_MCP_DEBUG=true` in the MCP environment config:

```json
{
  "mcpServers": {
    "xai": {
      "command": "C:\\Develop\\XAIMsp\\.venv\\Scripts\\python.exe",
      "args": ["C:\\Develop\\XAIMsp\\server.py"],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "GROK_WSL_DISTRO": "Ubuntu",
        "GROK_CLI_PATH": "/home/segal/.grok/bin/grok",
        "XAI_MCP_DEBUG": "true"
      }
    }
  }
}
```

This enables bridge diagnostics on stderr. Keep it off by default because MCP stdio servers should
stay quiet unless you are actively debugging startup or CLI invocation issues.

## Advanced Tool Parameters

### 1. Verification Loops (`self_check`)
In `grok_code_review`, `self_check=true` passes `--check` to the Grok CLI. Use it sparingly for
high-risk reviews only; it costs more time and quota, and it does not replace local verification.

### 2. Session Management & Continuation
- **Starting a Named Session**: `grok_ask` can receive `session_id`, but the Grok CLI expects a
  valid UUID for a new session.
- **Resuming a Session**: `grok_continue` can receive `resume` to pass a specific session id to
  `--resume`.
- **Continuing the Last Session**: If `resume` is omitted, `grok_continue` passes `--continue`,
  which means the most recent Grok session for that workspace. Prefer an explicit `resume` id when
  exact conversation continuity matters.

### 3. Reasoning & Effort Control
You can pass `reasoning_effort` to `grok_ask`, `grok_continue`, and `grok_code_review`. The default
for code reviews is `"high"`. Supported values depend on the installed Grok CLI/model.

### 4. Custom Rules
`grok_ask` and `grok_continue` support `rules` for run-scoped custom instructions. For code review,
prefer `grok_code_review`; it already embeds the strict offline-review prompt that worked best in
testing.

### 5. Raw Output
Set `raw_output=true` to receive a detailed dictionary rather than just the final text response. Use
this for debugging parser or CLI behavior, not as the normal workflow. The dictionary contains:

- `text`: The extracted assistant response.
- `stdout`: The raw stdout from the CLI.
- `stderr`: The raw stderr from the CLI (useful for diagnosing warnings or authentication issue details).
- `returncode`: The subprocess exit code.
- `parsed`: The parsed JSON payload object (if JSON output format was used).

# Claude Code Usage Notes

This project exposes the local x.ai Grok CLI as an MCP server.

Project path:

```text
/Users/zvisegal/devlope/XAIMsp
```

## Setup

Prefer the project venv Python in Claude Code MCP config:

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

If the venv is missing:

```bash
cd /Users/zvisegal/devlope/XAIMsp
/opt/homebrew/bin/python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

If Claude Code cannot find `grok`, set `GROK_CLI_PATH` in the MCP server environment:

```json
{
  "mcpServers": {
    "xai": {
      "command": "/Users/zvisegal/devlope/XAIMsp/.venv/bin/python",
      "args": ["/Users/zvisegal/devlope/XAIMsp/server.py"],
      "env": {
        "GROK_CLI_PATH": "/Users/zvisegal/.local/bin/grok"
      }
    }
  }
}
```

`GROK_CLI_PATH` is validated strictly:

- Leave it unset if `grok` is already on `PATH`.
- A bare command such as `grok` resolves through `PATH`.
- A path value must point to an executable file.
- Directories and non-executable files are rejected with a clear config error.
- Avoid relative local files such as `./grok` unless that is the intended executable.

Verify:

```bash
cd /Users/zvisegal/devlope/XAIMsp
. .venv/bin/activate
pytest -q -p no:cacheprovider
ruff check --no-cache .
python - <<'PY'
import server
print(server.grok_version())
PY
```

## Auth

The bridge uses the local `grok` CLI login.

```bash
grok models
```

If needed:

```bash
grok login
```

Failures can still happen when logged in, especially `402 Payment Required` or `429 Too Many Requests`.

## Workflow

Use this policy:

```text
CodeHelper first.
Grok second for risky/complex changes.
```

Use CodeHelper for repo navigation, file discovery, flow analysis, and normal code questions.

Use Grok only as a second reviewer for:

- risky save/auth/session/API changes
- shared components
- parser/subprocess/security-sensitive code
- regression-sensitive diffs
- “what did we miss?” checks after CodeHelper

Prefer the `grok_code_review` MCP tool. Send it focused snippets or diffs plus a concise CodeHelper summary. Do not use Grok as the primary code search tool.

Good prompt shape:

```text
CodeHelper found:
...

Review this focused snippet/diff as a second reviewer.
Find only concrete missed bugs.
For each finding include severity, trigger path, why it matters, and a proof test.
```

## Notes

`grok_code_review` is optimized for offline pasted-code review:

- Uses `grok-4.5` by default.
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
      "command": "/Users/zvisegal/devlope/XAIMsp/.venv/bin/python",
      "args": ["/Users/zvisegal/devlope/XAIMsp/server.py"],
      "env": {
        "GROK_CLI_PATH": "/Users/zvisegal/.local/bin/grok",
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

See [CLAUDE_CODE_UPDATE_GROK_PATH.md](CLAUDE_CODE_UPDATE_GROK_PATH.md) for the latest short update.

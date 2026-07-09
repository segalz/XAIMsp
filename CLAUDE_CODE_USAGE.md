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

- uses `grok-4.5` by default
- disables web search
- passes prompts through `--prompt-file`
- asks Grok not to inspect the workspace or use tools
- returns findings-only style output when Grok follows the prompt
- supports `raw_output=true` for debugging stdout/stderr/parser behavior

Still verify Grok findings against real files and tests before editing.

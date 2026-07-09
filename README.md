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

`GROK_CLI_PATH` is strict: bare commands resolve through `PATH`, while path values must point to an
executable file. Directories and non-executable files are rejected.

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

## Tools

- `grok_ask(prompt, workspace?, timeout_s?, model?, session_id?, max_turns?, reasoning_effort?, rules?, raw_output?)`
- `grok_continue(prompt, workspace?, timeout_s?, model?, resume?, max_turns?, reasoning_effort?, rules?, raw_output?)`
- `grok_code_review(code_or_diff, question?, primary_analysis?, workspace?, timeout_s?, model?, max_findings?, reasoning_effort?, self_check?, raw_output?)`
- `grok_version()`

`workspace` defaults to the MCP server's current directory. Pass the project path explicitly when
you want Grok to inspect a specific repo.

Use `grok_code_review` as a second-opinion reviewer after CodeHelper or manual analysis. It embeds
strict offline-review rules in the prompt, disables web search, and uses `--prompt-file`.

Advanced parameters:

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

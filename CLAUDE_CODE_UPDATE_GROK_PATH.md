# Claude Code Update: Grok CLI Path Handling

The x.ai MCP bridge now has stricter `GROK_CLI_PATH` handling.

Use the bridge from:

```text
/Users/zvisegal/devlope/XAIMsp
```

Recommended Claude Code MCP config:

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

If `grok` is already on `PATH`, `GROK_CLI_PATH` can be omitted.

Validation rules:

- `GROK_CLI_PATH=grok` resolves through `PATH`.
- `GROK_CLI_PATH=/some/path/grok` must point to an executable file.
- Directories are rejected.
- Non-executable files are rejected.
- A local `./grok` file no longer shadows the real `PATH` binary when `GROK_CLI_PATH=grok`.

Use policy:

```text
CodeHelper first.
Grok second for risky/complex changes.
```

Use `grok_code_review` for second-opinion review, preferably with focused snippets or diffs plus a concise CodeHelper summary.

Verify locally:

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

Latest pushed commit for this update:

```text
c6b610f Harden Grok CLI path resolution
```

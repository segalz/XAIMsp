"""Real Grok CLI smoke test.

This requires local Grok authentication and may consume quota.
"""

from pathlib import Path

import server


def main() -> None:
    workspace = str(Path(__file__).resolve().parent)
    version = server.grok_version()
    print(f"grok version: {version}")
    response = server.grok_ask(
        "Reply with exactly: XAI_MCP_OK",
        workspace=workspace,
        timeout_s=120,
        max_turns=1,
    )
    print(response)
    if "XAI_MCP_OK" not in response:
        raise SystemExit("smoke test failed: marker not found")
    print("PASS")


if __name__ == "__main__":
    main()

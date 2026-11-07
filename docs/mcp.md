# Optional MCP integration

For a host that supports local stdio MCP servers, install the pinned SDK runtime:

```sh
python3 install.py --runtime-only
.venv/bin/python -m unittest discover -s tests -v
```

Register the absolute `rpg-mcp` command with arguments
`["--data-dir", "/absolute/persistent/collection"]`. The launcher works from any
working directory. Registration format belongs to the host; a common shape is:

```json
{
  "mcpServers": {
    "voice-rpg": {
      "command": "/absolute/voice-rpg/rpg-mcp",
      "args": ["--data-dir", "/absolute/persistent/collection"]
    }
  }
}
```

The server supplies its instructions and generated schemas. Resume also delivers
the full guide because some hosts truncate discovery instructions. Refresh host
tool discovery after updates and verify a read in a fresh conversation.

## Diagnostics

Stdout is protocol-only. Stderr emits compact `tool_start` and `tool_end` JSON
events with time, invocation ID, tool/version, payload sizes and digest, handler
duration, error/warning codes and revisions. No argument values, story text or
credentials are logged. These measure work after a request reaches the server;
model generation, scheduling and voice playback are outside that interval.

The CLI has no protocol server and returns its JSON receipt directly. Use the
host's execution timing to distinguish command time from conversation latency.

## Optional ChatGPT/Codex installation

`python3 install.py --codex /absolute/path/to/codex` additionally registers a thin
local plugin through that CLI. `--prepare-only` generates its files under
`.integration` without installing. The plugin points to this checkout and its
`runs` collection. Do not register it a second time as a global MCP server.

For a remote ChatGPT connection, `install_tunnel.py` is a macOS-only adapter for
the separately installed official [Secure MCP Tunnel client](https://github.com/openai/tunnel-client).
Follow that client's account/workspace onboarding, then run:

```sh
.venv/bin/python install_tunnel.py setup --key-file --replace-key \
  --tunnel-id TUNNEL_ID --data-dir /absolute/persistent/collection
.venv/bin/python install_tunnel.py status
```

The setup helper accepts the runtime key through a hidden prompt and stores it
outside the repository. File mode supports unattended restart; the optional
Keychain mode needs an unlocked login Keychain. The user LaunchAgent requires a
logged-in macOS desktop session and cannot run through sleep. Local readiness
does not prove cloud discovery: connect/refresh the account plugin and verify a
live tool read. `stop` stops only this adapter's LaunchAgent.

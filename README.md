# Voice RPG

A portable game engine for an agent that narrates a voice adventure. Python and
SQLite own the saved world, checks, inventory, health, growth and recovery. Your
agent supplies the story and voice. The engine makes no model or network calls.

## Run locally

Requires Python 3.12+ on Linux or macOS. The CLI uses only the standard library.

```sh
git clone https://github.com/mapleroyal/voice-rpg.git
cd voice-rpg
./rpg doctor --pretty
printf '{}' | ./rpg resume --data-dir "$HOME/.local/share/voice-rpg" --pretty
```

Gameplay commands take one JSON object on stdin or through `--input FILE`.
Results use `{"ok":true,"result":...}`; errors use `{"ok":false,"error":...}`
and a nonzero exit status. `./rpg schema` lists commands; `./rpg schema COMMAND`
returns their generated input contract. Normal `resume` includes the canonical
narrator guide, including when there are no campaigns yet.

Choose one persistent `--data-dir` and use it consistently. Without that option,
the CLI uses this checkout's `runs` directory. Each collection includes its
campaigns, pending checks, journals and retry identities in `game.sqlite`.

## Install in Muse

Give Muse this repository's URL and ask it to install Voice RPG. In its persistent
home directory:

```sh
mkdir -p "$HOME/workspace"
git clone https://github.com/mapleroyal/voice-rpg.git "$HOME/workspace/voice-rpg"
cd "$HOME/workspace/voice-rpg"
python3 install_muse.py
```

The installer registers `voice_rpg` in Muse's custom-skill directory and prints
the installed command. It binds a separate persistent save directory to that
command, without installing dependencies or copying engine code or saves. Use
`python3 install_muse.py --help` for alternate paths. Re-running it updates this
installation and preserves saves; unrelated skills are not overwritten.

Open a fresh Muse chat and ask to play Voice RPG. The small registered skill
provides command discovery; the application supplies gameplay guidance and
schemas. No additional memory entry or connector is needed.

Update the checkout with `git pull --ff-only`, then rerun `python3 install_muse.py`.
Keep the checkout in place. If moving it, rerun installation from the new location
with the existing save directory. Voice delivery and tool access are supplied by
the host agent, so test a real call after installation.

## Other agents

Agents with local command execution can use the CLI directly. Hosts with native
MCP support can use the same engine through its eight stdio tools; see
[MCP setup](docs/mcp.md). Both interfaces use the same schemas and narrator guide.

## Maintenance

Use the installed command (or `./rpg` with the intended `--data-dir`) for
`doctor --pretty`, `backup --pretty`, and `restore /absolute/backup.sqlite`.
Successful mutations rotate consistent SQLite backups. A backup warning after a
successful mutation does not undo it. Keep important backups outside the VM as
well; persistent local storage is not an independent backup.

To restore, stop all callers/writers and start fresh narrator context afterward.
Restore covers the entire collection and validates its format. Do not copy an
open database as a backup. If SQLite cannot settle recovery files, preserve the
database and its `-journal`, `-wal` and `-shm` sidecars together before recovery.
Legacy v1 saves and arbitrary JSON imports are unsupported.

For experiments, override `--data-dir` with a disposable directory; the installed
Muse command supports this too. Delete only that test directory after all test
processes finish. Production saves need no reset for installation or updates.

## Development

```sh
python3 -m unittest discover -s tests -v
```

This runs the dependency-free suite; MCP protocol tests skip if its SDK is absent.
Install the [optional MCP runtime](docs/mcp.md) to run those tests as well. The
native macOS Keychain test is opt-in; normal installer tests use mocks. CI checks
both the dependency-free and MCP-enabled paths.

| Concern | Canonical source |
| --- | --- |
| Narrator behavior | `skills/voice-rpg/SKILL.md` |
| Rules and arithmetic | `voice_rpg/rules.py` |
| API schemas and descriptions | `voice_rpg/contracts.py` |
| Persistence and action lifecycle | `voice_rpg/engine.py` |
| CLI and MCP adapters | `voice_rpg/cli.py`, `voice_rpg/mcp.py` |

The guide and schemas are delivered from these sources, not maintained in host
registration files. Keep adapters small and gameplay behavior host-independent.

MIT licensed; see [LICENSE](LICENSE).

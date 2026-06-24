# pokedex-mcp

An [MCP](https://modelcontextprotocol.io) server that exposes Pokémon data from the
[PokéAPI](https://pokeapi.co) to MCP clients such as Claude Code.

## Tools

| Tool | Description |
| --- | --- |
| `get_pokemon` | Get information about an individual Pokémon (types, abilities, height, weight, base stats). |
| `get_pokemon_moves` | Get the list of moves a Pokémon can learn. |
| `get_move` | Get detailed information about a move (type, power, accuracy, PP, effect). |

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/)

## Setup

```bash
git clone <repo-url>
cd pokedex-mcp
uv sync
```

## Adding the server to Claude Code

The server speaks MCP over stdio. Run it with `uv` from the project directory.

### Using the `claude mcp add` command

From the project directory, register the server with Claude Code:

```bash
claude mcp add pokedex -- uv --directory /Users/joeshonmonroe/Projects/pokedex-mcp run main.py
```

`--directory` points `uv` at this project so it uses the correct virtual
environment regardless of where Claude Code is launched from. Replace the path
with the absolute path to your clone.

By default the server is added at the **local** scope (available to you in this
project only). Use `-s` to change that:

```bash
# Available in this project for everyone (writes a .mcp.json checked into the repo)
claude mcp add -s project pokedex -- uv --directory /Users/joeshonmonroe/Projects/pokedex-mcp run main.py

# Available to you across all projects
claude mcp add -s user pokedex -- uv --directory /Users/joeshonmonroe/Projects/pokedex-mcp run main.py
```

### Manual configuration

Alternatively, add the server directly to your MCP config (e.g. a project-scoped
`.mcp.json`):

```json
{
  "mcpServers": {
    "pokedex": {
      "command": "uv",
      "args": [
        "--directory",
        "/Users/joeshonmonroe/Projects/pokedex-mcp",
        "run",
        "main.py"
      ]
    }
  }
}
```

### Verify

Restart Claude Code (or run `/mcp` in an active session) and confirm `pokedex`
appears as connected. You can then ask things like *"Use the pokedex tools to
look up Pikachu's base stats"* and Claude will call the tools.

To check status from the command line:

```bash
claude mcp list
```

## Running the server directly

For local testing you can run the server on its own:

```bash
uv run main.py
```

It will wait for an MCP client to connect over stdio.

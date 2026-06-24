import logging
from typing import Any

from pydantic import AnyHttpUrl

import httpx
from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP

from .config import config
from .token_verifier import IntrospectionTokenVerifier

logger = logging.getLogger(__name__)

# Constants
POKEDEX_API_BASE = "https://pokeapi.co/api/v2"
USER_AGENT = "pokedex-app/1.0"

def create_oauth_urls() -> dict[str, str]:
    """Create OAuth URLs based on configuration (Keycloak-style)."""
    from urllib.parse import urljoin

    auth_base_url = config.auth_base_url

    return {
        "issuer": auth_base_url.rstrip("/"),
        "introspection_endpoint": urljoin(auth_base_url, "protocol/openid-connect/token/introspect"),
        "authorization_endpoint": urljoin(auth_base_url, "protocol/openid-connect/auth"),
        "token_endpoint": urljoin(auth_base_url, "protocol/openid-connect/token"),
        "jwks_uri": urljoin(auth_base_url, "protocol/openid-connect/certs"),
    }

def create_server() -> FastMCP:
    """Create and configure the FastMCP server."""

    config.validate()

    oauth_urls = create_oauth_urls()

    token_verifier = IntrospectionTokenVerifier(
        jwks_uri=oauth_urls["jwks_uri"],
        issuer=oauth_urls["issuer"],
        server_url=config.server_url,
    )

    app = FastMCP(
        name="Pokedex MCP Server",
        instructions="Resource Server that validates Keycloak-issued JWTs locally via the realm JWKS",
        host=config.HOST,
        port=config.PORT,
        debug=True,
        streamable_http_path="/",
        token_verifier=token_verifier,
        auth=AuthSettings(
            issuer_url=AnyHttpUrl(oauth_urls["issuer"]),
            required_scopes=[config.MCP_SCOPE],
            resource_server_url=AnyHttpUrl(config.server_url),
        ),
    )


    async def fetch_json(url: str) -> dict[str, Any] | None:
        """Make a GET request to the PokeAPI and return parsed JSON, or None on error."""
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, headers=headers, timeout=30.0)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPError:
                return None


    @app.tool()
    async def get_pokemon(name_or_id: str) -> str:
        """Get information about an individual Pokemon.

        Args:
            name_or_id: The name (e.g. "pikachu") or Pokedex number (e.g. "25") of the Pokemon.
        """
        url = f"{POKEDEX_API_BASE}/pokemon/{name_or_id.lower().strip()}"
        data = await fetch_json(url)
        if data is None:
            return f"Could not find a Pokemon matching '{name_or_id}'."

        types = ", ".join(t["type"]["name"] for t in data.get("types", []))
        abilities = ", ".join(
            a["ability"]["name"] + (" (hidden)" if a.get("is_hidden") else "")
            for a in data.get("abilities", [])
        )
        stats = "\n".join(
            f"  - {s['stat']['name']}: {s['base_stat']}" for s in data.get("stats", [])
        )

        # PokeAPI reports height in decimetres and weight in hectograms.
        height_m = data.get("height", 0) / 10
        weight_kg = data.get("weight", 0) / 10

        return (
            f"Name: {data['name'].capitalize()}\n"
            f"Pokedex #: {data['id']}\n"
            f"Type(s): {types or 'unknown'}\n"
            f"Height: {height_m} m\n"
            f"Weight: {weight_kg} kg\n"
            f"Abilities: {abilities or 'unknown'}\n"
            f"Base stats:\n{stats or '  unknown'}"
        )


    @app.tool()
    async def get_pokemon_moves(name_or_id: str) -> str:
        """Get the list of moves a Pokemon can learn.

        Args:
            name_or_id: The name (e.g. "pikachu") or Pokedex number (e.g. "25") of the Pokemon.
        """
        url = f"{POKEDEX_API_BASE}/pokemon/{name_or_id.lower().strip()}"
        data = await fetch_json(url)
        if data is None:
            return f"Could not find a Pokemon matching '{name_or_id}'."

        moves = data.get("moves", [])
        if not moves:
            return f"{data['name'].capitalize()} has no recorded moves."

        move_names = sorted(m["move"]["name"] for m in moves)
        formatted = "\n".join(f"  - {m}" for m in move_names)
        return f"{data['name'].capitalize()} can learn {len(move_names)} moves:\n{formatted}"


    @app.tool()
    async def get_move(name_or_id: str) -> str:
        """Get detailed information about a Pokemon move.

        Args:
            name_or_id: The name (e.g. "thunderbolt") or ID (e.g. "85") of the move.
        """
        slug = name_or_id.lower().strip().replace(" ", "-")
        url = f"{POKEDEX_API_BASE}/move/{slug}"
        data = await fetch_json(url)
        if data is None:
            return f"Could not find a move matching '{name_or_id}'."

        # The English flavor/effect text isn't guaranteed to exist for every move.
        effect = next(
            (
                e["short_effect"]
                for e in data.get("effect_entries", [])
                if e.get("language", {}).get("name") == "en"
            ),
            None,
        )
        if effect:
            # Substitute the effect_chance placeholder when present.
            effect = effect.replace("$effect_chance", str(data.get("effect_chance", "")))

        return (
            f"Name: {data['name'].replace('-', ' ').title()}\n"
            f"Move ID: {data['id']}\n"
            f"Type: {data.get('type', {}).get('name', 'unknown')}\n"
            f"Damage class: {data.get('damage_class', {}).get('name', 'unknown')}\n"
            f"Power: {data.get('power') if data.get('power') is not None else '—'}\n"
            f"Accuracy: {data.get('accuracy') if data.get('accuracy') is not None else '—'}\n"
            f"PP: {data.get('pp') if data.get('pp') is not None else '—'}\n"
            f"Priority: {data.get('priority', 0)}\n"
            f"Effect: {effect or 'No description available.'}"
        )

    return app


def main() -> int:
    """
    Run the Pokedex MCP Server.

    This server:
    - Provides RFC 9728 Protected Resource Metadata
    - Validates Keycloak-issued JWTs locally against the realm JWKS
    - Serves MCP tools requiring authentication

    Configuration is loaded from config.py and environment variables.
    """
    logging.basicConfig(level=logging.INFO)

    try:
        config.validate()
        oauth_urls = create_oauth_urls()

    except ValueError as e:
        logger.error("Configuration error: %s", e)
        return 1

    try:
        mcp_server = create_server()

        logger.info("Starting MCP Server on %s:%s", config.HOST, config.PORT)
        logger.info("Authorization Server: %s", oauth_urls["issuer"])
        logger.info("Transport: %s", config.TRANSPORT)

        mcp_server.run(transport=config.TRANSPORT)
        return 0

    except Exception:
        logger.exception("Server error")
        return 1


if __name__ == "__main__":
    exit(main())
    
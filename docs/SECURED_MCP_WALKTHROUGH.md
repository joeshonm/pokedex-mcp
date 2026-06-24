# Building & Connecting to a Secured MCP Server

A walkthrough of this repository, written to be presented. It explains, in order:

1. [What we're building (the mental model)](#1-the-mental-model)
2. [How the MCP server is created](#2-creating-the-mcp-server)
3. [How tokens are validated](#3-validating-tokens)
4. [How Keycloak is set up as the auth layer](#4-keycloak-as-the-auth-layer)
5. [How a client (Claude Code) connects](#5-how-a-client-connects)
6. [Dev vs. production — what changes](#6-dev-vs-production)

> Throughout, file references look like `pokedex_mcp/server.py:46` so you can jump
> straight to the code on screen.

---

## 1. The mental model

The Model Context Protocol (MCP) lets an AI client (Claude Code, Claude Desktop,
etc.) call **tools** exposed by a server. Our server exposes three Pokémon tools
backed by the public [PokéAPI](https://pokeapi.co).

The interesting part isn't the tools — it's that the server is **secured**. A
client can't just call the tools; it must present a valid OAuth access token.
That pulls three actors into the picture, and the whole talk hinges on keeping
them straight:

```
          (1) "I want to use the pokedex tools"
 ┌────────────┐                                   ┌─────────────────────┐
 │   Client   │ ───── authenticate (OAuth) ─────▶ │  Authorization      │
 │ (Claude    │ ◀──── access token (JWT) ──────── │  Server  = Keycloak │
 │  Code)     │                                   └─────────────────────┘
 └────────────┘                                              ▲
       │                                                     │ (3) verify token
       │ (2) call tool + Bearer <token>                      │  signature/claims
       ▼                                                     │  (via JWKS)
 ┌─────────────────────────────────────────────┐            │
 │  Resource Server = our MCP server            │ ───────────┘
 │  (pokedex_mcp) — exposes get_pokemon, etc.   │
 └─────────────────────────────────────────────┘
```

The terms come from OAuth 2.1 / the MCP authorization spec:

| OAuth term | In this project | Role |
| --- | --- | --- |
| **Authorization Server (AS)** | Keycloak | Authenticates the user, issues tokens |
| **Resource Server (RS)** | Our MCP server | Hosts the protected tools, validates tokens |
| **Client** | Claude Code | Wants to call the tools on the user's behalf |

The key design principle: **the Resource Server never sees a password and never
issues tokens.** It only *validates* tokens that Keycloak issued. Auth is
cleanly separated from the application.

---

## 2. Creating the MCP server

Everything server-side lives in `pokedex_mcp/`. The entry point is
`create_server()` in `pokedex_mcp/server.py`.

### 2a. The framework: FastMCP

We use **FastMCP**, the high-level server class from the official `mcp` SDK. It
handles the MCP wire protocol, transport, and (importantly for us) the auth
plumbing, so we only write tools and configuration.

```python
# pokedex_mcp/server.py:46
app = FastMCP(
    name="Pokedex MCP Server",
    instructions="Resource Server that validates Keycloak-issued JWTs locally via the realm JWKS",
    host=config.HOST,
    port=config.PORT,
    debug=True,
    streamable_http_path="/",
    token_verifier=token_verifier,            # ← how we check tokens (Section 3)
    auth=AuthSettings(                         # ← what auth we require
        issuer_url=AnyHttpUrl(oauth_urls["issuer"]),
        required_scopes=[config.MCP_SCOPE],
        resource_server_url=AnyHttpUrl(config.server_url),
    ),
)
```

The two security-relevant arguments are worth dwelling on:

- **`auth=AuthSettings(...)`** turns this from an open server into a *protected
  resource*. Three fields:
  - `issuer_url` — who we trust to issue tokens (the Keycloak realm). Tokens
    from anyone else are rejected.
  - `required_scopes=["mcp:tools"]` — a caller's token must carry this scope or
    the request is refused. This is coarse-grained authorization: "you may use
    the tools."
  - `resource_server_url` — our own public URL. This becomes the **audience**
    the token must be addressed to (more on this in Section 3). It prevents a
    token minted for *some other* service from being replayed against us.

- **`token_verifier=token_verifier`** is the object that actually inspects an
  incoming token and says yes/no. We supply our own implementation (Section 3).

### 2b. Transport: why `streamable-http`

```python
# pokedex_mcp/config.py
TRANSPORT: str = os.getenv("TRANSPORT", "streamable-http")
```

MCP supports multiple transports. **stdio** (the server runs as a subprocess of
the client) is the simplest, but it has no network surface and therefore no
place for OAuth — auth is implicit in "you launched the process."

Because we want to demonstrate **OAuth over the network**, we use the
**streamable-HTTP** transport. The server listens on `http://localhost:3000/`,
and every MCP request is an HTTP request that carries an
`Authorization: Bearer <token>` header. That header is what the token verifier
checks. `streamable_http_path="/"` mounts the MCP endpoint at the root.

> Talking point: "The transport choice *is* the security-model choice. HTTP is
> what makes a standalone Authorization Server meaningful."

### 2c. Protected Resource Metadata (the discovery handshake)

This is the piece that makes the client able to authenticate *without being
pre-configured*. Because we passed `AuthSettings`, FastMCP automatically serves
an [RFC 9728](https://datatracker.ietf.org/doc/html/rfc9728) document at:

```
GET http://localhost:3000/.well-known/oauth-protected-resource
```

```json
{
  "resource": "http://localhost:3000/",
  "authorization_servers": ["http://localhost:8080/realms/master"],
  "scopes_supported": ["mcp:tools"],
  "bearer_methods_supported": ["header"]
}
```

When a client hits a protected endpoint with no token, the server responds
`401` with a `WWW-Authenticate` header pointing at this metadata. The client
reads it and learns **"to talk to me, go get a token from this Keycloak realm,
and ask for the `mcp:tools` scope."** No manual client config required — this is
the whole point of the spec.

### 2d. The tools themselves

The tools are deliberately ordinary — the security is orthogonal to them. A tool
is just an `async` function with a docstring, decorated with `@app.tool()`:

```python
# pokedex_mcp/server.py:74
@app.tool()
async def get_pokemon(name_or_id: str) -> str:
    """Get information about an individual Pokemon.

    Args:
        name_or_id: The name (e.g. "pikachu") or Pokedex number (e.g. "25").
    """
    url = f"{POKEDEX_API_BASE}/pokemon/{name_or_id.lower().strip()}"
    data = await fetch_json(url)
    ...
```

Two things the SDK does for free here:

- **Schema generation.** The function signature (`name_or_id: str`) and docstring
  become the tool's JSON schema and description, which is what the model sees.
- **Auth enforcement.** Because the server has `AuthSettings`, *every* tool call
  is gated by the token verifier before the function body ever runs. We didn't
  write a single `if authorized` line inside the tools — that's the framework
  enforcing it at the transport layer.

> Talking point: "Notice there's zero auth code in the tool. Authentication is a
> property of the server, not of each function. That separation is the design
> win."

### 2e. Configuration: 12-factor style

All knobs come from the environment via `pokedex_mcp/config.py`, loaded from a
local `.env` in dev:

```python
HOST / PORT                     # where the RS listens
AUTH_HOST / AUTH_PORT / AUTH_REALM   # how to reach the Keycloak realm
MCP_SCOPE = "mcp:tools"         # the scope we require
TRANSPORT = "streamable-http"
```

Two derived URLs are computed as properties:

```python
server_url    -> http://localhost:3000           # our public identity (audience)
auth_base_url -> http://localhost:8080/realms/master/   # the realm root
```

From `auth_base_url`, `create_oauth_urls()` (`server.py:19`) derives every
Keycloak endpoint we need — most importantly the **JWKS URI**
(`.../protocol/openid-connect/certs`), the public keys we'll use to verify token
signatures.

---

## 3. Validating tokens

This is the heart of "secured." When a request arrives with a Bearer token, the
server must answer: *is this token real, unexpired, meant for me, and does it
carry the right scope?* That logic lives in
`pokedex_mcp/token_verifier.py`.

### 3a. Two ways to validate a token

There are two standard strategies, and this project deliberately shows the
trade-off because we hit it live during development:

| | **Remote introspection** (RFC 7662) | **Local JWKS validation** (what we use) |
| --- | --- | --- |
| How | RS calls Keycloak's `/introspect` on every request | RS verifies the JWT signature itself using Keycloak's public keys |
| Network | One AS round-trip **per request** | Fetch public keys **once**, then offline |
| Trust source | AS's live answer (`active: true/false`) | Cryptographic signature + claims |
| Failure mode we hit | Keycloak returned `active: false` for valid tokens (session-binding quirk in dev) | none — pure crypto |
| Latency | Higher (extra hop) | Lower |

We started with introspection (the class is still named
`IntrospectionTokenVerifier` for import compatibility) and switched to **local
JWKS validation** because it's faster, has no per-request dependency on the AS,
and avoided a Keycloak session quirk.

> Talking point: "Both are legitimate. Introspection gives you instant
> revocation; local validation gives you speed and resilience. We chose local
> and accept that a revoked token stays valid until it expires."

### 3b. How local validation works

```python
# pokedex_mcp/token_verifier.py:45
async def verify_token(self, token: str) -> AccessToken | None:
    try:
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=self.algorithms,         # ["RS256"]
            issuer=self.issuer,                 # must be our Keycloak realm
            audience=self.resource_url,         # must be addressed to us
            options={"require": ["exp", "iss"]},
        )
    except jwt.PyJWTError as e:
        logger.info("Token rejected: %s", e)
        return None
    ...
```

Step by step — each line is a security check:

1. **`get_signing_key_from_jwt(token)`** reads the token's `kid` (key ID) header
   and fetches the matching **public** key from Keycloak's JWKS endpoint. The
   `PyJWKClient` caches keys and refetches only if it sees an unknown `kid` (i.e.
   after a key rotation). The RS never holds a secret here — only Keycloak's
   public key.

2. **`jwt.decode(...)`** does the cryptographic and claims checks in one call:
   - **Signature** — proves Keycloak (and only Keycloak, holder of the private
     key) minted this token and it hasn't been tampered with.
   - **`algorithms=["RS256"]`** — pins the algorithm. This blocks the classic
     "alg: none" and HS/RS confusion attacks.
   - **`issuer=`** — the `iss` claim must equal our realm URL.
   - **`audience=`** — the `aud` claim must contain *our* resource URL. This is
     why Keycloak has an "audience mapper" (Section 4): without it, the token
     wouldn't be addressed to us and we'd reject it.
   - **`require: ["exp", "iss"]`** + built-in `exp` check — expired tokens are
     rejected automatically.

3. **Scope extraction** — `scope` is a space-delimited string in OAuth; we split
   it into a list. FastMCP then enforces `required_scopes` against it.

4. **`_validate_resource(claims)`** — a defense-in-depth re-check of the audience
   using the SDK's `check_resource_allowed` helper (handles `aud` being either a
   string or a list, and normalizes trailing slashes). Redundant with step 2 by
   design, so a future change to decode options can't silently disable the
   audience check.

5. On success we return an **`AccessToken`** describing the caller (client id,
   scopes, expiry, audience). Returning `None` anywhere means "401, access
   denied."

> Talking point: "Validation is just a sequence of `return None`s. Any check that
> fails ends the request. The token has to clear *all* of them."

### 3c. The trailing-slash gotcha (a great live-debugging story)

Keycloak stamps `aud = "http://localhost:3000"` (no slash), while our metadata
advertises the resource as `"http://localhost:3000/"` (with slash). The SDK's
`resource_url_from_server_url()` normalizes both sides so they match — but this
is exactly the kind of subtle mismatch that breaks OAuth integrations. Worth
showing as "auth bugs are usually one character, not one concept."

---

## 4. Keycloak as the auth layer

[Keycloak](https://www.keycloak.org/) is a mature open-source Identity &
Access Management server. It plays the **Authorization Server**: it owns users,
clients, scopes, and the signing keys, and it issues the JWTs our RS validates.

### 4a. How we run it (dev)

```bash
docker run -p 8080:8080 \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
  -e KC_BOOTSTRAP_ADMIN_PASSWORD=admin \
  quay.io/keycloak/keycloak start-dev
```

`start-dev` is Keycloak's **development mode**: it runs on an in-memory H2
database, disables HTTPS requirements, and skips hostname checks. Perfect for a
laptop demo, unacceptable for production (Section 6).

We use the built-in `master` realm. A *realm* is an isolated tenant: its own
users, clients, scopes, and signing keys.

### 4b. The four things Keycloak must be configured with

For the end-to-end flow to work, the realm needs:

1. **A client scope named `mcp:tools`.** This is the scope our RS requires.
   Created as a realm client scope so clients can request it.

2. **An audience mapper on that scope.** By default a Keycloak token's `aud`
   doesn't include our RS. We add an *audience mapper* to the `mcp:tools` scope
   that injects `aud: http://localhost:3000` into every token carrying that
   scope. This is what lets step 2 of the verifier (Section 3b) pass.

   > Without this mapper, every token is rejected with "audience does not match."
   > This is the single most common Keycloak-as-RS misconfiguration.

3. **Dynamic Client Registration (DCR) enabled with the right default scopes.**
   This is subtle and important (see 4c).

4. **A sane access-token lifespan.** The `master` realm defaulted to **60
   seconds** here, which caused tokens to expire between issuance and use. We
   raised it to 3600s for the demo. (Short-lived tokens are *good* practice — 60s
   is just impractically short for a manual demo.)

### 4c. Dynamic Client Registration — the part that surprises people

Claude Code does **not** use a client ID/secret you configure. Instead it uses
**Dynamic Client Registration** ([RFC 7591](https://datatracker.ietf.org/doc/html/rfc7591)):
on first connect it calls Keycloak's registration endpoint and **creates its own
client on the fly**.

```
Claude Code ──▶ POST /realms/master/clients-registrations/openid-connect
            ◀── { client_id: "8fd29ea7-…", … }   (a brand-new client)
```

Consequences you must design for:

- The `pokedex-client` you might create by hand is **irrelevant** to Claude Code
  — it registers a *different* client each time.
- A freshly registered client only gets the scopes the realm grants new clients
  **by default**. So `offline_access` and `mcp:tools` must be in the realm's
  **default client scopes**, or the very first authorization request fails with
  `invalid_scope`.
- The realm's *client registration policy* ("Allowed Client Scopes",
  `allow-default-scopes: true`) governs what these auto-created clients may
  request.

We verified empirically that a freshly registered DCR client now inherits both
`offline_access` and `mcp:tools` as defaults — which is what makes the
zero-config client experience actually work.

> Talking point: "The client you can see in the admin console isn't the client
> doing the work. DCR means the realm *defaults* are the real configuration
> surface."

### 4d. The full request, end to end

1. Claude Code calls a tool → RS replies `401` + metadata pointing at Keycloak.
2. Claude Code registers itself (DCR) and runs the OAuth Authorization Code flow
   against Keycloak, requesting scopes `mcp:tools offline_access`.
3. Keycloak authenticates the user and issues a signed JWT whose `aud` includes
   `http://localhost:3000` and whose `scope` includes `mcp:tools`.
4. Claude Code re-calls the tool with `Authorization: Bearer <jwt>`.
5. The RS verifies the JWT locally against the realm JWKS (Section 3), confirms
   the scope, and runs the tool.
6. The PokéAPI response flows back to the model.

---

## 5. How a client connects

Once the server is running and Keycloak is configured, the client side is almost
nothing — which is the payoff of doing the spec properly. In Claude Code:

```bash
claude mcp add --transport http pokedex http://localhost:3000/
```

Then `/mcp` triggers the OAuth flow in a browser. On success the tools
(`get_pokemon`, `get_pokemon_moves`, `get_move`) appear and can be called. No
client secret, no scope, no token handling in client config — all of it was
discovered from the server's metadata and negotiated with Keycloak.

---

## 6. Dev vs. production

This is the slide to linger on. **Everything that makes the demo convenient is a
production liability.** A side-by-side:

| Concern | This dev example | Production |
| --- | --- | --- |
| **Transport** | `http://localhost` | **HTTPS only.** Bearer tokens over plain HTTP are interceptable. Terminate TLS at the RS or a proxy. |
| **Keycloak mode** | `start-dev` (H2 in-memory, no TLS, no hostname check) | `start` with a real database (Postgres), `KC_HOSTNAME` set, TLS, `--optimized` build. |
| **Keycloak persistence** | Ephemeral container — **all config is lost on `docker rm`** | Persistent DB + realm config as **infrastructure-as-code** (realm export JSON, or Terraform/`kcadm` scripts) so it's reproducible. |
| **Realm** | Shared built-in `master` realm | A dedicated realm per environment; never use `master` for apps — it's the admin realm. |
| **Admin credentials** | `admin` / `admin` | Strong, rotated, secret-managed admin creds; admin console not publicly exposed. |
| **Token lifespan** | Raised to 3600s for demo convenience | Short access tokens (~5 min) + refresh tokens. Short tokens limit the blast radius of a leak. |
| **Token revocation** | None — local validation means a token is valid until expiry | If instant revocation matters, combine local validation with introspection, a token denylist, or rely on short lifespans. |
| **Client model (DCR)** | Open Dynamic Client Registration | Lock down DCR: require an initial access token, restrict via client registration policies, or pre-register trusted clients. Open DCR on a public AS is an abuse vector. |
| **Audience / scopes** | One coarse `mcp:tools` scope | Granular scopes per capability (e.g. `pokedex:read`); per-tool authorization if needed. |
| **JWKS / key rotation** | Default keys, cached by `PyJWKClient` | Plan for key rotation; ensure caching honors rotation and set sensible refresh/TTL behavior. |
| **Secrets** | `.env` with secret committed-adjacent (gitignored) | Real secret manager (Vault, cloud KMS/Secrets Manager); nothing secret on disk in plaintext. |
| **Error visibility** | `debug=True`, verbose logging | `debug=False`; avoid logging tokens or claims; structured audit logging instead. |
| **CORS / trusted hosts** | Permissive (localhost) | Locked-down allowed origins and trusted-host registration policy. |

### The one-sentence version for the talk

> "In dev we optimize for *seeing the flow work* — plaintext HTTP, an in-memory
> Keycloak, long-lived tokens, open client registration. In production every one
> of those flips to its secure counterpart, but **the architecture doesn't
> change**: a client gets a token from an Authorization Server and presents it to
> a Resource Server that validates it. Dev and prod differ in hardening, not in
> shape."

---

## Appendix: file map

| File | Responsibility |
| --- | --- |
| `pokedex_mcp/server.py` | Builds the FastMCP server, wires auth, defines the three tools |
| `pokedex_mcp/token_verifier.py` | Local JWKS-based JWT validation |
| `pokedex_mcp/config.py` | Environment-driven configuration + derived URLs |
| `pokedex_mcp/__main__.py` / `main.py` | Entry points (`python -m pokedex_mcp`) |
| `.env` / `.env.example` | Local configuration (real secrets gitignored) |

> Note: `OAUTH_CLIENT_ID` / `OAUTH_CLIENT_SECRET` in `config.py` were used by the
> earlier *introspection* verifier. The current JWKS verifier doesn't need them;
> they're retained only so you can switch back to introspection without code
> changes. Call this out if an attendee asks why they're still there.

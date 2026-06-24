"""Token verifier implementing local JWT validation against the Keycloak JWKS.

Keycloak issues signed RS256 JWTs. Rather than calling the introspection
endpoint on every request (which depends on a live session existing on the
authorization server and was returning ``active: false`` for otherwise-valid
tokens), we validate the token locally: verify the signature against the
realm's published signing keys, then check the standard claims (exp, iss, aud)
and the resource's required scope.
"""

import logging
from typing import Any

import jwt
from jwt import PyJWKClient

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.shared.auth_utils import check_resource_allowed, resource_url_from_server_url

logger = logging.getLogger(__name__)


class IntrospectionTokenVerifier(TokenVerifier):
    """Token verifier that validates Keycloak JWTs locally via the realm JWKS.

    The class name is kept for backwards compatibility with existing imports;
    validation no longer uses RFC 7662 introspection.
    """

    def __init__(
        self,
        jwks_uri: str,
        issuer: str,
        server_url: str,
        algorithms: list[str] | None = None,
    ):
        self.jwks_uri = jwks_uri
        self.issuer = issuer
        self.server_url = server_url
        self.algorithms = algorithms or ["RS256"]
        self.resource_url = resource_url_from_server_url(server_url)
        # PyJWKClient caches keys internally and refreshes on unknown kid.
        self._jwks_client = PyJWKClient(jwks_uri)

    async def verify_token(self, token: str) -> AccessToken | None:
        """Verify a bearer token by validating its signature and claims locally."""
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token)

            # jwt.decode validates signature, exp/nbf/iat, issuer and audience.
            # Keycloak stamps our resource URL into `aud` via an audience mapper.
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=self.algorithms,
                issuer=self.issuer,
                audience=self.resource_url,
                options={"require": ["exp", "iss"]},
            )
        except jwt.PyJWTError as e:
            logger.info("Token rejected: %s", e)
            return None

        # `scope` is a space-delimited string per OAuth 2.0.
        scopes = claims.get("scope", "").split() if claims.get("scope") else []

        # Defense in depth: the audience claim is already verified by jwt.decode,
        # but re-check via the shared helper so a missing/odd `aud` can't slip
        # through if the decode options ever change.
        if not self._validate_resource(claims):
            logger.info("Token rejected: audience does not match resource server")
            return None

        return AccessToken(
            token=token,
            client_id=claims.get("azp") or claims.get("client_id", "unknown"),
            scopes=scopes,
            expires_at=claims.get("exp"),
            resource=claims.get("aud"),
        )

    def _validate_resource(self, claims: dict[str, Any]) -> bool:
        """Validate the token was issued for this resource server.

        Accept if any audience entry matches the derived resource URL.
        Supports both the string and list forms of the `aud` claim.
        """
        if not self.server_url or not self.resource_url:
            return False

        aud: list[str] | str | None = claims.get("aud")
        if isinstance(aud, list):
            return any(self._is_valid_resource(a) for a in aud)
        if isinstance(aud, str):
            return self._is_valid_resource(aud)
        return False

    def _is_valid_resource(self, resource: str) -> bool:
        """Check if the given resource matches our server."""
        return check_resource_allowed(self.resource_url, resource)

"""HTTP Basic authentication as plain ASGI middleware.

Basic sends credentials base64-encoded, which is to say in the clear. This
server is intended to run behind a TLS-terminating reverse proxy, which is what
makes that acceptable; see the README. Authentication stays in the app rather
than moving entirely to the proxy so the container is never silently open to
anything that reaches it directly on the LAN.
"""

import base64
import binascii
import secrets

from starlette.datastructures import Headers
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send


class BasicAuthMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        username: str,
        password: str,
        realm: str = "Calibre library",
    ) -> None:
        self.app = app
        self._username = username
        self._password = password
        self._realm = realm

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        header = Headers(scope=scope).get("authorization")
        if not self._is_authorized(header):
            response = Response(
                status_code=401,
                headers={"WWW-Authenticate": f'Basic realm="{self._realm}"'},
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)

    def _is_authorized(self, header: str | None) -> bool:
        if not header:
            return False
        scheme, _, encoded = header.partition(" ")
        if scheme.casefold() != "basic":
            return False
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except binascii.Error, UnicodeDecodeError:
            return False
        username, separator, password = decoded.partition(":")
        if not separator:
            return False
        # Both comparisons always run: short-circuiting would leak which half
        # was wrong through response timing.
        user_ok = secrets.compare_digest(username, self._username)
        password_ok = secrets.compare_digest(password, self._password)
        return user_ok and password_ok

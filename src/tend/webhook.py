"""HTTP receiver for external producers.

Exposes:
  POST /say   — direct TTS (no LLM round-trip); body: {text, category?, urgent?}
  POST /event — structured event for skill mediation; body: {kind, ...}

Authenticated via `Authorization: Bearer <token>`. Bound to loopback only;
the caller is responsible for keeping the network surface narrow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web
from loguru import logger

from tend.dispatch import dispatch_event


def build_app(
    *,
    token: str,
    announcer,
    dispatch: Callable[[str, dict], Awaitable[None]],
    skills_root: Path,
) -> web.Application:
    """Construct the aiohttp Application. Pure construction; the caller
    owns lifecycle (start/stop)."""

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        header = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if header != expected:
            return web.json_response(
                {"error": "unauthorized"}, status=401,
            )
        return await handler(request)

    async def handle_say(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid-json"}, status=400)
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return web.json_response(
                {"error": "missing 'text' field"}, status=400,
            )
        category = str(body.get("category", "general"))
        urgent = bool(body.get("urgent", False))
        delivered = await announcer.announce(
            text=text, source="webhook", category=category, urgent=urgent,
        )
        return web.json_response({"delivered": bool(delivered)})

    async def handle_event(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid-json"}, status=400)
        kind = body.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            return web.json_response(
                {"error": "missing 'kind' field"}, status=400,
            )
        payload = {k: v for k, v in body.items() if k != "kind"}
        names = await dispatch_event(
            kind=kind, payload=payload,
            dispatch=dispatch, skills_root=skills_root,
        )
        return web.json_response({"dispatched": names})

    app = web.Application(middlewares=[auth_middleware])
    app.router.add_post("/say", handle_say)
    app.router.add_post("/event", handle_event)
    return app


class WebhookServer:
    """Lifecycle wrapper. main.py constructs one and calls start/stop."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        app: web.Application,
    ):
        self._host = host
        self._port = port
        self._app = app
        self._runner: web.AppRunner | None = None

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info(f"webhook server listening on {self._host}:{self._port}")

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

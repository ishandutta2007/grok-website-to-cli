"""
WebSocket Bridge Server
~~~~~~~~~~~~~~~~~~~~~~~~

Runs a local WebSocket server that the paired browser extension connects to.
The CLI sends commands through this bridge to control the Grok tab in the
user's real Edge browser.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

import websockets
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)

DEFAULT_PORT = 18765


class GrokBridge:
    """WebSocket server bridging CLI commands to the browser extension.

    The extension connects as a client, and the CLI sends commands through
    this bridge. Each command gets a unique ID so responses can be matched.
    """

    def __init__(self, port: int = DEFAULT_PORT) -> None:
        self.port = port
        self._extension_ws: Optional[WebSocketServerProtocol] = None
        self._pending: dict[str, asyncio.Future] = {}
        self._connected = asyncio.Event()
        self._server: Optional[Any] = None

    @property
    def is_connected(self) -> bool:
        """Whether the extension is currently connected."""
        if self._extension_ws is None:
            return False
        # Support both legacy WebSocketServerProtocol (.open) and new ServerConnection (.state)
        if hasattr(self._extension_ws, "open"):
            return self._extension_ws.open
        if hasattr(self._extension_ws, "state"):
            return getattr(self._extension_ws.state, "name", "") == "OPEN"
        return True

    async def start(self) -> None:
        """Start the WebSocket server on localhost."""
        self._server = await websockets.serve(
            self._on_connection,
            "127.0.0.1",
            self.port,
            ping_interval=20,
            ping_timeout=10,
        )
        logger.info("WebSocket bridge listening on ws://127.0.0.1:%d", self.port)

    async def stop(self) -> None:
        """Shut down the WebSocket server."""
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._connected.clear()

    async def wait_for_extension(self, timeout: float = 60) -> None:
        """Block until the browser extension connects.

        Args:
            timeout: Maximum seconds to wait for the extension.

        Raises:
            TimeoutError: If the extension doesn't connect in time.
        """
        try:
            await asyncio.wait_for(self._connected.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"Browser extension did not connect within {int(timeout)}s.\n"
                "Make sure the 'Grok CLI Bridge' extension is installed and enabled in Edge.\n"
                "The extension auto-connects when the CLI server is running."
            )

    async def send_command(
        self,
        cmd_type: str,
        timeout: float = 30,
        **kwargs: Any,
    ) -> dict:
        """Send a command to the extension and wait for the response.

        Args:
            cmd_type: The command type (e.g. 'find_grok_tab', 'send_prompt').
            timeout: Maximum seconds to wait for a response.
            **kwargs: Additional command parameters.

        Returns:
            The response data dict from the extension.

        Raises:
            ConnectionError: If the extension is not connected.
            TimeoutError: If the extension doesn't respond in time.
            RuntimeError: If the extension returns an error.
        """
        if not self.is_connected:
            raise ConnectionError(
                "Extension is not connected. "
                "Check that the Grok CLI Bridge extension is running in Edge."
            )

        msg_id = str(uuid.uuid4())
        command = {"id": msg_id, "type": cmd_type, **kwargs}

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._pending[msg_id] = future

        try:
            await self._extension_ws.send(json.dumps(command))
            logger.debug("Sent command: %s (id=%s)", cmd_type, msg_id[:8])

            result = await asyncio.wait_for(future, timeout=timeout)

            if not result.get("success"):
                error_msg = result.get("error", "Unknown error from extension")
                raise RuntimeError(f"Extension error: {error_msg}")

            return result.get("data", {})
        finally:
            self._pending.pop(msg_id, None)

    # ── Internal handlers ─────────────────────────────────────────────────

    async def _on_connection(self, ws: WebSocketServerProtocol) -> None:
        """Handle a new WebSocket connection from the extension."""
        logger.info("Browser extension connected.")
        self._extension_ws = ws
        self._connected.set()

        try:
            async for raw_message in ws:
                try:
                    message = json.loads(raw_message)
                    msg_id = message.get("id")
                    if msg_id and msg_id in self._pending:
                        self._pending[msg_id].set_result(message)
                    else:
                        logger.debug(
                            "Received unmatched message: %s",
                            str(message)[:200],
                        )
                except json.JSONDecodeError:
                    logger.warning("Received non-JSON message from extension.")
        except websockets.exceptions.ConnectionClosed:
            logger.warning("Extension disconnected.")
        finally:
            self._extension_ws = None
            self._connected.clear()
            # Cancel any pending futures
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(
                        ConnectionError(
                            "Extension disconnected while waiting for response."
                        )
                    )
            self._pending.clear()

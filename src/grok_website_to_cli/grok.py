"""
Grok Automation (Extension-based)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

High-level orchestration of Grok interactions via the paired browser extension.
All DOM operations are performed by the extension's content script running
inside the user's real Edge browser session.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from grok_website_to_cli.browser import GrokBridge

logger = logging.getLogger(__name__)


class GrokAutomation:
    """Orchestrates Grok interactions through the browser extension bridge.

    Sends high-level commands to the extension, which handles the actual
    DOM manipulation inside the user's logged-in Edge browser.
    """

    def __init__(
        self,
        bridge: GrokBridge,
        max_wait_seconds: int = 180,
        poll_interval: float = 3.0,
    ) -> None:
        self.bridge = bridge
        self.max_wait_seconds = max_wait_seconds
        self.poll_interval = poll_interval

    async def find_or_open_grok_tab(self) -> int:
        """Find an existing Grok tab or open a new one.

        Returns:
            The tab ID of the Grok tab.
        """
        # Search for existing Grok tabs
        result = await self.bridge.send_command("find_grok_tab")
        tabs = result.get("tabs", [])

        if tabs:
            tab = tabs[0]
            tab_id = tab["id"]
            logger.info("Found Grok tab: %s (id=%d)", tab.get("title", ""), tab_id)
            # Activate the tab and bring window to focus
            await self.bridge.send_command("activate_tab", tabId=tab_id)
            return tab_id
        else:
            logger.info("No Grok tab found. Opening a new one...")
            result = await self.bridge.send_command("open_grok_tab", timeout=30)
            tab_id = result["tabId"]
            logger.info("Opened new Grok tab (id=%d)", tab_id)
            return tab_id

    async def send_prompt(
        self,
        prompt_text: str,
        max_retries: int = 5,
        retry_delay: float = 3.0,
    ) -> None:
        """Send a prompt to the Grok input box.

        The extension's content script handles finding the input element,
        setting the value (React-compatible), and submitting with Enter.

        Retries up to ``max_retries`` times on transient failures
        (timeouts, disconnections, extension errors).

        Args:
            prompt_text: The full prompt text to send.
            max_retries: Maximum number of send attempts.
            retry_delay: Seconds to wait between retries.
        """
        logger.info("Sending prompt to Grok (%d chars)...", len(prompt_text))

        # Capture initial footer count before sending
        try:
            initial_status = await self.bridge.send_command(
                "check_response_status", timeout=5
            )
            diag = initial_status.get("diag", {})
            self._initial_footer_count = diag.get("actionFooters", 0)
        except Exception:
            self._initial_footer_count = 0

        for attempt in range(1, max_retries + 1):
            try:
                await self.bridge.send_command(
                    "send_prompt",
                    prompt=prompt_text,
                    timeout=30,
                )
                logger.info(
                    "Prompt submitted successfully (attempt %d/%d).",
                    attempt,
                    max_retries,
                )
                return
            except (ConnectionError, TimeoutError, RuntimeError, OSError) as exc:
                logger.warning(
                    "send_prompt failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )
                if attempt < max_retries:
                    logger.info("Retrying send_prompt in %.1fs...", retry_delay)
                    await asyncio.sleep(retry_delay)

        raise RuntimeError(f"Failed to send prompt after {max_retries} attempts.")

    async def wait_for_response(self) -> None:
        """Wait for Grok to finish generating its response.

        Strategy: Grok removes the last action footer ("Create share link"
        button) while generating and restores it when done. We detect
        generation completion by watching for the footer count to
        INCREASE between consecutive polls (the dip → recovery pattern).
        """

        logger.info(
            "Waiting for Grok response (up to %ds)... [v1-footer-dip]",
            self.max_wait_seconds,
        )

        # Initial delay to let generation start
        await asyncio.sleep(3)

        start_time = asyncio.get_event_loop().time()
        prev_footers = None  # footer count from previous poll

        while (asyncio.get_event_loop().time() - start_time) < self.max_wait_seconds:
            try:
                status = await self.bridge.send_command(
                    "check_response_status", timeout=10
                )
            except Exception as exc:
                logger.info("Status check failed: %s", exc)
                await asyncio.sleep(self.poll_interval)
                continue

            diag = status.get("diag", {})
            footer_count = diag.get("actionFooters", 0)
            elapsed = int(asyncio.get_event_loop().time() - start_time)

            logger.info(
                "Poll: footers=%d prev=%s elapsed=%ds diag=%s",
                footer_count,
                prev_footers,
                elapsed,
                diag,
            )

            # Detect footer count INCREASING between polls.
            # During generation the count dips; when done it recovers.
            if prev_footers is not None and footer_count > prev_footers:
                logger.info(
                    "DONE! Footers went %d → %d. Returning.",
                    prev_footers,
                    footer_count,
                )
                await asyncio.sleep(0.5)
                return

            prev_footers = footer_count
            await asyncio.sleep(self.poll_interval)

        logger.warning(
            "Timed out after %ds while waiting for response.", self.max_wait_seconds
        )

    async def extract_last_code_block(
        self,
        max_retries: int = 5,
        retry_delay: float = 3.0,
    ) -> Optional[str]:
        """Extract the text of the last code block from the Grok response.

        The extension tries multiple strategies:
        1. Click the copy button on the code block
        2. Read innerText directly

        Retries up to ``max_retries`` times on transient failures (empty
        result, connection errors, or extension errors) with a delay between
        each attempt.

        Args:
            max_retries: Maximum number of extraction attempts.
            retry_delay: Seconds to wait between retries.

        Returns:
            The code block text, or None if no code blocks were found after
            all retries.
        """
        for attempt in range(1, max_retries + 1):
            try:
                result = await self.bridge.send_command(
                    "extract_last_code_block", timeout=15
                )
                text = result.get("text")
                if text:
                    method = result.get("method", "unknown")
                    logger.info(
                        "Extracted code block (%d chars) via %s (attempt %d/%d).",
                        len(text),
                        method,
                        attempt,
                        max_retries,
                    )
                    return text.strip()
                else:
                    logger.warning(
                        "No code blocks found (attempt %d/%d).",
                        attempt,
                        max_retries,
                    )
            except (RuntimeError, ConnectionError, OSError) as exc:
                logger.warning(
                    "Code block extraction failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )

            if attempt < max_retries:
                logger.info("Retrying code block extraction in %.1fs...", retry_delay)
                await asyncio.sleep(retry_delay)

        logger.warning("Could not extract code block after %d attempts.", max_retries)
        return None

    async def extract_full_response(
        self,
        max_retries: int = 5,
        retry_delay: float = 3.0,
    ) -> Optional[str]:
        """Extract the full text of the latest Grok response.

        Retries up to ``max_retries`` times on transient failures.

        Args:
            max_retries: Maximum number of extraction attempts.
            retry_delay: Seconds to wait between retries.

        Returns:
            The full response text, or None if nothing was found after all
            retries.
        """
        for attempt in range(1, max_retries + 1):
            try:
                result = await self.bridge.send_command(
                    "extract_full_response", timeout=15
                )
                text = result.get("text")
                if text:
                    logger.info(
                        "Extracted full response (%d chars, attempt %d/%d).",
                        len(text),
                        attempt,
                        max_retries,
                    )
                    return text.strip()
                else:
                    logger.warning(
                        "No response content found (attempt %d/%d).",
                        attempt,
                        max_retries,
                    )
            except (RuntimeError, ConnectionError, OSError) as exc:
                logger.warning(
                    "Full response extraction failed (attempt %d/%d): %s",
                    attempt,
                    max_retries,
                    exc,
                )

            if attempt < max_retries:
                logger.info(
                    "Retrying full response extraction in %.1fs...", retry_delay
                )
                await asyncio.sleep(retry_delay)

        logger.warning(
            "Could not extract full response after %d attempts.", max_retries
        )
        return None

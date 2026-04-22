"""Phase 7: NATS slash-command routing — data-only verification.

This phase introduces no new adapter code; Phase 4 already wires
``_on_prompt`` through ``_dispatch_command`` / ``_message_handler`` for
any prompt classified by ``_looks_like_command``. These tests pin the
invariants a fresh reviewer would need to check by hand:

* Every command name/alias advertised to gateway callers (the
  ``GATEWAY_KNOWN_COMMANDS`` set derived from ``COMMAND_REGISTRY``) is
  classified as a command by ``_looks_like_command`` — so the adapter
  won't silently route a slash command down the text-prompt path.

* Every such command resolves back to a ``CommandDef`` via
  ``resolve_command`` — proving the adapter-produced ``MessageEvent``
  will hit a known handler in ``gateway/run.py::_handle_message`` rather
  than being treated as unknown text.

* The eight commands explicitly called out in design doc §10 (and
  progress doc T7.1) are all gateway-available.

* ``/help`` renders as a single plain-text ``ResponseChunk`` over NATS —
  no attachments, no structured extras — and the payload is the real
  ``gateway_help_lines()`` output, so a future change to the help
  renderer can't silently degrade the NATS experience.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType
from gateway.platforms.nats import NatsAdapter
from hermes_cli.commands import (
    COMMAND_REGISTRY,
    GATEWAY_KNOWN_COMMANDS,
    _is_gateway_available,
    _resolve_config_gates,
    gateway_help_lines,
    resolve_command,
)


# Progress doc T7.1 / design doc §10 exemplar list. Keep these eight
# pinned explicitly even though they're a subset of
# ``GATEWAY_KNOWN_COMMANDS`` — if one of them ever gets flagged
# ``cli_only=True`` by mistake, the broader invariant would still hold
# but the exemplar case would regress silently.
DESIGN_DOC_COMMANDS = (
    "new",
    "reset",
    "model",
    "status",
    "stop",
    "help",
    "compress",
    "resume",
)


def _build_adapter() -> NatsAdapter:
    extra = {
        "servers": ["nats://127.0.0.1:4222"],
        "owner": "rene",
        "name": "gateway",
    }
    return NatsAdapter(PlatformConfig(enabled=True, extra=extra))


# ---------------------------------------------------------------------------
# T7.1 — Gateway-eligible commands route correctly
# ---------------------------------------------------------------------------


class TestDesignDocCommandsAreGatewayAvailable:
    """All eight commands called out in design doc §10 must be reachable."""

    @pytest.mark.parametrize("command", DESIGN_DOC_COMMANDS)
    def test_command_in_gateway_known_commands(self, command: str) -> None:
        assert command in GATEWAY_KNOWN_COMMANDS

    @pytest.mark.parametrize("command", DESIGN_DOC_COMMANDS)
    def test_command_resolves_via_registry(self, command: str) -> None:
        assert resolve_command(command) is not None


class TestGatewayCommandsClassifiedAsSlash:
    """Every gateway-eligible name + alias passes ``_looks_like_command``.

    Regression guard: if someone introduces a command name that starts
    with a character ``_looks_like_command`` rejects (e.g. a digit would
    pass, but a symbol wouldn't), slash-command traffic for that name
    would silently fall through to the text-agent path and spend budget
    running an LLM over a literal ``"/thing"`` prompt.
    """

    def test_every_gateway_known_command_is_classified(self) -> None:
        adapter = _build_adapter()
        misses = [
            name for name in GATEWAY_KNOWN_COMMANDS
            if not adapter._looks_like_command(f"/{name}")
        ]
        assert misses == []

    def test_every_gateway_known_command_is_classified_with_args(self) -> None:
        # Common caller shape: ``/model gpt-4o-mini``. Heuristic must not
        # choke on trailing args.
        adapter = _build_adapter()
        misses = [
            name for name in GATEWAY_KNOWN_COMMANDS
            if not adapter._looks_like_command(f"/{name} foo bar")
        ]
        assert misses == []


class TestGatewayCommandsResolveBackToCommandDef:
    """Every gateway-eligible name + alias must resolve back to a CommandDef.

    If a command is in ``GATEWAY_KNOWN_COMMANDS`` but ``resolve_command``
    returns None, the adapter emits ``MessageEvent(COMMAND)`` for it but
    ``gateway/run.py::_handle_message`` rejects it as unknown — a silent
    dispatch failure that shows up as an empty reply.
    """

    def test_all_known_commands_resolve(self) -> None:
        overrides = _resolve_config_gates()
        unresolved: list[str] = []
        for cmd in COMMAND_REGISTRY:
            if not _is_gateway_available(cmd, overrides):
                continue
            if resolve_command(cmd.name) is None:
                unresolved.append(cmd.name)
            for alias in cmd.aliases:
                if resolve_command(alias) is None:
                    unresolved.append(f"{cmd.name}:{alias}")
        assert unresolved == []


# ---------------------------------------------------------------------------
# T7.2 — /help renders as a plain-text ResponseChunk on the wire
# ---------------------------------------------------------------------------


async def _real_help_body() -> str:
    """Call the real ``GatewayRunner._handle_help_command`` and return
    its output string.

    ``_handle_help_command`` only reads ``gateway_help_lines()`` and the
    optional skill-command registry; it doesn't touch any runner state.
    ``object.__new__`` bypasses ``__init__`` to avoid building a full
    gateway just to render help text — same pattern used in
    ``tests/gateway/test_title_command.py`` and friends.
    """
    from gateway.config import Platform
    from gateway.run import GatewayRunner
    from gateway.session import SessionSource

    runner = object.__new__(GatewayRunner)
    event = MessageEvent(
        text="/help",
        message_type=MessageType.COMMAND,
        source=SessionSource(
            platform=Platform.NATS,
            user_id="alice",
            chat_id="alice",
            user_name="alice",
            chat_type="dm",
        ),
    )
    return await runner._handle_help_command(event)


class TestHelpRenderedAsPlainTextChunk:
    """``/help`` must produce one plain-text ``ResponseChunk`` — no
    attachments, no buttons, just readable text on stdout if dumped via
    ``nats sub``.

    All assertions exercise the **real** ``_handle_help_command`` output
    (via ``_real_help_body``) rather than a locally-reconstructed copy.
    If the handler ever injects ANSI escapes, adds unexpected structural
    wrapping, or emits non-UTF-8 bytes, these tests surface it before it
    reaches NATS callers.
    """

    @pytest.mark.asyncio
    async def test_dispatch_publishes_single_response_chunk(self) -> None:
        adapter = _build_adapter()
        help_body = await _real_help_body()
        adapter._message_handler = AsyncMock(return_value=help_body)

        stream = MagicMock()
        stream.send = AsyncMock()

        event = MessageEvent(
            text="/help",
            message_type=MessageType.COMMAND,
            source=adapter.build_source(chat_id="alice"),
        )

        await adapter._dispatch_command(event, stream)

        # Exactly one chunk published, no follow-ups.
        assert stream.send.await_count == 1

        chunk = stream.send.await_args.args[0]
        text = getattr(chunk, "text", None)
        assert isinstance(text, str)

        # Body contains the core entries — real gateway_help_lines data.
        assert "/help" in text
        assert "/new" in text
        assert "/stop" in text
        assert "/status" in text

        # No attachments / structured extras on a plain text response.
        # ResponseChunk in conftest records kwargs; ``attachments`` is the
        # only optional kwarg the real SDK exposes for text chunks.
        assert getattr(chunk, "attachments", None) in (None, [])

    @pytest.mark.asyncio
    async def test_help_body_is_utf8_safe_plain_text(self) -> None:
        # Emoji header and arrows should round-trip through the UTF-8
        # wire cleanly. Pin the encoding invariant on the **real**
        # handler output — any future change that injects e.g. terminal
        # escape sequences (from a misguided Rich/colorama call inside
        # ``_handle_help_command``) surfaces here before hitting NATS.
        help_body = await _real_help_body()
        encoded = help_body.encode("utf-8")
        assert encoded.decode("utf-8") == help_body
        # No ANSI escape sequences — NATS callers won't be on a TTY.
        assert "\x1b[" not in help_body
        # ``gateway_help_lines()`` must have contributed — sanity on the
        # real path being exercised (not a mysteriously empty string).
        assert "/help" in help_body
        # Used as a direct ResponseChunk payload — must not be empty.
        assert help_body.strip()

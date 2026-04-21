"""NATS gateway adapter.

Registers one ``natsagent.Agent`` at ``agents.<agent>.<owner>.<name>`` and
routes inbound NATS Agent Protocol v0.1 prompts through the gateway's
normal ``MessageEvent`` pipeline. Streams responses back chunk-by-chunk
over the reply subject; the SDK owns terminator + heartbeat emission.

Protocol spec: ``../nats-ai-pysdk/docs/nats-agent-protocol.md`` (v0.1).
Hermes architectural reference: ``docs/nats-gateway-design.md``.

Phase 2 scope (this file, as of initial landing): config parsing +
``NatsAdapter`` skeleton. Connection, streaming, attachments, and
mid-stream queries land in phases 3–6 (see
``docs/nats-gateway-progress.md``).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

try:
    import natsagent  # noqa: F401  (presence check; symbols imported lazily)
    NATSAGENT_AVAILABLE = True
except ImportError:
    NATSAGENT_AVAILABLE = False

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
)

logger = logging.getLogger(__name__)


# Defaults per docs/nats-gateway-design.md §4.
DEFAULT_AGENT = "hermes"
DEFAULT_SESSION_DEFAULT = "default"
DEFAULT_HEARTBEAT_INTERVAL_S = 30
DEFAULT_MAX_PAYLOAD = "1MB"
DEFAULT_ATTACHMENTS_OK = True
DEFAULT_ACK_KEEPALIVE_INTERVAL_S = 20

# §6.6 recommends callers default to 60 s inactivity timeout. Keep the
# adapter's keep-alive cadence strictly below that so callers never trip
# on idle disconnects while the handler is silent mid-reasoning.
MAX_ACK_KEEPALIVE_INTERVAL_S = 60

# Matches the SDK's §2.1 size grammar — a number followed by B/KB/MB/GB.
# We pre-flight the value here so bad configs fail at startup, not during
# agent construction deep in the stack trace.
_MAX_PAYLOAD_RE = re.compile(r"^\s*\d+\s*(?:B|KB|MB|GB)\s*$", re.IGNORECASE)

# SDK's §2.2 subject-token grammar for the ``agent`` field. Owner/name are
# sanitized by the SDK (base64-url fallback for non-conforming tokens), so
# we only insist on non-empty there.
_AGENT_TOKEN_RE = re.compile(r"^[a-z0-9-]+$")


def check_nats_requirements() -> bool:
    """Return True iff the ``natsagent`` SDK is importable.

    Mirrors the ``check_*_requirements`` predicate every other adapter
    exposes for ``gateway.run._create_adapter`` to short-circuit when the
    dependency is missing.
    """
    return NATSAGENT_AVAILABLE


class NatsConfigError(ValueError):
    """Raised when ``PlatformConfig.extra`` for the NATS platform is invalid.

    Surfaced via ``_set_fatal_error(retryable=False)`` in
    :meth:`NatsAdapter.__init__` so the gateway fails fast with a
    readable message instead of crashing during ``connect()``.
    """


@dataclass(frozen=True)
class NatsAdapterSettings:
    """Parsed + validated NATS adapter configuration.

    Built from ``PlatformConfig.extra`` via :meth:`from_extra`. Frozen so
    no code path can mutate the resolved settings after ``__init__``.
    """

    servers: Optional[List[str]]
    context: Optional[str]
    agent: str
    owner: str
    name: str
    session_default: str
    heartbeat_interval_s: int
    max_payload: str
    attachments_ok: bool
    ack_keepalive_interval_s: int

    @classmethod
    def from_extra(cls, extra: Dict[str, Any]) -> "NatsAdapterSettings":
        """Parse ``config.extra`` into a validated settings object.

        Raises :class:`NatsConfigError` with an actionable message on any
        validation failure; never returns a partially-populated instance.
        """
        extra = extra or {}

        servers, context = _parse_transport(extra)

        agent = _require_token(
            extra.get("agent"),
            default=DEFAULT_AGENT,
            field_name="agent",
            pattern=_AGENT_TOKEN_RE,
        )
        owner = _require_token(
            extra.get("owner"),
            default=None,
            field_name="owner",
            pattern=None,
        )
        name = _require_token(
            extra.get("name"),
            default=None,
            field_name="name",
            pattern=None,
        )

        session_default = _optional_str(
            extra.get("session_default"),
            default=DEFAULT_SESSION_DEFAULT,
            field_name="session_default",
        )

        heartbeat_interval_s = _positive_int(
            extra.get("heartbeat_interval_s"),
            default=DEFAULT_HEARTBEAT_INTERVAL_S,
            field_name="heartbeat_interval_s",
        )

        max_payload = _optional_str(
            extra.get("max_payload"),
            default=DEFAULT_MAX_PAYLOAD,
            field_name="max_payload",
        )
        if not _MAX_PAYLOAD_RE.match(max_payload):
            raise NatsConfigError(
                f"NATS: 'max_payload' {max_payload!r} is not a valid size "
                f"(expected e.g. '1MB', '512KB', '4GB')"
            )

        attachments_ok = extra.get("attachments_ok", DEFAULT_ATTACHMENTS_OK)
        if not isinstance(attachments_ok, bool):
            raise NatsConfigError(
                f"NATS: 'attachments_ok' must be a boolean, got "
                f"{type(attachments_ok).__name__}"
            )

        ack_keepalive_interval_s = _positive_int(
            extra.get("ack_keepalive_interval_s"),
            default=DEFAULT_ACK_KEEPALIVE_INTERVAL_S,
            field_name="ack_keepalive_interval_s",
        )
        if ack_keepalive_interval_s >= MAX_ACK_KEEPALIVE_INTERVAL_S:
            raise NatsConfigError(
                f"NATS: 'ack_keepalive_interval_s' ({ack_keepalive_interval_s}) "
                f"must be < {MAX_ACK_KEEPALIVE_INTERVAL_S}s — protocol §6.6 "
                f"recommends callers default to 60 s inactivity timeout, so "
                f"keep-alive needs headroom below that"
            )

        return cls(
            servers=servers,
            context=context,
            agent=agent,
            owner=owner,
            name=name,
            session_default=session_default,
            heartbeat_interval_s=heartbeat_interval_s,
            max_payload=max_payload,
            attachments_ok=attachments_ok,
            ack_keepalive_interval_s=ack_keepalive_interval_s,
        )

    @property
    def identity(self) -> str:
        """Stable lock identity ``{agent}:{owner}:{name}``.

        Used by :meth:`NatsAdapter.connect` (Phase 3) to scope the
        ``acquire_scoped_lock`` call per design doc §5.
        """
        return f"{self.agent}:{self.owner}:{self.name}"


def _parse_transport(extra: Dict[str, Any]) -> tuple[Optional[List[str]], Optional[str]]:
    """Extract (servers, context) from extra, enforcing exactly-one."""
    raw_servers = extra.get("servers")
    raw_context = extra.get("context")

    servers: Optional[List[str]] = None
    context: Optional[str] = None

    has_servers = raw_servers not in (None, "", [])
    has_context = raw_context not in (None, "")

    if has_servers and has_context:
        raise NatsConfigError(
            "NATS: specify either 'servers' or 'context', not both"
        )
    if not has_servers and not has_context:
        raise NatsConfigError(
            "NATS: exactly one of 'servers' (list of URLs) or 'context' "
            "(nats CLI context name) is required"
        )

    if has_servers:
        if isinstance(raw_servers, str):
            candidates = [raw_servers]
        elif isinstance(raw_servers, (list, tuple)):
            candidates = list(raw_servers)
        else:
            raise NatsConfigError(
                f"NATS: 'servers' must be a string or list of strings, "
                f"got {type(raw_servers).__name__}"
            )
        servers = [str(s).strip() for s in candidates if str(s).strip()]
        if not servers:
            raise NatsConfigError(
                "NATS: 'servers' must contain at least one non-empty URL"
            )

    if has_context:
        if not isinstance(raw_context, str):
            raise NatsConfigError(
                f"NATS: 'context' must be a string, got "
                f"{type(raw_context).__name__}"
            )
        context = raw_context.strip()
        if not context:
            raise NatsConfigError("NATS: 'context' must be non-empty")

    return servers, context


def _require_token(
    value: Any,
    default: Optional[str],
    field_name: str,
    pattern: Optional[re.Pattern[str]],
) -> str:
    """Return a stripped non-empty token, applying ``default`` if unset.

    If ``pattern`` is given, fail fast when the supplied value doesn't
    match — used to catch invalid ``agent`` tokens before the SDK's own
    :class:`AgentSubject.new` surfaces the same error from deeper in the
    stack.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        if default is None:
            raise NatsConfigError(f"NATS: '{field_name}' is required")
        value = default

    if not isinstance(value, str):
        raise NatsConfigError(
            f"NATS: '{field_name}' must be a string, got "
            f"{type(value).__name__}"
        )
    stripped = value.strip()
    if pattern is not None and not pattern.fullmatch(stripped):
        raise NatsConfigError(
            f"NATS: '{field_name}' {stripped!r} must match {pattern.pattern} "
            f"(protocol §2.2)"
        )
    return stripped


def _optional_str(value: Any, default: str, field_name: str) -> str:
    if value is None:
        return default
    if not isinstance(value, str):
        raise NatsConfigError(
            f"NATS: '{field_name}' must be a string, got {type(value).__name__}"
        )
    stripped = value.strip()
    if not stripped:
        return default
    return stripped


def _positive_int(value: Any, default: int, field_name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        # bool is a subclass of int — reject it explicitly to avoid silent
        # coercion of ``True`` to ``1``.
        raise NatsConfigError(
            f"NATS: '{field_name}' must be an integer, got bool"
        )
    try:
        coerced = int(value)
    except (TypeError, ValueError) as exc:
        raise NatsConfigError(
            f"NATS: '{field_name}' must be an integer, got {value!r}"
        ) from exc
    if coerced <= 0:
        raise NatsConfigError(
            f"NATS: '{field_name}' must be positive, got {coerced}"
        )
    return coerced


class NatsAdapter(BasePlatformAdapter):
    """Gateway adapter for the NATS Agent Protocol v0.1.

    Phase 2 skeleton — parses settings and registers as a platform.
    ``connect()``, ``send()``, and the streaming pipeline land in phases
    3 and 4; see ``docs/nats-gateway-design.md`` §6 and
    ``docs/nats-gateway-progress.md`` for the current state.
    """

    def __init__(self, config: PlatformConfig) -> None:
        super().__init__(config, Platform.NATS)

        # Per-chat PromptStream handles. Populated by Phase 4's
        # ``_on_prompt`` and consulted by ``send()`` / attachment helpers.
        # Initialised here so later phases can assume the attribute exists
        # regardless of whether ``connect()`` ran.
        self._active_streams: Dict[str, Any] = {}
        self._nc: Optional[Any] = None
        self._agent: Optional[Any] = None
        self._settings: Optional[NatsAdapterSettings] = None

        try:
            self._settings = NatsAdapterSettings.from_extra(config.extra or {})
        except NatsConfigError as exc:
            self._set_fatal_error(
                "nats_config_error",
                str(exc),
                retryable=False,
            )
            logger.error("[%s] %s", self.name, exc)

    # ------------------------------------------------------------------
    # Abstract methods — Phase 2 stubs. Real implementations in Phase 3+.
    #
    # Stubs intentionally fail *gracefully* rather than raising: the
    # gateway's connect_all() loop (gateway/run.py:2076) treats
    # ``connect() returning False + has_fatal_error + retryable=False``
    # as a permanent-fatal state and won't schedule 30-s reconnect
    # attempts, so a partially-landed NATS integration doesn't spam
    # the gateway log until Phase 3 ships.
    # ------------------------------------------------------------------

    async def connect(self) -> bool:
        self._set_fatal_error(
            "nats_not_implemented",
            "NATS adapter connect() is not yet implemented "
            "(Phase 3 — see docs/nats-gateway-progress.md)",
            retryable=False,
        )
        return False

    async def disconnect(self) -> None:
        # Idempotent no-op. Safe to call before Phase 3 lands, and
        # mirrors the contract Phase 3's real disconnect() will uphold
        # (no partial state to clean up when connect() never succeeded).
        return None

    async def send(  # pragma: no cover — Phase 4
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return SendResult(
            success=False,
            error="NATS send() is not yet implemented (Phase 4)",
        )

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        # Phase 3 wires this into the gateway's session machinery; the
        # return shape is already final (design doc §3) so landing it in
        # the skeleton doesn't block other work.
        return {"name": chat_id, "type": "dm"}

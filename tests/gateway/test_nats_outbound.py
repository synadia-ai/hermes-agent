"""Phase 5 (T5.5): outbound attachments + race-safe stream lookup.

Covers:

* ``send_image_file`` / ``send_document`` / ``send_voice`` / ``send_video``
  — each wraps the file in an :class:`natsagent.Attachment` and publishes
  one :class:`ResponseChunk` carrying the caption as ``text`` and the
  attachment in ``attachments``. Missing paths surface as
  ``SendResult(success=False)`` rather than raising.
* ``send_document`` ``file_name`` override — honors the caller's explicit
  wire-filename instead of ``Path(file_path).name``, which callers use
  when staged files live under a content hash but should reach the
  recipient under their original name.
* Race-safe lookup (T5.0) — the ``_current_stream`` contextvar resolves
  to the handler's own stream even when a later prompt overwrites
  ``_active_streams`` for the same chat_id; send helpers fired from the
  earlier handler's context still land on the earlier handler's reply
  subject.
* Concurrent-``x-session`` regression test — two overlapping
  ``_on_prompt`` invocations with the same chat_id each fire send
  helpers, and each lands on its own stream. This is the T5.0 guard.

The conftest's ``_ensure_natsagent_mock`` installs a ``_FakeAttachment``
that records ``filename`` on construction; tests assert on
``chunk.attachments[0].filename`` to verify the adapter wrapped the
file correctly.
"""

from __future__ import annotations

import asyncio
import sys
from contextvars import copy_context
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.nats import NatsAdapter, _current_stream


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


def _valid_extra(**overrides) -> dict:
    base = {
        "servers": ["nats://127.0.0.1:4222"],
        "owner": "rene",
        "name": "gateway",
        "ack_keepalive_interval_s": 1,
    }
    base.update(overrides)
    return base


def _build_adapter(**extra_overrides) -> NatsAdapter:
    return NatsAdapter(PlatformConfig(enabled=True, extra=_valid_extra(**extra_overrides)))


def _fake_stream() -> MagicMock:
    stream = MagicMock()
    stream.send = AsyncMock()
    request = MagicMock()
    request.data = b""
    stream._request = request
    return stream


@pytest.fixture
def tmp_file(tmp_path: Path):
    """Return a factory that writes a named file under tmp_path and returns its Path."""

    def _make(name: str = "report.pdf", content: bytes = b"%PDF-1.4 fake content") -> Path:
        p = tmp_path / name
        p.write_bytes(content)
        return p

    return _make


@pytest.fixture
def ensure_contextvar_reset():
    """Ensure no test leaks a bound ``_current_stream`` into the next one.

    Setting ``_current_stream`` outside a ``_on_prompt`` context (which
    tests in this module do to exercise the race-safe path) without the
    matching ``.reset(token)`` would pollute follow-up tests running in
    the same xdist worker. This fixture captures and restores the state.
    """
    original = _current_stream.get()
    yield
    # Re-set rather than reset — ``reset(token)`` requires the token from
    # the matching set(), which tests may or may not have produced.
    _current_stream.set(original)


# ---------------------------------------------------------------------------
# send_image_file
# ---------------------------------------------------------------------------


class TestSendImageFile:
    @pytest.mark.asyncio
    async def test_publishes_response_chunk_with_attachment(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        path = tmp_file("photo.png", b"\x89PNG\r\n\x1a\nfake")
        result = await adapter.send_image_file(
            chat_id="alice",
            image_path=str(path),
            caption="look at this",
        )

        assert result.success is True
        assert result.message_id
        stream.send.assert_awaited_once()
        chunk = stream.send.await_args.args[0]
        assert getattr(chunk, "text", "") == "look at this"
        attachments = getattr(chunk, "attachments", None) or []
        assert len(attachments) == 1
        # _FakeAttachment records filename on ``from_path(str(path))`` —
        # the test mock stores the full path; real SDK would strip to
        # basename. Either form is acceptable — we just need the file
        # reference to survive the wrap.
        assert "photo" in getattr(attachments[0], "filename", "")

    @pytest.mark.asyncio
    async def test_returns_failure_when_no_active_stream(self, tmp_file):
        adapter = _build_adapter()
        path = tmp_file("photo.png")
        result = await adapter.send_image_file(
            chat_id="nobody", image_path=str(path)
        )
        assert result.success is False
        assert "no active NATS stream" in (result.error or "")

    @pytest.mark.asyncio
    async def test_returns_failure_when_path_missing(self):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        result = await adapter.send_image_file(
            chat_id="alice", image_path="/does/not/exist.png"
        )
        assert result.success is False
        assert "not found" in (result.error or "")
        # Critically — no partial send: the attachment build must fail
        # before any stream.send fires, otherwise the caller gets a
        # half-chunk in the response log.
        stream.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_caption_defaults_to_empty_string(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        path = tmp_file("cat.jpg", b"\xff\xd8\xff\xe0 JPEG-ish")
        await adapter.send_image_file(chat_id="alice", image_path=str(path))

        chunk = stream.send.await_args.args[0]
        # ResponseChunk(text="") is the contract — no caller should get
        # a ``None`` text field where "" is the protocol-correct default.
        assert getattr(chunk, "text", None) == ""


# ---------------------------------------------------------------------------
# send_document
# ---------------------------------------------------------------------------


class TestSendDocument:
    @pytest.mark.asyncio
    async def test_publishes_response_chunk_with_attachment(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        path = tmp_file("report.pdf", b"%PDF-fake")
        result = await adapter.send_document(
            chat_id="alice",
            file_path=str(path),
            caption="quarterly numbers",
        )

        assert result.success is True
        chunk = stream.send.await_args.args[0]
        assert getattr(chunk, "text", "") == "quarterly numbers"
        attachments = getattr(chunk, "attachments", None) or []
        assert len(attachments) == 1

    @pytest.mark.asyncio
    async def test_file_name_overrides_wire_filename(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        # Staged under a hash, but the user should see "Summary.pdf".
        staged = tmp_file("abc123deadbeef", b"%PDF-fake")
        result = await adapter.send_document(
            chat_id="alice",
            file_path=str(staged),
            file_name="Summary.pdf",
        )

        assert result.success is True
        chunk = stream.send.await_args.args[0]
        attachment = (chunk.attachments or [None])[0]
        assert attachment is not None
        # The conftest's _FakeAttachment.from_bytes pins filename verbatim
        # — we used from_bytes because from_path would have forced the
        # staged name. Verify the override landed.
        assert getattr(attachment, "filename", "") == "Summary.pdf"

    @pytest.mark.asyncio
    async def test_returns_failure_when_path_missing(self):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        result = await adapter.send_document(
            chat_id="alice", file_path="/missing.docx"
        )
        assert result.success is False
        assert "not found" in (result.error or "")
        stream.send.assert_not_awaited()


# ---------------------------------------------------------------------------
# send_voice / send_video
# ---------------------------------------------------------------------------


class TestSendVoiceVideo:
    @pytest.mark.asyncio
    async def test_send_voice_publishes_attachment(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        path = tmp_file("note.ogg", b"OggS fake")
        result = await adapter.send_voice(
            chat_id="alice", audio_path=str(path), caption="voice memo"
        )

        assert result.success is True
        chunk = stream.send.await_args.args[0]
        assert getattr(chunk, "text", "") == "voice memo"
        assert len(chunk.attachments or []) == 1

    @pytest.mark.asyncio
    async def test_send_video_publishes_attachment(self, tmp_file):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        path = tmp_file("clip.mp4", b"\x00\x00\x00 fake mp4")
        result = await adapter.send_video(
            chat_id="alice", video_path=str(path), caption="demo"
        )

        assert result.success is True
        chunk = stream.send.await_args.args[0]
        assert getattr(chunk, "text", "") == "demo"
        assert len(chunk.attachments or []) == 1


# ---------------------------------------------------------------------------
# Race-safe lookup — T5.0
# ---------------------------------------------------------------------------


class TestResolveStream:
    def test_contextvar_wins_over_dict_lookup(self, ensure_contextvar_reset):
        """When a handler's contextvar is set, it bypasses the dict —
        even if the dict has a stale / different entry for the same chat_id."""
        adapter = _build_adapter()
        handler_stream = _fake_stream()
        other_stream = _fake_stream()
        adapter._active_streams[("alice", id(other_stream))] = other_stream

        # Simulate being inside the handler context.
        token = _current_stream.set(handler_stream)
        try:
            resolved = adapter._resolve_stream("alice")
        finally:
            _current_stream.reset(token)

        assert resolved is handler_stream

    def test_dict_fallback_used_when_contextvar_unset(self, ensure_contextvar_reset):
        adapter = _build_adapter()
        stream = _fake_stream()
        adapter._active_streams[("alice", id(stream))] = stream

        assert _current_stream.get() is None
        assert adapter._resolve_stream("alice") is stream

    def test_returns_none_when_nothing_registered(self, ensure_contextvar_reset):
        adapter = _build_adapter()
        assert _current_stream.get() is None
        assert adapter._resolve_stream("nobody") is None

    def test_dict_lookup_ignores_other_chat_ids(self, ensure_contextvar_reset):
        adapter = _build_adapter()
        stream_a = _fake_stream()
        stream_b = _fake_stream()
        adapter._active_streams[("alice", id(stream_a))] = stream_a
        adapter._active_streams[("bob", id(stream_b))] = stream_b

        assert adapter._resolve_stream("alice") is stream_a
        assert adapter._resolve_stream("bob") is stream_b


# ---------------------------------------------------------------------------
# Concurrent x-session regression — the T5.0 regression guard
# ---------------------------------------------------------------------------


class TestConcurrentSameSessionRegression:
    """Two overlapping ``_on_prompt`` calls sharing a ``chat_id`` each
    fire a send helper; each send must land on its own stream.

    Pre-T5.0, ``_active_streams[chat_id] = stream`` would be overwritten
    by the second handler so the first handler's send would route to
    the second handler's reply subject. Post-T5.0, the handler-scoped
    contextvar ensures each send stays on its own stream regardless of
    dict overwrite order.
    """

    @pytest.mark.asyncio
    async def test_two_handlers_one_session_send_to_own_streams(
        self, monkeypatch, tmp_file
    ):
        adapter = _build_adapter()

        # Two distinct streams arriving with the same x-session="shared".
        stream_a = _fake_stream()
        stream_b = _fake_stream()

        envelope_a = MagicMock()
        envelope_a.prompt = "prompt A"
        envelope_a.attachments = None
        envelope_b = MagicMock()
        envelope_b.prompt = "prompt B"
        envelope_b.attachments = None

        # Plumb the x-session hack — _extract_session reads
        # stream._request.data and we want both to resolve to "shared".
        stream_a._request.data = b'{"prompt":"prompt A","x-session":"shared"}'
        stream_b._request.data = b'{"prompt":"prompt B","x-session":"shared"}'

        # Gate both handlers so they overlap — handler A blocks while B
        # starts, then B's send fires, then A's send fires, then both
        # unblock. This proves the handler-scoped contextvar doesn't
        # depend on strict ordering.
        start_a = asyncio.Event()
        start_b = asyncio.Event()
        a_sent_image = asyncio.Event()
        b_sent_image = asyncio.Event()
        release_a = asyncio.Event()

        path_a = tmp_file("a.png", b"\x89PNGfakeA")
        path_b = tmp_file("b.png", b"\x89PNGfakeB")

        async def _run_a(event, s, chat_id):
            start_a.set()
            # Wait until B has registered + sent its chunk so both
            # streams are live in _active_streams at once.
            await start_b.wait()
            await b_sent_image.wait()
            # Now from handler A's context, fire a send. If the
            # contextvar lookup is correct, this lands on stream_a.
            result = await adapter.send_image_file(
                chat_id="shared", image_path=str(path_a), caption="A"
            )
            assert result.success is True
            a_sent_image.set()
            await release_a.wait()

        async def _run_b(event, s, chat_id):
            start_b.set()
            # Ensure A is already in flight before we send — otherwise
            # there's no race to test.
            await start_a.wait()
            result = await adapter.send_image_file(
                chat_id="shared", image_path=str(path_b), caption="B"
            )
            assert result.success is True
            b_sent_image.set()

        # Flip the branch — _on_prompt dispatches text prompts through
        # _run_text_prompt; we swap in our per-handler runners. We have
        # to swap globally, so key on the stream identity passed in.
        dispatch = {id(stream_a): _run_a, id(stream_b): _run_b}

        async def _fake_run(event, s, chat_id):
            await dispatch[id(s)](event, s, chat_id)

        monkeypatch.setattr(adapter, "_run_text_prompt", _fake_run)

        # Launch both handlers concurrently.
        task_a = asyncio.create_task(adapter._on_prompt(envelope_a, stream_a))
        task_b = asyncio.create_task(adapter._on_prompt(envelope_b, stream_b))

        # B completes once its image send lands; then release A.
        await asyncio.wait_for(task_b, timeout=3.0)
        release_a.set()
        await asyncio.wait_for(task_a, timeout=3.0)

        # Now the regression assertion: each stream received exactly ONE
        # ResponseChunk (its own image), NOT the other handler's image.
        assert stream_a.send.await_count == 1
        assert stream_b.send.await_count == 1

        chunk_a = stream_a.send.await_args.args[0]
        chunk_b = stream_b.send.await_args.args[0]
        assert getattr(chunk_a, "text", None) == "A"
        assert getattr(chunk_b, "text", None) == "B"

        # Registration cleanup — both compound keys are popped.
        assert adapter._active_streams == {}
        # And the contextvar is reset on both handlers' exits.
        assert _current_stream.get() is None


# ---------------------------------------------------------------------------
# format_message — T5.4 (no-op)
# ---------------------------------------------------------------------------


class TestFormatMessage:
    def test_returns_content_verbatim(self):
        adapter = _build_adapter()
        assert adapter.format_message("hello **world**") == "hello **world**"

    def test_empty_string_passthrough(self):
        adapter = _build_adapter()
        assert adapter.format_message("") == ""

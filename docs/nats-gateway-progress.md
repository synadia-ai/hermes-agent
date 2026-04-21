# NATS Gateway — Progress Tracker

**Purpose of this file.** This is the single source of truth for "where are we in the NATS gateway implementation?" across context-cleared sessions. `TaskList` state **might** survive `/clear` but I am not betting on it — this file does.

---

## If you are reading this in a fresh session

You (Claude) are resuming work on the NATS gateway channel for Hermes Agent. The user has cleared the context between phases on purpose. Do this, in order:

1. **Read `docs/nats-gateway-design.md` in full.** It is the architectural reference — protocol↔adapter mapping, streaming model, session identity, lock scope, approval hook design, failure modes. ~650 lines. Everything there has been reviewed and approved by the user.
2. **Read this file to the end.** The "Status" section tells you the last completed phase; the "Task checklist" tells you exactly which `T#.#` items are done vs. pending; the "Decision log" captures anything decided mid-flight that is not in the design doc.
3. **Call `TaskList`.** If the task list is empty or out of sync with this file's checkboxes, treat this file as authoritative and recreate the tasks via `TaskCreate` (titles, descriptions, and `activeForm` values are listed below in "Task definitions reference" — copy verbatim).
4. **Pick up at the next `[ ]` task** and work through the current phase's items. Do not skip ahead into later phases — the phases have dependencies and the user wants phase-boundary reviews.
5. **At end of phase**, follow the "End-of-phase ritual" below. **Do not clear context yourself** — that is the user's call. Just report that the phase is done and the ritual is complete.

Do not rewrite the design doc unless the user asks. If a design decision turns out wrong during implementation, append a note to this file's **Decision log** and flag it to the user before proceeding.

---

## Status

- **Last completed phase:** Phase 4 — Inbound path (T4.1 through T4.4)
- **Next phase:** Phase 5 — Outbound attachments & formatting (T5.0 through T5.5). **T5.0 is a Phase-4-shortcoming #1 fix folded in as a prerequisite** — see the "Fold-in justification" note at the top of Phase 5 below.
- **Branch:** `nats-gateway` (feature branch; PR target is `main`)
- **Known blockers:** none
- **Open design questions pending user input:** 4 items listed in §16 of `docs/nats-gateway-design.md`. Default answers are noted there; proceed with defaults unless the user redirects.

When you finish a phase, update the two bullets above and tick its tasks in the "Task checklist" below.

---

## Phase-by-phase task checklist

Tick the box when the task is complete. One authoritative list; do not let TaskList drift from this file.

### Phase 0 — Docs first

- [x] **T0.1** — Write `docs/nats-gateway-design.md`
- [x] **T0.2** — Add CLAUDE.md pointers to design doc + natsagent SDK location

### Phase 1 — Scaffolding & config

- [x] **T1.1** — Add `Platform.NATS` enum value in `gateway/config.py`
- [x] **T1.2** — Extend `_apply_env_overrides()` for NATS (NATS_URL, NATS_CONTEXT, HERMES_NATS_{AGENT,OWNER,NAME,SESSION})
- [x] **T1.3** — Extend `get_connected_platforms()` for NATS (enabled AND (servers OR context))
- [x] **T1.4** — Register NATS adapter in `_create_adapter()` (gateway/run.py ~line 2717)
- [x] **T1.5** — Add `natsagent` to `pyproject.toml` extras (deferred `all`-extra inclusion — see Decision log 2026-04-21)
- [x] **T1.6** — Add `_ensure_natsagent_mock()` in `tests/gateway/conftest.py`

### Phase 2 — Adapter skeleton

- [x] **T2.1** — Create `gateway/platforms/nats.py` skeleton (`check_nats_requirements`, `NatsAdapter` stub, `NatsAdapterSettings` dataclass, validation)
- [x] **T2.2** — `tests/gateway/test_nats_config.py` — config parsing (happy/bad/env-override)

### Phase 3 — Connection & lifecycle

- [x] **T3.1** — Implement `NatsAdapter.connect()` (lock, natsagent.connect, Agent.start, _mark_connected)
- [x] **T3.2** — Implement `NatsAdapter.disconnect()` (idempotent, cancel handlers, agent.stop, nc.close, release lock)
- [x] **T3.3** — Implement `get_chat_info()` (returns `{"name": chat_id, "type": "dm"}`)
- [x] **T3.4** — `tests/gateway/test_nats_connect.py` — connect/disconnect/lock/handler registration

### Phase 4 — Inbound path (the meaty one; plan for a dedicated session)

- [x] **T4.1** — Implement `_on_prompt(envelope, stream)` — x-session → chat_id, attachments → media cache, MessageEvent, `_active_streams[chat_id] = stream`, keep-alive task, command-vs-text branch, cleanup
- [x] **T4.2** — Wire `_active_streams` into `send()` — look up PromptStream, `stream.send(ResponseChunk(text=content))`, return SendResult
- [x] **T4.3** — Wire streaming deltas — adapter-owned AIAgent with `stream_delta_callback` forwarding to a queue → pump → `stream.send`. (Ownership decision: adapter owns the callback; see §6.1 of design doc.)
- [x] **T4.4** — `tests/gateway/test_nats_inbound.py` — envelope in, MessageEvent, deltas emitted, keep-alive fires, terminator, attachment round-trip

### Phase 5 — Outbound attachments & formatting

**Fold-in justification (T5.0).** Phase 4's "known shortcoming #1" — concurrent prompts on the same `x-session` overwrite `_active_streams[chat_id]` so tool outputs from handler A can land on handler B's reply subject — was deliberately deferred at Phase 4's close. Phase 5 adds four new tool-accessible methods (`send_image_file` / `send_document` / `send_voice` / `send_video`) that all resolve through `_active_streams[chat_id]`, so the blast radius of the race quadruples in this phase. Fixing it here (before the send helpers) costs less than retrofitting later — build the send helpers on a race-safe lookup rather than patching four call sites after the fact.

- [ ] **T5.0** — Race-safe stream lookup. Options (pick one during implementation):
  * **(a) Pass-through:** plumb the handler's own `PromptStream` into tool calls via a contextvar / `asyncio.TaskGroup`-scoped state, so each tool fires on *its* handler's stream regardless of `_active_streams` state. Cleanest but touches the tool dispatch surface.
  * **(b) Compound key:** key `_active_streams` by `(chat_id, stream_id)` (e.g. id of the PromptStream object) and have send helpers look up through the caller's captured handler context rather than chat_id alone.
  * **(c) Per-chat_id stack:** `_active_streams[chat_id]` becomes a `list[PromptStream]`; `_on_prompt` pushes on entry, pops on exit, send helpers use the top-of-stack. Simplest change but LIFO semantics are wrong for legitimate concurrent sessions — second handler's sends would go to the first stream until the first exits. Reject unless (a)/(b) prove too invasive.
  * **Recommendation:** start with (b). The other two are heavier lifts; (b) is a 2-line dict shape change + one call-site update per send helper.
- [ ] **T5.1** — Implement `send_image_file` (`Attachment.from_path(path)` → `stream.send(ResponseChunk(text=caption, attachments=[...]))`), built on the T5.0 race-safe lookup.
- [ ] **T5.2** — Implement `send_document` (same pattern, generic file)
- [ ] **T5.3** — Implement `send_voice` / `send_video` (same pattern; v0.1 doesn't distinguish on wire)
- [ ] **T5.4** — `format_message()` override (no-op for symmetry — NATS carries text verbatim; already landed in Phase 4 as a class method, but confirm it's wired into the same call paths Phase 5 uses)
- [ ] **T5.5** — `tests/gateway/test_nats_outbound.py` — image/doc/voice → ResponseChunk.attachments[0] shape, **plus a concurrent-x-session regression test**: two overlapping `_on_prompt` invocations with the same chat_id, each firing a send helper, must land on their own streams respectively (this is the T5.0 regression guard and belongs in the outbound test file because the send helpers are where the race becomes observable).

### Phase 6 — Mid-stream queries (NATS-local)

- [ ] **T6.1** — Survey `_pending_approvals` usage in `gateway/run.py` + read `hermes_cli/callbacks.py` (research spike; notes into Decision log)
- [ ] **T6.2** — Add `async def request_interaction(self, chat_id, prompt, *, kind, timeout) -> str | None` on `BasePlatformAdapter` (default raises NotImplementedError); NATS implementation calls `stream.ask(prompt, timeout=timeout)`
- [ ] **T6.3** — In `gateway/run.py`, route approval callback through `adapter.request_interaction` when adapter overrides the base default (capability check via `type(adapter).request_interaction is not BasePlatformAdapter.request_interaction`). Preserve existing behavior for non-NATS adapters.
- [ ] **T6.4** — `tests/gateway/test_nats_query.py` — approval callback triggers Query chunk; simulate caller reply; agent resumes

### Phase 7 — Slash commands

- [ ] **T7.1** — Confirm gateway-eligible commands (`/new`, `/reset`, `/model`, `/status`, `/stop`, `/help`, `/compress`, `/resume`) route as `MessageEvent(COMMAND)` with no new code. Data-only verification.
- [ ] **T7.2** — Manually verify `/help` output renders sensibly as plain-text chunks over NATS

### Phase 8 — End-to-end verification (manual; requires local nats-server)

- [ ] **T8.1** — Local nats-server + hermes smoke config (already documented in §14 of design doc; confirm it still applies)
- [ ] **T8.2** — `examples/01-discover.py` lists `agents.hermes.<owner>.<name>`
- [ ] **T8.3** — `examples/02-prompt-text.py` — simple prompt streams a response
- [ ] **T8.4** — `examples/03-prompt-attachment.py` — hermes ingests a PDF and streams a summary
- [ ] **T8.5** — `examples/04-query-reply.py` — tool call that requires approval; Query chunk; reply "yes"; stream resumes
- [ ] **T8.6** — `examples/05-liveness.py` in background; kill hermes; `is_online()` flips False after 3× interval
- [ ] **T8.7** — `nats` CLI interop — `nats req '$SRV.INFO.Synadia Agents'` and `nats sub 'agents.hermes.*.*.heartbeat'` per protocol Appendix C
- [ ] **T8.8** — `scripts/run_tests.sh` — full suite green

### Phase 9 — Polish & docs

- [ ] **T9.1** — Update `gateway/platforms/ADDING_A_PLATFORM.md` with any new integration points that emerged (e.g. `request_interaction` if it gets generalized)
- [ ] **T9.2** — Append "Lessons learned" section to `docs/nats-gateway-design.md` (especially surprises in stream_delta_callback wiring or attachments)
- [ ] **T9.3** — Add example config snippet to README or new `docs/nats-gateway.md` (user-facing)

---

## End-of-phase ritual

Run this every time a phase's tasks are all ticked off. **In order.**

1. **Run the relevant test subset** via `scripts/run_tests.sh`:
   - Phase 2: `scripts/run_tests.sh tests/gateway/test_nats_config.py`
   - Phase 3: `scripts/run_tests.sh tests/gateway/test_nats_connect.py`
   - Phase 4: `scripts/run_tests.sh tests/gateway/test_nats_inbound.py`
   - Phase 5: `scripts/run_tests.sh tests/gateway/test_nats_outbound.py`
   - Phase 6: `scripts/run_tests.sh tests/gateway/test_nats_query.py`
   - Phase 8: `scripts/run_tests.sh` (full suite)
   - Other phases: no dedicated tests — skip
2. **Update this file's "Status" block** — bump `Last completed phase` and `Next phase`.
3. **Tick any remaining `[ ]` boxes** for the just-completed phase. Scan the list for drift vs. TaskList.
4. **Append to the Decision log** if anything was decided mid-flight (API tweaks, discovered surprises, deferred items).
5. **Run `TaskList`** and ensure its state matches this file. If divergent, update TaskList — this file is authoritative.
6. **Commit the phase** on the `nats-gateway` branch. Stage only the files touched by this phase (including this progress doc's updates from steps 2–4). Message format: `feat(gateway): <short phase summary> (phase N)`; body lists what changed and any decisions worth surfacing to a reviewer. Use the standard Claude Code `Co-Authored-By` trailer. Do NOT push and do NOT use `--no-verify`.
7. **Report to the user:** "Phase N done. Commit: `<short SHA>`. Tests: `<passing>`. Ready for review / context clear."
8. **Do not push or clear context yourself.** Those remain the user's call.

---

## Decision log (append-only)

Use this to capture non-obvious decisions made during implementation — things a fresh session wouldn't know from reading the design doc alone. New entries at the bottom; include date + phase.

### 2026-04-21 — Phase 0 — Progress doc + CLAUDE.md pointer added

Design doc + this progress doc now exist. CLAUDE.md updated to point at both so fresh sessions pick them up automatically. No architectural decisions changed.

### 2026-04-21 — Phase 1 — `natsagent` deliberately NOT added to the `all` extra

T1.5 says "add `natsagent` to pyproject.toml extras (and the `all` extra)." The `nats` extra is in place (`natsagent>=0.1.0,<1`) but it was **not** added to the `all` extra. Reason: `natsagent` is not yet published to PyPI (the design doc §14 acknowledges this — local install is `pip install -e ../nats-ai-pysdk`). Adding a non-PyPI dep to `all` would break `pip install 'hermes-agent[all]'` for every user doing the standard onboarding install. Reverse this once the SDK ships on PyPI — one line to add `"hermes-agent[nats]"` to the `all` list in `pyproject.toml`.

### 2026-04-21 — Phase 1 — Env overrides trigger `enabled=True` on any NATS env var

`_apply_env_overrides()` creates/enables the NATS platform entry if *any* of `NATS_URL`, `NATS_CONTEXT`, `HERMES_NATS_{AGENT,OWNER,NAME,SESSION}` is set. This matches Signal's "any creds env present ⇒ enable" pattern (`gateway/config.py:926-943`). Note that `get_connected_platforms()` still gates on `enabled AND (servers OR context)` — so setting only `HERMES_NATS_OWNER` without `NATS_URL`/`NATS_CONTEXT` enables the platform but it won't show as connected. That's intentional: lets you pre-populate identity via env and complete config via YAML.

### 2026-04-21 — Phase 1 — Pre-existing test failures observed, not introduced by Phase 1

`scripts/run_tests.sh tests/gateway/` reports two failures on clean `main` (verified by stashing Phase 1 changes): `test_agent_cache.py::TestAgentCacheIdleResume::test_close_vs_release_full_teardown_difference` and `test_matrix.py::TestMatrixUploadAndSend::test_upload_encrypted_room_uses_file_payload`. These are pre-existing, unrelated to NATS work. Flagged here so a future phase doesn't blame them on NATS changes. `tests/gateway/test_config.py` (the most directly relevant test file) passes cleanly after Phase 1 edits.

### 2026-04-21 — Phase 2 — `max_payload` validated via local regex, not the SDK's `parse_human_bytes`

The design doc §4 calls for pre-flighting `max_payload` through `natsagent._bytes.parse_human_bytes`. The adapter uses a local regex (`_MAX_PAYLOAD_RE`) instead. Reason: the SDK's `_bytes` module is private/underscored, and the gateway test harness installs `natsagent` as a `MagicMock` — `from natsagent._bytes import parse_human_bytes` can't resolve on a MagicMock module, so the check would either crash or no-op silently under test. A local regex matches the §2.1 grammar ("positive integer followed by B/KB/MB/GB") and keeps the validation deterministic whether the real SDK or the mock is loaded. The SDK still re-validates at `Agent(...)` construction time in Phase 3, so this is belt-and-braces rather than belt-only.

### 2026-04-21 — Phase 2 — `agent` token strictly validated; `owner`/`name` deferred to SDK

`NatsAdapterSettings.from_extra` enforces the §2.2 regex (`^[a-z0-9-]+$`) on the `agent` token but only insists on non-empty for `owner` / `name`. Reason: the SDK's `AgentSubject._sanitize()` base64-url-escapes non-conforming owner/name tokens rather than rejecting them, so a strict regex here would reject inputs the SDK would have accepted. The `agent` token has no such fallback — the SDK rejects it outright — so failing fast in our settings parser gives a cleaner error message than the SDK's exception surfacing from inside `connect()`.

### 2026-04-21 — Phase 2 — `bool` rejected for integer fields

Plain `int(True) == 1` would silently pass `heartbeat_interval_s`/`ack_keepalive_interval_s` validation. `_positive_int` rejects `bool` explicitly — a YAML `heartbeat_interval_s: true` is always a mistake, and surfacing it as a config error beats emitting heartbeats every 1 s in production.

### 2026-04-21 — Phase 2 — `_active_streams`/`_nc`/`_agent` initialised on adapter regardless of config validity

Even when `NatsAdapterSettings.from_extra` fails, `NatsAdapter.__init__` initialises `_active_streams = {}`, `_nc = None`, `_agent = None`. Reason: Phase 3's `connect()` and Phase 4's `send()` assume these attributes exist. If a fatal-error adapter somehow reaches later-phase code (e.g. GatewayRunner still calling `get_chat_info()` on it), `AttributeError` would be a harder failure than "not connected". Cheap guard, no downside.

### 2026-04-21 — Phase 3 — Conftest mock: `nc.close` explicitly made awaitable

`tests/gateway/conftest.py::_ensure_natsagent_mock` now wires `mod.connect.return_value.close = AsyncMock()`. Background: `mod.connect = AsyncMock()` returns a MagicMock when awaited, and a MagicMock's `.close()` returns another MagicMock — which can't be `await`-ed. `NatsAdapter.disconnect()`'s `await self._nc.close()` would blow up in every test touching the lifecycle path. No downstream cost — the real nats-py `Client.close` is already a coroutine, so keeping `close` async matches production behavior.

### 2026-04-21 — Phase 3 — `_on_prompt` ships a placeholder response instead of a real pipeline

The handler registered at `agent.on_prompt(...)` is a one-liner that sends a short "NATS adapter is online, Phase 4 wires the real pipeline" ResponseChunk and returns. Reason: `natsagent.Agent.start()` enforces that a handler is registered (raises otherwise), and we need `connect()` to land a fully-running micro service in Phase 3 so `$SRV.PING` discovery and heartbeat emission can be verified against a real nats-server between phases. Phase 4 swaps this handler for the real `x-session` + attachment + MessageEvent pipeline (T4.1). The placeholder is test-asserted (`TestPromptHandlerStub`) so any regression during Phase 4's swap will be caught.

### 2026-04-21 — Phase 3 — `disconnect()` teardown order: `agent.stop()` before `nc.close()`

`_teardown_handles()` stops the agent first, then closes the NATS client. Reason: the SDK's heartbeat publisher runs inside the agent's background task and emits on the NATS connection. Closing `nc` first would surface a burst of "connection closed" warnings from the heartbeat loop before the stop signal reaches it. Both halves of teardown are wrapped in try/except so a failing stop() doesn't prevent the close() and vice-versa — gateway shutdown runs disconnect() over every adapter in sequence and one raising would abort teardown for all the others after it.

### 2026-04-21 — Phase 3 — Lock release on connect failure is routed through `_teardown_handles()`, not a separate code path

Previously in Telegram (`gateway/platforms/telegram.py:910`), the connect() failure branch explicitly calls `_release_platform_lock()`. The NATS adapter instead routes both the success-disconnect path and the connect-failure path through a single `_teardown_handles()` helper. Reason: Phase 4 adds more handles (`_active_streams`, `stream_delta_callback`, keep-alive task) that also need cleanup in both paths — centralizing the teardown logic now means T4.x doesn't have to remember to wire cleanup into two places.

### 2026-04-21 — Phase 3 — Shutdown event + in-flight handler tracking landed early (pre-Phase 4)

Design doc §9 calls for "signal cancellation to in-flight `_on_prompt` handlers / await all outstanding pump / keep-alive / `_on_prompt` tasks" during shutdown. Phase 3's placeholder handler is a one-liner with no long-running work, so this was initially deferred to Phase 4. Review feedback: land the infrastructure now so Phase 4's handler body inherits the cancellation behavior for free instead of having to retrofit it.

The machinery:
- `self._shutdown_event: asyncio.Event` — set at the top of `_teardown_handles`, cleared at the top of `connect()`. Phase 4's streaming loop will `if self._shutdown_event.is_set(): break` between deltas.
- `self._in_flight_handlers: set[asyncio.Task]` — `_on_prompt` registers its own task via `asyncio.current_task()` at entry and discards it in a `finally` block. `_teardown_handles` cancels every live task and `asyncio.gather(..., return_exceptions=True)`s them before `agent.stop()` runs.
- `discard` (not `remove`) in the finally block: `_teardown_handles` may call `_in_flight_handlers.clear()` after gather returns, so the finally may find the task already gone.

Tests cover: task registration/deregistration on normal completion, finally-block tolerance of a mid-handler `clear()` (regression guard for `remove` vs. `discard`), cancellation of a hanging handler during `disconnect()` bounded by `asyncio.wait_for`, shutdown event set-before-stop ordering, shutdown event cleared by a retry `connect()`.

### 2026-04-21 — Phase 3 — Disconnect ordering test tightened with a side_effect call-order recorder

The original `test_disconnect_after_successful_connect_tears_down_in_order` asserted `agent.stop.assert_awaited_once()` + `nc.close.assert_awaited_once()` but NOT their relative order — the name was aspirational. Tightened by attaching `side_effect=lambda: call_order.append("stop")` / `("close")` to each mock and asserting `call_order == ["stop", "close"]`. `mock.call_args_list` is per-mock, so cross-mock ordering genuinely requires a shared recorder; `MagicMock.attach_mock` is the other standard option but the side_effect approach is one line shorter.

### 2026-04-21 — Phase 4 — Command vs. text prompt split at `_on_prompt`

Design doc §6.1 says "adapter-owned AIAgent, bypass GatewayStreamConsumer" (api_server pattern). Task list T4.1 literally says "`handle_message(event)`". Reconciling: a pure api_server-style bypass loses slash commands, which §10 explicitly wants. Pure `handle_message(event)` routes text prompts through `GatewayStreamConsumer` whose edit-a-single-message model is nonsense on NATS.

Phase 4 resolves this with a two-branch dispatch inside `_on_prompt`:
- Slash commands → `self._message_handler(event)` directly (gateway's `_handle_message` runs, returns the rendered response string, we wrap it in a `ResponseChunk` and publish). The gateway's command path short-circuits before `GatewayStreamConsumer` is ever constructed, so this is clean.
- Text prompts → adapter-owned `AIAgent` via `_run_agent_sync` in an executor, with a `stream_delta_callback` that feeds an `asyncio.Queue` drained by `_pump_deltas`. Each delta is its own `ResponseChunk`.

The classification heuristic (`_looks_like_command`) rejects paths (`/var/log/foo`), double-slashes (`//`), and bodies with non-alnum first chars — matches `MessageEvent.get_command()`'s behaviour in `base.py:746`.

### 2026-04-21 — Phase 4 — `SUPPORTS_MESSAGE_EDITING = False` on NatsAdapter

NATS publishes each streaming chunk as a fresh `ResponseChunk`; the protocol has no edit semantics (§6.1). `gateway/run.py:9597-9599` short-circuits `GatewayStreamConsumer` construction when the adapter reports it can't edit, so setting this flag is the cheapest way to ensure any code path that does go through `handle_message(event)` (slash commands today, possibly more tomorrow) gracefully skips the edit-based consumer instead of making noise. Streaming is wired adapter-locally via `_run_text_prompt` regardless. `weixin` and `qqbot` both use the same flag for the same reason.

### 2026-04-21 — Phase 4 — `_extract_x_session` peeks `stream._request.data`

Design doc §3 flags this as "open question (b)" — accepted here as MVP. The SDK's `Envelope` pydantic model has `extra="ignore"` (envelope.py:35), so `x-session` is dropped before our handler sees it. `PromptStream.__init__` stores the request on `self._request`, and `request.data` is the raw payload (agent.py:258). We JSON-re-parse the raw bytes locally. This is a private attribute today; if the SDK renames it, the adapter breaks loud and fast (attribute error at handler entry) rather than silently routing every session to `"default"`. A note to upstream a public raw-bytes handle to `nats-ai-pysdk` is carried in design doc §13 non-goals.

### 2026-04-21 — Phase 4 — Attachment cache failures convert to `RuntimeError`

`cache_image_from_bytes` raises `ValueError` when the magic bytes don't match (e.g. caller uploaded HTML as `.jpg`). The SDK's `_on_prompt_request` wraps any `Exception` from the handler into a 500 error frame (agent.py:270-272). For attachment-validation errors that are clearly caller-fault, 400 would be more accurate, but the SDK only differentiates based on the exception class it recognizes — `ProtocolError` → 400, anything else → 500. Raising `RuntimeError` gets us 500 with a clean message; upgrading to 400 would require either importing `natsagent.ProtocolError` at the adapter (tight coupling to the SDK's error module, which the test-harness mock barely models) or plumbing a typed error-response path into the handler. The design doc's §11 table already marks oversize-envelope as "deferred"; attachment-validation gets similar treatment for now.

### 2026-04-21 — Phase 4 — `_final_response_text` fallback publishes final text when no deltas streamed

Streaming deltas are fed via `stream_delta_callback` which the agent may not invoke (streaming disabled in config, tool-only turn, provider fallback). `run_conversation` returns a dict shape `{"final_response": "..."}` — we publish it as one `ResponseChunk` if and only if no deltas already landed. `threading.Event` guards the "anything streamed?" flag because the callback runs on the worker thread while the finalizer runs on the event-loop thread; a plain `bool` wouldn't be visible across threads without an explicit barrier.

### 2026-04-21 — Phase 4 — `Platform.NATS` added to `hermes_cli/platforms.py` + `hermes-nats` toolset

`_get_platform_tools(config, Platform.NATS.value)` requires a `PLATFORMS["nats"]["default_toolset"]` entry or it `KeyError`s. Registered `"nats"` → `"hermes-nats"` in `hermes_cli/platforms.py` (the shared registry, `tools_config.py` derives from it), and added a `hermes-nats` toolset in `toolsets.py` that mirrors `_HERMES_CORE_TOOLS` — same scope as other messaging platforms. A tighter NATS-specific subset can be carved out later if we want to restrict tools by transport.

### 2026-04-21 — Phase 4 — Conftest mock gained `StatusChunk` + kwargs-recording ResponseChunk

Phase 3's `ResponseChunk = MagicMock` was good enough for the placeholder handler which passed a bare string. Phase 4 emits `ResponseChunk(text=delta)` and `StatusChunk(status="ack")` via kwargs — tests assert on `chunk.text` / `chunk.status` to verify the adapter wrapped outgoing content correctly. Plain `MagicMock(text=...)` would return a MagicMock on attribute access rather than the string we passed, so the conftest now installs small kwargs-recording classes. Real SDK pydantic models behave the same way with the same surface.

### 2026-04-21 — Phase 4 — `_on_prompt` re-raises `CancelledError`, swallows all other exceptions

The SDK's `_on_prompt_request` has two clauses: `except Exception` → respond 500 + terminator, but `CancelledError` (a `BaseException` in 3.11+) falls through. Phase 4's handler mirrors that split — `CancelledError` re-raises so shutdown cancellation propagates cleanly through `_teardown_handles`'s `gather(return_exceptions=True)`. Arbitrary exceptions also re-raise so the SDK can convert them into a 500 error frame; we log them at ERROR level first so the gateway log has the full stack trace, not just the SDK's sanitized description line.

### 2026-04-21 — Phase 4 post-review — Authorization: NATS added to `_is_user_authorized` early-return set

Surfaced during Phase 4 self-review. `gateway/run.py:_is_user_authorized` had no handling for `Platform.NATS`. Commands dispatched via `_message_handler` hit the user allowlist check, which treated the caller's `x-session` string as a user_id and rejected it unless pre-paired — so `/help` over NATS replied with a pairing code instead of the help text. Design doc §10.1 already delegates NATS authorization to the NATS server layer (accounts / NKey / JWT / TLS), mirroring Webhook (HMAC) and HomeAssistant (HASS_TOKEN). Fix: add `Platform.NATS` to the `(HOMEASSISTANT, WEBHOOK)` early-return tuple. Regression test lives in `tests/gateway/test_unauthorized_dm_behavior.py::test_nats_is_authorized_without_user_allowlist`.

### 2026-04-21 — Phase 4 post-review — Command text lstripped before `MessageEvent` construction

Surfaced during Phase 4 self-review. `_looks_like_command` tolerates leading whitespace (``"  /help"`` → True, covered by tests), but `MessageEvent.is_command` / `get_command()` in `base.py:732` require literal `text.startswith("/")`. Before the fix, a whitespace-prefixed command would pass our heuristic → we'd mark it as `MessageType.COMMAND` and call `_dispatch_command(event, stream)` → the gateway's `_handle_message` → `event.get_command()` returns `None` → falls through to the text-agent path, which we already decided to bypass. Net result: silent misrouting. Fix: when `is_command` is True, set `event_text = prompt_text.lstrip()` before constructing the `MessageEvent`. Regression test in `test_nats_inbound.py::TestOnPromptIntegration::test_command_text_is_lstripped_for_gateway_dispatch`.

### 2026-04-21 — Phase 4 — Known shortcomings (NOT fixed in Phase 4; carry forward)

Each of these is a deliberate MVP trade-off that should either land in a later phase or be promoted to a design-doc non-goal. Logged here so future Claudes don't waste cycles rediscovering them as "bugs".

1. **Concurrent prompts on the same `x-session` overwrite `_active_streams[chat_id]`.** Two prompts with the same `x-session` arriving in quick succession race — the second replaces the first in `_active_streams`, so tool outputs from the first handler (e.g., `send_image_file`) land on the second handler's reply subject. The finally-block guards against popping the wrong key on cleanup (`current is stream`) but that only protects exit; it doesn't protect the mid-handler mis-route. **Status: SCHEDULED for Phase 5 as T5.0** (folded in as a prerequisite because the four new `send_*` tool methods quadruple the blast radius). See Phase 5's "Fold-in justification" note above for implementation options (a/b/c).

2. **`/stop` cannot interrupt a running NATS agent.** We bypass `self.handle_message(event)` for text prompts (design doc §6.1, api_server-style agent ownership), so `_active_sessions[session_key]` in `BasePlatformAdapter` is never populated. The gateway's `/stop` handler walks `_active_sessions` to find a running agent — for NATS that dict is always empty, so `/stop` becomes a no-op. Callers can drop their NATS subscription to abandon a run; real interrupt support would require either (a) routing text through `handle_message` and adding a NATS-aware stream consumer, or (b) adapter-local active-session tracking that the `/stop` handler is taught to consult. Defer to post-MVP.

3. **Unbounded `delta_queue`.** `asyncio.Queue()` is unbounded by default; if the model produces deltas faster than `stream.send` can drain them, memory grows linearly with the run. Not practical at LLM token rates (thousands of tokens per second max, each chunk small) but would matter if we ever drove a non-token data stream through the same pump. Not scheduled for a fix.

4. **Attachment-validation errors return SDK 500, not 400.** The SDK's `_on_prompt_request` only maps `ProtocolError` to 400; anything else becomes 500. `cache_image_from_bytes` raising `ValueError` on non-image bytes is caller-fault and should be 400 per §9.3 semantics, but the handler raises `RuntimeError` → 500. Fix requires either importing `natsagent.ProtocolError` directly (tight coupling to a module the test-harness mock barely models) or plumbing a typed error-response path. Noted in the Phase 4 attachment decision-log entry; revisit if callers complain.

5. **Session interrupt / busy-session merging is gone.** `BasePlatformAdapter.handle_message` has useful logic for "photo burst merging", "busy-session handoff", and "pending-message queue drain". NATS `_on_prompt` bypasses all of that. For a request/reply wire protocol that's fine (the caller controls concurrency), but anything downstream that assumes `_pending_messages`/`_active_sessions` population won't work over NATS. Document as a limitation.

6. **Private `stream._request.data` access for x-session peek.** Design doc §3 option (b), pre-approved. If the SDK renames `_request` or `data`, we blow up loud at handler entry (AttributeError via `getattr(..., None)` returning None → falls back to session default). Acceptable for MVP; upstream a public raw-bytes handle to `nats-ai-pysdk` when convenient.

---

## Task definitions reference

If `TaskList` is empty after a context clear and you need to recreate the tasks, use these verbatim. Subject / description / activeForm are the `TaskCreate` parameters.

| T#.#   | subject                                                                 | activeForm                              |
|--------|-------------------------------------------------------------------------|-----------------------------------------|
| T0.1   | T0.1 — Write design doc docs/nats-gateway-design.md                     | Writing NATS gateway design doc         |
| T0.2   | T0.2 — Add CLAUDE.md pointer to design doc and natsagent SDK             | Updating CLAUDE.md pointers             |
| T1.1   | T1.1 — Add Platform.NATS enum value                                     | Adding Platform.NATS                    |
| T1.2   | T1.2 — Extend _apply_env_overrides() for NATS                           | Adding NATS env overrides               |
| T1.3   | T1.3 — Extend get_connected_platforms() for NATS                        | Extending get_connected_platforms for NATS |
| T1.4   | T1.4 — Register NATS adapter in _create_adapter()                       | Registering NATS adapter                |
| T1.5   | T1.5 — Add natsagent to pyproject.toml extras                           | Adding natsagent to pyproject           |
| T1.6   | T1.6 — Add natsagent mock in tests/gateway/conftest.py                  | Mocking natsagent in conftest           |
| T2.1   | T2.1 — Create gateway/platforms/nats.py skeleton                        | Creating NATS adapter skeleton          |
| T2.2   | T2.2 — Unit test for config parsing                                     | Testing NATS config parsing             |
| T3.1   | T3.1 — Implement NATS connect()                                         | Implementing NATS connect               |
| T3.2   | T3.2 — Implement NATS disconnect()                                      | Implementing NATS disconnect            |
| T3.3   | T3.3 — Implement get_chat_info()                                        | Implementing get_chat_info              |
| T3.4   | T3.4 — Tests for connect/disconnect                                     | Testing NATS connect/disconnect         |
| T4.1   | T4.1 — Implement _on_prompt handler                                     | Implementing _on_prompt handler         |
| T4.2   | T4.2 — Wire _active_streams into send()                                 | Wiring _active_streams into send        |
| T4.3   | T4.3 — Wire streaming deltas                                            | Wiring NATS streaming deltas            |
| T4.4   | T4.4 — Tests for inbound path                                           | Testing NATS inbound path               |
| T5.0   | T5.0 — Race-safe `_active_streams` lookup (folded-in Phase 4 shortcoming) | Making NATS stream lookup race-safe     |
| T5.1   | T5.1 — Implement send_image_file                                        | Implementing send_image_file            |
| T5.2   | T5.2 — Implement send_document                                          | Implementing send_document              |
| T5.3   | T5.3 — Implement send_voice / send_video                                | Implementing send_voice/send_video      |
| T5.4   | T5.4 — format_message override (likely no-op)                           | Overriding format_message               |
| T5.5   | T5.5 — Tests for outbound attachments + concurrent-x-session regression | Testing NATS outbound attachments       |
| T6.1   | T6.1 — Survey _pending_approvals usage                                  | Surveying _pending_approvals usage      |
| T6.2   | T6.2 — Add adapter-side query hook method                               | Adding request_interaction hook         |
| T6.3   | T6.3 — Wire approval callbacks to adapter.request_interaction           | Wiring approval callbacks               |
| T6.4   | T6.4 — Tests for NATS query reply                                       | Testing NATS query reply                |
| T7.1   | T7.1 — Decide which slash commands are exposed over NATS                | Deciding NATS slash commands            |
| T7.2   | T7.2 — Confirm /help renders sensibly over plain-text chunks            | Verifying /help rendering               |
| T8.1   | T8.1 — Local NATS + hermes smoke config                                 | Documenting local smoke config          |
| T8.2   | T8.2 — Verify 01-discover.py lists hermes                               | Verifying discover                      |
| T8.3   | T8.3 — Verify 02-prompt-text.py streams                                 | Verifying prompt-text streaming         |
| T8.4   | T8.4 — Verify 03-prompt-attachment.py                                   | Verifying attachment flow               |
| T8.5   | T8.5 — Verify 04-query-reply.py approval path                           | Verifying query-reply                   |
| T8.6   | T8.6 — Verify 05-liveness.py                                            | Verifying liveness                      |
| T8.7   | T8.7 — nats CLI interop check                                           | Verifying nats CLI interop              |
| T8.8   | T8.8 — Full test suite green                                            | Running full test suite                 |
| T9.1   | T9.1 — Update ADDING_A_PLATFORM.md if new integration points emerged    | Updating ADDING_A_PLATFORM              |
| T9.2   | T9.2 — Expand design doc with lessons learned                           | Expanding design doc with lessons       |
| T9.3   | T9.3 — Example hermes config snippet in docs                            | Adding example config snippet           |

Descriptions for each task are listed in the design doc's Phase tables + the plan the user originally approved. Short paraphrase of each is given in the phase checklist above — sufficient for TaskCreate.

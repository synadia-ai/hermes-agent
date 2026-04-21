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

- **Last completed phase:** Phase 1 — Scaffolding & config (T1.1 through T1.6)
- **Next phase:** Phase 2 — Adapter skeleton (T2.1, T2.2)
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

- [ ] **T2.1** — Create `gateway/platforms/nats.py` skeleton (`check_nats_requirements`, `NatsAdapter` stub, `NatsAdapterSettings` dataclass, validation)
- [ ] **T2.2** — `tests/gateway/test_nats_config.py` — config parsing (happy/bad/env-override)

### Phase 3 — Connection & lifecycle

- [ ] **T3.1** — Implement `NatsAdapter.connect()` (lock, natsagent.connect, Agent.start, _mark_connected)
- [ ] **T3.2** — Implement `NatsAdapter.disconnect()` (idempotent, cancel handlers, agent.stop, nc.close, release lock)
- [ ] **T3.3** — Implement `get_chat_info()` (returns `{"name": chat_id, "type": "dm"}`)
- [ ] **T3.4** — `tests/gateway/test_nats_connect.py` — connect/disconnect/lock/handler registration

### Phase 4 — Inbound path (the meaty one; plan for a dedicated session)

- [ ] **T4.1** — Implement `_on_prompt(envelope, stream)` — x-session → chat_id, attachments → media cache, MessageEvent, `_active_streams[chat_id] = stream`, keep-alive task, `handle_message(event)`, cleanup
- [ ] **T4.2** — Wire `_active_streams` into `send()` — look up PromptStream, `stream.send(ResponseChunk(text=content))`, return SendResult
- [ ] **T4.3** — Wire streaming deltas — adapter-owned AIAgent with `stream_delta_callback` forwarding to a queue → pump → `stream.send`. (Ownership decision: adapter owns the callback; see §6.1 of design doc.)
- [ ] **T4.4** — `tests/gateway/test_nats_inbound.py` — envelope in, MessageEvent, deltas emitted, keep-alive fires, terminator, attachment round-trip

### Phase 5 — Outbound attachments & formatting

- [ ] **T5.1** — Implement `send_image_file` (`Attachment.from_path(path)` → `stream.send(ResponseChunk(text=caption, attachments=[...]))`)
- [ ] **T5.2** — Implement `send_document` (same pattern, generic file)
- [ ] **T5.3** — Implement `send_voice` / `send_video` (same pattern; v0.1 doesn't distinguish on wire)
- [ ] **T5.4** — `format_message()` override (no-op for symmetry — NATS carries text verbatim)
- [ ] **T5.5** — `tests/gateway/test_nats_outbound.py` — image/doc/voice → ResponseChunk.attachments[0] shape

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
6. **Report to the user:** "Phase N done. Tests: <passing>. Ready for review / context clear."
7. **Do not commit, push, or clear context yourself.** Those are user decisions. Wait.

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
| T5.1   | T5.1 — Implement send_image_file                                        | Implementing send_image_file            |
| T5.2   | T5.2 — Implement send_document                                          | Implementing send_document              |
| T5.3   | T5.3 — Implement send_voice / send_video                                | Implementing send_voice/send_video      |
| T5.4   | T5.4 — format_message override (likely no-op)                           | Overriding format_message               |
| T5.5   | T5.5 — Tests for outbound attachments                                   | Testing NATS outbound attachments       |
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

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

`AGENTS.md` in this repo is the authoritative long-form development guide. This file distills what Claude Code needs to be productive immediately; consult `AGENTS.md` for the full rationale, examples, and edge cases behind any section below.

## Environment

```bash
source venv/bin/activate   # ALWAYS activate before running Python
```

The bootstrap script `./setup-hermes.sh` installs `uv`, creates `venv`, installs `.[all,dev]`, and symlinks `~/.local/bin/hermes`. `./hermes` auto-detects the venv — no need to `source` first when just running.

User state lives in `~/.hermes/` (config.yaml, .env, sessions, skills, memory). Tests and code must NEVER hardcode this — see the Profiles section.

## Testing

**ALWAYS use `scripts/run_tests.sh`** — do not invoke `pytest` directly. The wrapper enforces CI parity (unset `*_API_KEY`/`*_TOKEN`, `TZ=UTC`, `LANG=C.UTF-8`, `-n 4` xdist workers, temp `HERMES_HOME`). Running raw `pytest` on a multi-core machine with keys set has repeatedly caused "works locally, fails in CI" incidents in both directions.

```bash
scripts/run_tests.sh                                  # full suite
scripts/run_tests.sh tests/gateway/                   # one directory
scripts/run_tests.sh tests/agent/test_foo.py::test_x  # one test
scripts/run_tests.sh -v --tb=long                     # pass-through pytest flags
```

`tests/conftest.py` also enforces hermetic behavior via autouse fixtures. `_isolate_hermes_home` redirects `HERMES_HOME` to a temp dir — never hardcode `~/.hermes/` in tests. Profile tests must also mock `Path.home()` (see `tests/hermes_cli/test_profiles.py`).

Worker count above 4 surfaces test-ordering flakes that CI never sees. Always run the full suite before pushing.

## TUI build/dev (ui-tui)

```bash
cd ui-tui
npm install         # first time
npm run dev         # watch (rebuilds hermes-ink + tsx --watch)
npm run build       # full build
npm run type-check  # tsc --noEmit
npm run lint        # eslint
npm test            # vitest
```

## Architecture

Hermes Agent is a self-improving AI agent with a CLI, a TUI, and a messaging gateway sharing one agent core. The project is primarily Python; the TUI is Ink/React over a stdio JSON-RPC bridge to Python.

### Import dependency chain (respect this order)

```
tools/registry.py           # no deps — imported by all tool files
        ↑
tools/*.py                  # each calls registry.register() at import time
        ↑
model_tools.py              # imports registry + triggers tool discovery
        ↑
run_agent.py, cli.py, batch_runner.py, environments/
```

Any `tools/*.py` file with a top-level `registry.register()` call is auto-discovered by `discover_builtin_tools()` — no manual import list to maintain.

### Core entry points

- `run_agent.py` — `AIAgent` class. The synchronous conversation loop is inside `run_conversation()`: call model, dispatch tool calls through `handle_function_call()`, append results, repeat until `max_iterations`/`iteration_budget` or the model returns a final message. Messages follow OpenAI format; reasoning content is stored in `assistant_msg["reasoning"]`.
- `cli.py` — `HermesCLI`, the interactive terminal orchestrator (Rich banner/panels, `prompt_toolkit` input, `KawaiiSpinner`). `process_command()` dispatches on canonical command names resolved via `resolve_command()`.
- `model_tools.py` — tool orchestration, schema collection, dispatch.
- `toolsets.py` — toolset definitions and `_HERMES_CORE_TOOLS`.
- `hermes_state.py` — `SessionDB`, SQLite + FTS5 session store.
- `hermes_cli/main.py` — entry point for all `hermes` subcommands; calls `_apply_profile_override()` to set `HERMES_HOME` before any module imports.
- `gateway/run.py` — messaging gateway main loop, shares slash commands with CLI.
- `gateway/platforms/` — per-platform adapters (telegram, discord, slack, whatsapp, signal, matrix, email, webhook, api_server, …). `base.py` defines `BasePlatformAdapter`; `ADDING_A_PLATFORM.md` is the authoritative checklist when adding a new one (adapter methods, `Platform` enum, config wiring, env overrides, status locks, tests).
- `tui_gateway/` — Python stdio JSON-RPC backend for the Ink TUI in `ui-tui/`.

### Slash command registry

All slash commands are defined once in `COMMAND_REGISTRY` in `hermes_cli/commands.py` as `CommandDef` objects. CLI dispatch, gateway dispatch, `/help`, Telegram BotCommand menu, Slack `/hermes` subcommands, and autocomplete all derive from this registry automatically.

To add a command: add a `CommandDef` to `COMMAND_REGISTRY`, then add an `elif canonical == "name":` branch in `HermesCLI.process_command()` (cli.py) and, if the command is gateway-available, in `gateway/run.py`. Adding an alias requires only extending the `aliases` tuple — nothing else.

`cli_only` + `gateway_config_gate="dotpath"` makes a command conditionally available in the gateway when the config value is truthy.

### Adding a tool

Two files:

1. Create `tools/your_tool.py`. At module top-level, call `registry.register(name=, toolset=, schema=, handler=, check_fn=, requires_env=)`. Handlers MUST return a JSON string.
2. Add the tool name to either `_HERMES_CORE_TOOLS` in `toolsets.py` (available on all platforms) or a new toolset.

If the schema description mentions a path, compute it at import time with `display_hermes_home()` so it's profile-aware. For persistent tool state, use `get_hermes_home()` as the base — never `Path.home() / ".hermes"`.

Agent-level tools (todo, memory) are intercepted by `run_agent.py` before `handle_function_call()` — see `tools/todo_tool.py` for the pattern.

Never name tools from other toolsets in a schema description (e.g. `browser_navigate` saying "prefer web_search"). Those tools may be unavailable and the model will hallucinate calls. Inject cross-references dynamically in `get_tool_definitions()` in `model_tools.py`.

### TUI process model

```
hermes --tui
  └─ Node (Ink)  ──stdio JSON-RPC──  Python (tui_gateway)
       │                                  └─ AIAgent + tools + sessions
       └─ renders transcript, composer, prompts, activity
```

TypeScript owns the screen. Python owns sessions, tools, model calls, slash command logic. Transport is newline-delimited JSON-RPC over stdio; see `tui_gateway/server.py` for the method/event catalog. Built-in client commands (`/help`, `/quit`, `/clear`, `/resume`, `/copy`, `/paste`) handle locally in `app.tsx`; everything else goes through `slash.exec` → persistent `_SlashWorker` subprocess → `command.dispatch` fallback.

### Configuration

- `config.yaml`: add option to `DEFAULT_CONFIG` in `hermes_cli/config.py` and bump `_config_version` to trigger migration for existing users.
- `.env`: add to `OPTIONAL_ENV_VARS` with `{description, prompt, url, password, category}` metadata.
- Three config loaders exist: `load_cli_config()` (CLI), `load_config()` (`hermes tools`/`setup`), direct YAML load (gateway). Keep them consistent.

### Skin engine

`hermes_cli/skin_engine.py` — data-driven CLI theming. Built-in skins live in `_BUILTIN_SKINS`; user skins are YAML files dropped into `~/.hermes/skins/`. Missing keys inherit from the `default` skin. Activate via `/skin <name>` or `display.skin` in config.yaml. Adding a new theme is pure data — no code changes.

## Critical policies

### Prompt caching must not break

The agent relies on Anthropic prompt caching. Do NOT alter past context mid-conversation, change toolsets mid-conversation, or reload memory/rebuild system prompts mid-conversation. The only sanctioned mid-conversation context mutation is context compression. Skill slash commands are injected as a **user message**, not the system prompt, specifically to preserve caching.

### Profiles (multi-instance isolation)

Hermes supports profiles — multiple fully isolated instances each with their own `HERMES_HOME` (config, keys, memory, sessions, skills, gateway). `_apply_profile_override()` in `hermes_cli/main.py` sets `HERMES_HOME` before module imports, so all `get_hermes_home()` callers automatically scope to the active profile.

- Code paths: `from hermes_constants import get_hermes_home` — never `Path.home() / ".hermes"`.
- User-facing messages: `display_hermes_home()` — returns `~/.hermes` or `~/.hermes/profiles/<name>`.
- Module-level constants that call `get_hermes_home()` at import time are fine (profile override runs first).
- Gateway platform adapters that connect with a unique credential (bot token) must call `acquire_scoped_lock()`/`release_scoped_lock()` from `gateway.status` in connect/disconnect. Canonical pattern: `gateway/platforms/telegram.py`.
- Profile management operations are HOME-anchored, not HERMES_HOME-anchored: `_get_profiles_root()` returns `Path.home() / ".hermes" / "profiles"` intentionally, so `hermes -p coder profile list` sees all profiles regardless of the active one.

Hardcoding `~/.hermes` was the root cause of 5 bugs fixed in PR #3575.

### Working directory

- CLI: current directory (`os.getcwd()`).
- Messaging: `MESSAGING_CWD` env var (default: home directory).

### Background process notifications (gateway)

`terminal(background=true, notify_on_complete=true)` spawns a watcher that triggers a new agent turn on completion. Verbosity controlled via `display.background_process_notifications` or `HERMES_BACKGROUND_NOTIFICATIONS`: `all` | `result` | `error` | `off`.

## Adding a gateway platform

When building a new messaging platform adapter (e.g. a NATS channel), follow `gateway/platforms/ADDING_A_PLATFORM.md` end-to-end. It enumerates every integration point that actually exists in the codebase: adapter methods, `Platform` enum + env overrides in `gateway/config.py`, slash-command availability gates, session wiring, status locks for profile isolation, and test coverage. Missing items cause silent feature regressions (reply loops, wrong working directory, profile collisions) rather than loud failures.

Canonical adapter to copy from: `gateway/platforms/telegram.py` — it uses `acquire_scoped_lock()`/`release_scoped_lock()` from `gateway.status` correctly and is the reference pattern for profile-safe credentialed adapters.

### NATS gateway channel

The NATS gateway channel is implemented at `gateway/platforms/nats.py`. Key references:

- `docs/nats-gateway-design.md` is the architectural reference — protocol↔adapter mapping, streaming model, session identity, lock scope, approval hook design, failure modes. §17 of that doc captures the retrospective lessons learned during implementation (contextvar-through-`run_coroutine_threadsafe` pitfalls, structural race elimination via per-session serialization, adapter-owned-`AIAgent` side-effect audit, canonical user-message templates). Read it before touching NATS code.
- `docs/nats-gateway-progress.md` is the phase-by-phase progress log (completed through Phase 9). The decision log at the bottom captures non-obvious moment-of-landing context that the design doc alone doesn't cover.
- `docs/nats-gateway.md` points to the user-facing setup guide at `website/docs/user-guide/messaging/nats.md`.
- Agent-side SDK: `natsagent` at `../nats-ai-pysdk` (package `natsagent`). It wraps micro-service registration, heartbeats, chunk wrapping, terminators, error headers, and mid-stream `stream.ask()` automatically. Protocol spec: `../nats-ai-pysdk/docs/nats-agent-protocol.md`. Until it ships on PyPI, install with `uv pip install --python venv/bin/python -e ../nats-ai-pysdk`.

The NATS adapter is the canonical example in the codebase of an **adapter-owned `AIAgent`** (api_server-style, not `handle_message`-routed) + **per-session `asyncio.Lock` serialization** + **`request_interaction` approval hook**. If you're building another transport with the same shape (programmatic request/reply, non-edit-based streaming), copy patterns from `nats.py`, not `telegram.py`.

## Scratch directory: `RENE/`

`RENE/` is a gitignored scratch directory for files that are valuable to keep locally but are not yet decided to be committed (imported specs, notes, draft docs, work-in-progress artifacts). Nothing in `RENE/` ships. When the user asks you to save something that isn't clearly production-ready, default to writing it there.

## Known pitfalls

- **`simple_term_menu`** renders buggily in tmux/iTerm2 (ghosting on scroll) — use `curses` (stdlib). See `hermes_cli/tools_config.py`.
- **`\033[K`** (ANSI erase-to-EOL) leaks as literal `?[K` under `prompt_toolkit`'s `patch_stdout`. Pad with spaces instead: `f"\r{line}{' ' * pad}"`.
- **`_last_resolved_tool_names`** in `model_tools.py` is a process-global. `_run_single_child()` in `delegate_tool.py` saves/restores it around subagent execution — may be temporarily stale during child agent runs.
- **Cross-tool names in schema descriptions** can cause hallucinated calls when the referenced toolset is disabled — inject dynamically in `get_tool_definitions()`.

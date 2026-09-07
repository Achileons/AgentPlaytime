# AgentPlaytime

> "Track your AI playtime. Unlock achievements. Question your life choices."

AgentPlaytime is a macOS-first, local-first command-line application that measures how long supported AI applications and agent processes are running. It is **not** an active-window tracker: foreground and background do not matter. If a supported tool is running, its timer runs; when the tool stops, its timer stops.

## Privacy

**AgentPlaytime does not read prompts, conversations, keystrokes, screenshots, clipboard contents, or window contents.**

**AgentPlaytime must NEVER collect:**

- prompts
- conversations
- keystrokes
- screenshots
- window contents
- clipboard contents

**Only application/process runtime metadata is stored.** Data stays locally in SQLite at `~/Library/Application Support/AgentPlaytime/agentplaytime.db` by default. There is no telemetry, cloud sync, or browser extension.

## What v0.1 does

Version 0.1 only tracks runtime for these supported tools:

- ChatGPT Desktop
- Claude Desktop
- Claude Code CLI
- Codex CLI or desktop, when detectable
- Cursor

Native macOS applications are identified as applications so their helper and renderer processes do not become separate timers. Command-line agents are found through process inspection. Multiple instances of the same tool still count as one logical running timer.

AgentPlaytime polls roughly every two seconds and records each session's tool, UTC start time, and UTC end time. Switching to another application does not stop a session.

## Requirements

- macOS
- Python 3.12 or newer

## Install for local development

```console
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install ".[dev]"
```

The macOS-only PyObjC Cocoa framework is installed automatically on macOS. `psutil` is used for process inspection.

## Usage

First, inspect what AgentPlaytime can see on this Mac:

```console
agentplaytime doctor
```

The diagnostic output helps verify actual bundle identifiers and process names before relying on a detection rule. It does not create runtime sessions.

Use `agentplaytime doctor --all` to include the full privacy-safe metadata snapshot available to the current environment, or `agentplaytime doctor --json` for structured output. Process diagnostics intentionally omit command arguments because commands such as `claude -p` and `codex exec` can contain prompt text.

Start the tracker and leave it running:

```console
agentplaytime track
```

The tracker reports transitions such as:

```text
[START] ChatGPT
[STOP] ChatGPT — 12m 31s
```

Show accumulated runtime per tool:

```console
agentplaytime stats
```

Run the test suite:

```console
pytest
```

Stop tracking with `Ctrl-C`. AgentPlaytime closes active sessions during graceful shutdown.

## Detection notes and v0.1 limitations

AgentPlaytime uses `NSWorkspace` bundle identifiers for native applications and `psutil` executable metadata for command-line agents. If a restricted macOS environment returns an empty `NSWorkspace` result, a fallback reads only exact, known application-service labels from `launchctl`; unrelated output is filtered before Python receives it.

`doctor` labels bundle rules that were verified on the current development Mac separately from compatibility candidates that could not be verified here. Display names alone are diagnostic hints and never start a timer.

Graceful `SIGINT`/`SIGTERM` shutdown closes every active session. After a hard crash or power loss, v0.1 cannot know whether a tool ran continuously during the tracker outage, so a previously open session is resumed until the next observation. During a detector-backend outage, the tracker warns and retains that backend's last known state to avoid false stop/start flapping; a permanent outage can therefore extend a session until detection recovers or the tracker shuts down.

## v0.1 scope and future plans

This first release intentionally has no graphical or menu-bar interface. It does not implement achievements, streaks, XP, an Agent Card, cloud sync, telemetry, or a browser extension.

Steam-style achievements and shareable profile cards are planned for a later release. Those ideas are future plans; v0.1 is deliberately focused on reliable, locally verifiable runtime detection.

## License

AgentPlaytime is available under the [MIT License](LICENSE).

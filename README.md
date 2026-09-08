# AgentPlaytime

> "Track your AI playtime. Unlock achievements. Question your life choices."

AgentPlaytime is a macOS-first, local-first command-line application that measures how long supported AI applications and agent processes are running. It is **not** an active-window tracker: foreground and background do not matter. If a supported tool is running, its timer runs; when the tool stops, its timer stops.

## Privacy

**AgentPlaytime does not read prompts, conversations, command arguments, keystrokes, screenshots, clipboard contents, window contents, environment variables, user file contents, or process memory.**

**AgentPlaytime must NEVER collect:**

- prompts
- conversations
- command arguments containing prompts
- keystrokes
- screenshots
- window contents
- clipboard contents
- environment variables
- user file contents
- process memory

**Only application/process runtime metadata is stored.** Data stays locally in SQLite at `~/Library/Application Support/AgentPlaytime/agentplaytime.db` by default. There is no telemetry, cloud sync, or browser extension.

## What v0.2 does

Version 0.2 tracks runtime and reports period-based local statistics for these supported tools:

- ChatGPT Desktop
- Claude Desktop
- Claude Code CLI
- Codex CLI or desktop, when detectable
- Cursor

Native macOS applications are identified as applications so their helper and renderer processes do not become separate timers. Command-line agents are found through process inspection. Multiple instances of the same tool still count as one logical running timer.

AgentPlaytime polls roughly every two seconds and records each session's tool, UTC start time, and UTC end time. Switching to another application does not stop a session. Statistics can summarize today, the last seven local calendar days, or all recorded time, including sessions that are still running.

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

Show all-time statistics per tool:

```console
agentplaytime stats
```

Choose a reporting period:

```console
agentplaytime stats --period today
agentplaytime stats --period week
agentplaytime stats --period all
```

`today` begins at the current local calendar day's midnight. `week` begins at local midnight six days before today, so it contains the last seven local calendar days including today. `all` includes every recorded session through the current time and remains the default for backward compatibility.

Each report includes total runtime, overlapping session count, average clipped session duration, longest clipped session duration, and current running status:

```text
AgentPlaytime — Today
Timezone: Europe/Istanbul
Total runtime: 2h 14m 0s

Tool        Total  Sessions  Average  Longest  Status
Claude  1h 20m 0s         3  26m 40s   45m 0s  running
Codex      54m 0s         2   27m 0s   35m 0s  stopped
```

Sessions are clipped to the selected period. A session crossing local midnight contributes only its post-midnight portion to `today`; a session crossing the seven-day boundary contributes only its overlapping portion to `week`. Each overlapping database session counts once, and averages and longest-session values use those clipped durations. Simultaneously running different tools contribute independently to the combined total.

Use `--json` with any period for machine-readable output:

```console
agentplaytime stats --period today --json
```

```json
{
  "period": "today",
  "timezone": "Europe/Istanbul",
  "generated_at": "2026-09-08T15:00:00+03:00",
  "range": {
    "start": "2026-09-08T00:00:00+03:00",
    "end": "2026-09-08T15:00:00+03:00"
  },
  "total_seconds": 8040,
  "tools": [
    {
      "tool": "Claude",
      "total_seconds": 4800,
      "session_count": 3,
      "average_session_seconds": 1600,
      "longest_session_seconds": 2700,
      "running": true
    }
  ]
}
```

JSON durations are non-negative integer seconds. Sub-second fractions are truncated only after a metric is aggregated. For an empty all-time database, `range.start` is `null`; otherwise it is the earliest recorded session start in local time.

AgentPlaytime resolves the Mac's IANA timezone from macOS system metadata and uses timezone-aware UTC conversions, including daylight-saving transitions. If an IANA name cannot be resolved, it safely falls back to the Mac's current fixed UTC offset and displays that offset as the timezone name; a seven-day report spanning an offset change may be less precise under this rare fallback.

Run the test suite:

```console
pytest
```

Stop tracking with `Ctrl-C`. AgentPlaytime closes active sessions during graceful shutdown.

## Detection notes and v0.2 limitations

AgentPlaytime uses `NSWorkspace` bundle identifiers for native applications and `psutil` executable metadata for command-line agents. If a restricted macOS environment returns an empty `NSWorkspace` result, a fallback reads only exact, known application-service labels from `launchctl`; unrelated output is filtered before Python receives it.

`doctor` labels bundle rules that were verified on the current development Mac separately from compatibility candidates that could not be verified here. Display names alone are diagnostic hints and never start a timer.

Graceful `SIGINT`/`SIGTERM` shutdown closes every active session. After a hard crash or power loss, v0.2 cannot know whether a tool ran continuously during the tracker outage, so a previously open session is resumed until the next observation. During a detector-backend outage, the tracker warns and retains that backend's last known state to avoid false stop/start flapping; a permanent outage can therefore extend a session until detection recovers or the tracker shuts down.

## v0.2 scope and future plans

This release intentionally has no graphical or menu-bar interface. It does not implement achievements, streaks, XP, an Agent Card, cloud sync, telemetry, or a browser extension.

Steam-style achievements and shareable profile cards are planned for a later release. Those ideas are future plans; v0.2 is deliberately focused on reliable runtime detection and local, period-based statistics.

## License

AgentPlaytime is available under the [MIT License](LICENSE).

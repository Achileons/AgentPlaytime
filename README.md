# AgentPlaytime

> "Track your AI playtime. Unlock achievements. Question your life choices."

AgentPlaytime is a macOS-first, local-first command-line application that measures how long supported AI applications and agent processes are running. It is **not** an active-window tracker: foreground and background do not matter. If a supported tool is running, its timer runs; when the tool stops, its timer stops. The optional v0.3 background service excludes Mac sleep when power monitoring is active.

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

**Only application/process runtime metadata, service state, power-event timestamps, and local operational logs are stored.** Session data stays locally in SQLite at `~/Library/Application Support/AgentPlaytime/agentplaytime.db` by default. There is no telemetry, cloud sync, or browser extension. Service diagnostics filter `launchctl print` down to runtime fields before Python receives its output; argument and environment sections are discarded.

## What v0.3 does

Version 0.3 adds an optional macOS background service to the existing runtime tracker and period-based local statistics for these supported tools:

- ChatGPT Desktop
- Claude Desktop
- Claude Code CLI
- Codex CLI or desktop, when detectable
- Cursor

Native macOS applications are identified as applications so their helper and renderer processes do not become separate timers. Command-line agents are found through process inspection. Multiple instances of the same tool still count as one logical running timer.

AgentPlaytime polls roughly every two seconds and records each session's tool, UTC start time, and UTC end time. Switching to another application does not stop a session. Statistics can summarize today, the last seven local calendar days, or all recorded time, including sessions that are still running.

The service keeps tracking when Terminal is closed, starts at user login, handles sleep/wake events, and writes bounded local logs. The existing database schema is unchanged; no migration or reset is needed.

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
    },
    {
      "tool": "Codex",
      "total_seconds": 3240,
      "session_count": 2,
      "average_session_seconds": 1620,
      "longest_session_seconds": 2100,
      "running": false
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

## macOS background service

Installing the Python package does **not** install or start a LaunchAgent. Opt in explicitly, from the installed virtual environment:

```console
source .venv/bin/activate
agentplaytime service install
agentplaytime service status
```

No `sudo` or administrator access is needed. This is a per-user **LaunchAgent**, not a system LaunchDaemon. It runs in `gui/<uid>` while you are logged in to a macOS graphical session. It does not run before login or while the Mac is powered off. After installation you may close Terminal; the service does not depend on shell activation.

### Commands

| Command | Behavior |
| --- | --- |
| `agentplaytime service install` | Validate Python and the installed module, atomically write the plist, bootstrap and start. Repeating with identical settings reuses the existing service. Changed settings gracefully stop the old job before replacement. |
| `agentplaytime service start` | Bootstrap an installed, unloaded job, or kickstart a loaded but stopped job. Already running is a successful no-op; not installed is an error. |
| `agentplaytime service stop` | Boot out the job, requesting graceful shutdown without KeepAlive restarting it. Already stopped/absent is a no-op. The plist remains: explicit start or the next login can start it again. |
| `agentplaytime service restart` | Gracefully stop, then start. Not installed is an error. |
| `agentplaytime service status` | Read-only installed/loaded/running state, PID, paths, configuration validity, tracker state, power monitoring, warnings and errors. Also supports `--json`. |
| `agentplaytime service uninstall` | Boot out the job and remove only its validated plist. Already absent is a no-op. Database, sessions, logs and other LaunchAgents are preserved. |

`install`, `start` and `restart` request startup; launchd may still report a pending/stopped job briefly. Recheck `service status`. A process being **running** does not necessarily mean it has acquired the tracker lock: `Tracker: waiting_for_lock` means manual tracking owns that database. `Power monitoring: active` requires a fresh health report from the matching service PID, not merely an importable API. Missing/stale health reports show `unknown` (including after a long sleep until the runner updates them).

Status exits `0` for a healthy configuration, including intentionally stopped or not installed; `2` for a broken/unsafe configuration; `1` when launchctl state cannot be determined. Other lifecycle errors return `1`. Successful lifecycle operations print the resulting status and inherit its exit code. `doctor` remains an informational command returning `0`, with errors included in its output.

### Files and launch configuration

- Label: `com.agentplaytime.tracker`
- Plist: `~/Library/LaunchAgents/com.agentplaytime.tracker.plist`
- Database: `~/Library/Application Support/AgentPlaytime/agentplaytime.db`
- Shared tracker lock: the database filename plus `.lock`
- Log: `~/Library/Logs/AgentPlaytime/tracker.log`
- Health metadata: `~/Library/Logs/AgentPlaytime/service-state.json`

The plist is generated with `plistlib`, stored with owner-only permissions, and atomically replaced in its directory. Symlink targets, foreign labels and files/directories writable by other users are refused. Uninstall never deletes directories or follows a plist symlink.

`ProgramArguments` contains separate arguments: the absolute virtual-environment Python path, `-I -m agentplaytime.service.runner`, and explicit database, log and interval options. Spaces in paths are supported; no shell is used. Python's isolated mode avoids dependence on the current directory, shell activation or `PYTHONPATH`. The virtualenv interpreter symlink is intentionally not resolved to the base Python executable.

Configuration: `RunAtLoad=true`, `KeepAlive=true`, `ProcessType=Background`, `ThrottleInterval=30`, `ExitTimeOut=30`, `LimitLoadToSessionType=Aqua`, `Umask=077`. Modern `launchctl bootstrap`, `bootout`, `kickstart` (without force-killing an existing worker), and `print` manage the exact user-domain job. launchd can restart a crashed worker, throttled to prevent a rapid retry loop. The service uses the default two-second interval; a public service configuration editor is not part of v0.3.

### Manual tracker conflicts

Manual tracking and the background runner share the same non-blocking exclusive lock for the same database. If the service owns it, `agentplaytime track` fails cleanly. If a manual tracker owns it, the service stays alive and waits for the lock, without opening its database or creating sessions; it takes over once the manual tracker releases the lock. Statistics and doctor remain usable during tracking. Different databases have independent locks.

To return to manual mode permanently, uninstall the service; this preserves all recorded data:

```console
agentplaytime service uninstall
agentplaytime track
```

For a temporary switch, use `service stop` followed by `track`. After stopping manual tracking with `Ctrl-C`, use `service start` to resume the background service.

### Sleep, wake and shutdown

The background runner subscribes to `NSWorkspaceWillSleepNotification` and `NSWorkspaceDidWakeNotification` on the NSWorkspace notification center through the existing PyObjC dependency. Callbacks record only the event kind and UTC timestamp in a thread-safe queue; they never write to SQLite. A single runner loop pumps Cocoa and serializes polling, power events and database changes.

On sleep, all active tool sessions end at the notification timestamp. On wake, detector caches are reset and currently running tools are detected again; new sessions begin at the wake timestamp. Queued notifications are processed in timestamp order even if the process cannot reconcile until after wake. A detector sample spanning a power event is discarded. The pre-sleep and post-wake intervals remain separate sessions, so sleep time is excluded.

If the power API cannot be registered or its run loop fails, logs and runtime diagnostics explicitly show **degraded** monitoring. Tracking continues, but sleep exclusion is not guaranteed. If monitoring fails while marked asleep, tracking resumes from the current observation without filling the sleep gap. Restart after resolving an API failure to retry registration. `doctor` distinguishes API availability from actual runtime monitoring.

SIGINT/SIGTERM request shutdown on the owning loop and close sessions before closing SQLite and releasing the lock. launchd allows up to 30 seconds for graceful termination. Forced kills, sudden power loss or an unresponsive detector can prevent cleanup.

### Logs and troubleshooting

Python's rotating file handler limits logs to **1 MiB per file plus 3 backups**, approximately 4 MiB total. Logs contain only version, service start/stop, allowlisted tool transitions, power timestamps, generic backend warnings and exception type names. Raw detector exceptions, subprocess output, prompts, process arguments and environment dumps are never logged. Health metadata is a small atomic JSON file refreshed approximately every 30 seconds or when runtime state changes. Logs and metadata remain local and survive uninstall.

Start troubleshooting with:

```console
agentplaytime service status
agentplaytime doctor
agentplaytime doctor --json
tail -n 50 "$HOME/Library/Logs/AgentPlaytime/tracker.log"
```

- **Not installed:** run `service install` when you are ready to enable background tracking.
- **Waiting for lock:** stop the manual tracker with `Ctrl-C`; do not delete its lock file or database.
- **Python missing / module cannot be imported:** keep the project and virtualenv in a stable location. If moved/deleted, recreate the environment, reinstall the package, then run `service install` from that environment to update paths. Do not rename/move the virtualenv while the service uses it.
- **Malformed/unsafe plist:** inspect only the displayed AgentPlaytime plist. A foreign label or malformed XML is deliberately not overwritten; move that exact file aside yourself after checking it, then reinstall. Do not delete the LaunchAgents directory. A syntactically valid AgentPlaytime plist with outdated settings can be repaired by `service install`.
- **launchctl failure:** run from your own logged-in GUI session. Check macOS System Settings → General → Login Items & Extensions for blocked background activity. AgentPlaytime will not bypass OS controls or request `sudo`.
- **Running but no tracking / repeated exits:** check the bounded log, configured paths and module import warning; then use `service restart` after addressing the cause.
- **Power API available but monitoring unknown/degraded:** API availability alone does not prove the worker subscribed successfully. Check service health/logs and restart to retry. Actual sleep/wake behavior should be smoke-tested on your Mac after you explicitly install the service.
- **Stats cannot open the database in a restricted sandbox:** SQLite can require WAL/shared-memory sidecars even for a read-only connection. Use your normal Terminal with access to the data directory; do not reset/delete the database. `stats` uses a read-only SQLite connection and does not initialize or migrate the schema; a missing database returns empty statistics without creating files.

## Detection notes and v0.3 limitations

AgentPlaytime uses `NSWorkspace` bundle identifiers for native applications and `psutil` executable metadata for command-line agents. If a restricted macOS environment returns an empty `NSWorkspace` result, a fallback reads only exact, known application-service labels from `launchctl`; unrelated output is filtered before Python receives it.

`doctor` labels bundle rules that were verified on the current development Mac separately from compatibility candidates that could not be verified here. Display names alone are diagnostic hints and never start a timer.

Graceful `SIGINT`/`SIGTERM` shutdown closes every active session. After a hard crash or power loss, v0.3 still cannot know whether a tool ran continuously during the tracker outage, so a previously open session is resumed until the next observation. Such an outage can overcount time; automatic restart does not reconstruct missing history. During a detector-backend outage, the tracker warns and retains that backend's last known state to avoid false stop/start flapping; a permanent outage can therefore extend a session until detection recovers or the tracker shuts down.

Sleep exclusion is implemented in the **background runner**; the existing manual `track` command retains its original behavior and may count sleep. App runtime is not attention, productivity, or keyboard activity; screen lock or an idle awake Mac does not pause tracking. Missed power notifications cannot be reconstructed. For several delayed sleep/wake cycles, wake detection uses the tools available when events are processed, not a recoverable historical process snapshot. Backward wall-clock jumps are clamped to avoid negative new session durations; historical sessions and unobserved clock/sleep changes can remain imprecise.

Automated service tests use temporary homes/data/logs, fake launchctl runners, synthetic power events and an isolated synthetic child for SIGTERM. They never install a real LaunchAgent or invoke real mutating launchctl commands. Both source and installed-package suites should be run when changing service integration:

```console
PYTHONPATH=src .venv/bin/python -m pytest
.venv/bin/python -m pip install --force-reinstall --no-deps --no-build-isolation .
.venv/bin/python -m pytest
```

## v0.3 scope and future plans

This release intentionally has no graphical or menu-bar interface. It does not implement achievements, streaks, XP, an Agent Card, cloud sync, telemetry, or a browser extension.

Steam-style achievements and shareable profile cards are planned for a later release. Those ideas are future plans; v0.3 is focused on background tracking, sleep/wake integration, and local, period-based statistics.

## License

AgentPlaytime is available under the [MIT License](LICENSE).

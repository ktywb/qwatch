# qwatch

`qwatch` is a read-only terminal monitor for Intel/Altera Quartus Prime
compilations on Linux. It reads the same compiler databases used by Quartus
Monitoring Mode without launching an additional `quartus_sh` process.

## Data sources

- `runlog.db`: flow and task status
- task `.qmsgdb` files: incremental compiler messages
- `state.meta` and report files: checkpoint/completion events
- `/proc`: CPU, memory, I/O, thread, process and liveness metrics
- `flow.<run>.heartbeat`: stale-flow detection when available

Build logs are display-only and are never used to determine compilation state.

## Requirements

- Linux with `/proc`
- Python 3.10 or newer
- A Quartus project whose compiler database is under `qdb/_compiler`

No third-party Python packages are required.

## Compatibility

The current release has been tested on Linux with **Quartus Prime Pro 25.1.0,
Build 129, Patches 0.36 SC**. Other Quartus editions and releases have not yet
been verified.

`qwatch` intentionally validates its inputs, but some inputs are internal
Quartus implementation details rather than stable public APIs:

- Compiler status discovery currently expects
  `qdb/_compiler/**/legacy/*/runlog.db`.
- The `status` table is schema-checked before use. Required fields include task
  identity, run name, percentage, status, timestamps, process/parent IDs,
  checkpoint/result fields, success and diagnostic counts, and hostname. An
  incompatible database fails explicitly with `unsupported runlog schema`
  instead of being interpreted silently.
- Flow rendering currently recognizes the English Quartus Prime Pro task names
  `Analysis & Synthesis`, `Analysis & Elaboration`, `Synthesis`, `Fitter`,
  `Plan`, `Place`, `Route`, `Fast Forward`, `Retime`, `Fitter (Finalize)`,
  `Assembler`, and `Timing Analysis (Finalize)`. Future Quartus naming changes
  may require a compatibility mapping.
- Process metrics and active `.qmsgdb` discovery use Linux `/proc`, including
  `/proc/<pid>/stat`, `status`, `io`, `fd`, and `/proc/uptime`. Other operating
  systems are not currently supported.

Compatibility should therefore be verified explicitly when adding a new
Quartus release, especially across major-version changes.

## Usage

Run from the Quartus project root:

```console
python3 /path/to/qwatch.py PROJECT [INTERVAL] [LOG_OR_AUTO] [LOG_LINES]
```

For example:

```console
python3 /path/to/qwatch.py vpart_pcie 1
```

Useful options:

```text
--root PATH         Quartus project root (default: current directory)
--stitch-gap SEC    Maximum gap between linked Quartus invocations (default: 60)
```

Keys:

- `q` or `Ctrl-C`: exit
- `Space`: pause/resume data sampling
- `+` / `-`: shorten/lengthen the sampling interval
- `Up` / `Down`: scroll messages or the optional build log
- `m`: switch between Quartus messages and the build-log view, when enabled

## Optional build-log view

The default UI displays Quartus `.qmsgdb` messages only. To enable the optional
build-log view, pass a log path explicitly:

```console
python3 /path/to/qwatch.py vpart_pcie 1 path/to/build.log 20
```

Passing `auto` enables a convenience fallback that selects the newest
`logs/*.latest.log` file:

```console
python3 /path/to/qwatch.py vpart_pcie 1 auto 20
```

This naming convention is not provided by Quartus. The build log is used only
for display and never affects flow detection, task status, or session grouping.

Terminal resizes redraw immediately from the cached snapshot and do not trigger
extra SQLite or `/proc` sampling.

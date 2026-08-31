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

## Usage

Run from the Quartus project root:

```console
python3 /path/to/qwatch.py PROJECT [INTERVAL] [LOG_OR_AUTO] [LOG_LINES]
```

For example:

```console
python3 /path/to/qwatch.py vpart_pcie 1 auto 20
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
- `m`: switch between Quartus messages and the build-log view

Terminal resizes redraw immediately from the cached snapshot and do not trigger
extra SQLite or `/proc` sampling.

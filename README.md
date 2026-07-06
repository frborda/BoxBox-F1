# F1 Live Telemetry

A Windows desktop app that charts **Formula 1 telemetry live** (speed, throttle,
brake, RPM, gear) for one or several cars at once, with **distance on the X
axis** and one series per driver. Built with
[Fast-F1](https://github.com/theOehrly/Fast-F1), PySide6 and pyqtgraph.

![F1 Live Telemetry — Race mode replaying a race, with timing tower and track map](docs/screenshot.png)

**Times / Gap** — gap to a reference driver over the whole race, tyre
degradation per stint and the timing tower with pit stops and averages:

![Times / Gap mode with degradation analysis](docs/screenshot-gap.png)

**Quali comparison** — live laps against a target lap, with the cumulative
delta trace and per-sector/microsector delta cards updating in real time:

![Quali comparison mode against a target lap](docs/screenshot-quali.png)

## Modes

| Mode | Behaviour |
|------|-----------|
| **Race** | Sliding window configurable in laps (½ to 20 or the whole session; default 1), plus free space on the right so the latest values and each series' label stay visible. The X axis is the **track position** ((lap − 1) × lap length + lap meter), so the same corner falls on the same vertical for every car: braking points line up even when a car runs behind. |
| **Race 2** | Fixed X axis from 0 to the last meter of the lap. Each series overwrites ("eats") its own previous-lap line as it advances, with a visible gap ahead of each car's cursor. |
| **Quali** | Comparison of the current lap against a **target lap** (any completed lap of any driver). Three levels: the channel traces (dashed target + live current laps), the **cumulative delta trace** (X = distance, Y = seconds gained/lost vs the target at every meter, X axes linked), and **per-driver cards** (up to 4, in a 2-column grid) with the total lap delta in large type and one row per sector: the sector chip on the left and its 8 microsectors aligned next to it — no horizontal scrolling; green = doing better, red = worse, and the most recently crossed microsector is highlighted. |
| **Times / Gap** | Gap chart against a reference driver (marked "(ref)" in the legend; X = **track position**, with "total distance / L\<lap\> +\<meters\>" ticks and a vertical line at every lap boundary; window configurable from ½ to 20 laps or the whole session; Y = seconds, **+ = slower than the reference at the same position, − = faster**) plus comparison tables ordered by track position: summary (P1..Pn, current lap, last, best, S1-S3, gap), lap times per driver, microsector deltas (24 splits, green/red cells) and per-corner minimum speeds. Sectors and microsectors are **rolling** (current lap in real time; until crossed, the value from one lap ago, dimmed) and each driver's most recently completed microsector is highlighted. |

## Data sources

- **Demo (synthetic)** — 6 simulated cars on a fictional circuit. No network
  needed; try the app and all its modes any time.
- **Replay (Fast-F1 historical)** — replays any real session (2018 onwards) as
  if it were live, with a speed multiplier (x1 to x25). The first load of a
  session downloads data (may take a few minutes); it is cached afterwards.
- **Live (F1 Live Timing)** — SignalR Core client for
  `livetiming.formula1.com/signalrcore`. It decodes `CarData.z` (speed, RPM,
  gear, throttle, brake, DRS at ~4 Hz per car) and `Position.z`, integrates
  speed to obtain distance and takes lap numbers from `TimingData`. **Data
  only flows while an official session is running**, and the full stream
  requires an **F1TV subscription token** (the same one Fast-F1 uses; sign in
  from the capture window). Every message is recorded to
  `%LOCALAPPDATA%\f1telem\recordings\`.
- **Capture (recorded live)** — follows a capture file written by the
  **capturer** with minimal delay (new lines are decoded as soon as they hit
  the disk, ~50 ms). The timeline lets you seek back anywhere in the session
  while the capture keeps growing, and the red **LIVE** button jumps back to
  the latest data.

## Capturer

A companion app that ONLY captures the live stream to a file, so the
visualizer (this app, even multiple instances) can follow it live or rewind
without touching the network connection:

```powershell
F1LiveTelemetry.exe --capture     # or: python -m f1telem --capture
```

It shows the output file, connection status and data counters, and offers
**Sign in with F1TV…** (browser flow, token shared with Fast-F1). Then open
the main app and pick the *Capture (recorded live)* source: it automatically
follows the most recent capture file.

## Features

- **Pause and hot speed change** (demo/replay): the ⏸ button next to the
  timeline pauses/resumes, and the speed selector can be changed at any
  moment without reconnecting.
- **Timeline** (replay only): a slider below the charts seeks to any point of
  the session; jumping rebuilds the whole state up to that instant (drivers,
  selection and the target lap are kept) and playback continues from there. A
  **ruler marks the start of every lap** (numbered adaptively); clicking a
  mark jumps straight to that lap. Pit stops show as diamonds (driver color),
  flag/SC periods as colored bands and rain as a thin blue stripe. The
  timeline stays active after the replay ends so you can seek back.
- **Timing tower** (right panel, above the map): every car ordered by track
  position, with gap to the leader (or "+nL" when lapped), interval to the
  car ahead, last lap, best lap (green = personal best, purple = session
  best), pit-stop count and **AVG5/AVG10** (average of the last 5/10 laps,
  excluding pit in/out laps). Gaps only compute with real positions: at the
  start, telemetry does not know each car's exact grid slot, so gaps begin at
  the end of sector 1 of lap 1 — the first fixed point common to all cars —
  using a grid offset estimated by projecting car positions onto the track.
- **Weather**: air and track temperature, wind and rain in the status bar
  (synchronized with the replayed instant).
- **Browsable session picker**: the GP field is a combo with the year's
  calendar (loaded in the background via Fast-F1); free typing still works.
- **Stint summary** (Degradation tab): average pace and degradation slope
  (s/lap, linear fit) per stint and compound, next to the lap-time vs
  tyre-age chart (one series per stint, colored by compound).
- **Tyres and strategy** (replay): the "By lap" table tints every cell by
  compound, adds the tyre age in parentheses and marks pit-stop laps with
  "P".
- **Flags and Safety Car**: background bands on the gap chart and the
  timeline, and a banner on the tower while yellow/SC/VSC/red is active.
  While a marshal sector is under yellow flag, that stretch of the **track
  map is painted yellow** (replay: race control messages + Fast-F1 marshal
  sectors).
- **Real corners** ("Corners" tab): minimum speed at each numbered corner of
  the circuit (T1, T2, … from Fast-F1 `circuit_info`), rolling over the
  current lap and colored against the reference; corners are also labelled on
  the map.
- **Track map** (right panel, below the tower): the track outline with each
  selected driver's current position (dot + code + 5-second trail fading
  toward its tail, toggleable), synchronized with the same clock as the
  charts. In demo and replay the outline is immediate; live, it builds itself
  once a car completes a lap (`Position.z` feed).
- **Chart ↔ map correlation**: hovering a chart shows a ring on the map at
  the corresponding track point; hovering the map outline shows a vertical
  reference line on the active chart at the corresponding lap meter.
- **Chart interactions**: crosshair tooltip with every visible series' value
  at the cursor; double-click a line to hide it (double-click empty space to
  restore); optional text labels on significant peaks (straight-end top
  speeds above, corner minimum speeds below); the **X window** selector sets
  the axis width in laps for Race and Times/Gap (each mode remembers its
  own).
- **Smooth rendering**: 30 fps refresh; sliding windows and each series' tip
  interpolate between telemetry batches (never predicting ahead of real
  data), so lines draw continuously and precisely.
- **PostgreSQL (optional)**: with **Record** enabled, every sample is
  inserted into a local `telemetry` table (created automatically). If the
  database is unavailable, the app keeps working and reports it in the
  status bar.

## Usage (development)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.\run.ps1
```

1. Pick a source and press **Connect**.
2. Check the drivers you want to chart — the list is alphabetical and has a
   **Select all** (teammates share the team color and are distinguished by
   line style).
3. Switch mode and channel at will; in **Quali**, pick the target driver and
   lap (each lap shows its time) and press **Set**.

## Windows executable

```powershell
.\build.ps1
```

Produces `dist\F1LiveTelemetry\F1LiveTelemetry.exe` (self-contained folder,
no Python required).

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python tests\smoke.py         # full app with the demo source + live decoder
.venv\Scripts\python tests\replay_check.py  # real Fast-F1 integration (downloads data)
```

## Technical notes

- Live distance is integrated trapezoidally from speed; lap length is
  estimated as the median of observed laps (in replay it is computed exactly
  from Fast-F1 data).
- Lap/sector/microsector times are interpolated from the crossing instant of
  24 distance marks per lap. With ~4-5 Hz telemetry the accuracy is about
  ±0.1 s — good for comparisons, not official timing — and "sectors" are
  distance thirds of the lap (not the official ones). The gap between cars is
  the time difference when passing the same track position, with each lap
  anchored to its own finish-line crossing.
- Fast-F1 cache: `%LOCALAPPDATA%\f1telem\cache`. Settings:
  `%APPDATA%\f1telem\config.json`.

## Credits

- **[Fast-F1](https://github.com/theOehrly/Fast-F1)** by
  [@theOehrly](https://github.com/theOehrly) — this project relies on Fast-F1
  for historical session data, lap and telemetry parsing, circuit info,
  weather, race control messages and the event schedule. Huge thanks to its
  author and contributors.
- Live data comes from the public F1 live timing stream.

## Disclaimer

This is an unofficial project and is not associated in any way with the
Formula 1 companies. F1, FORMULA ONE, FORMULA 1, FIA FORMULA ONE WORLD
CHAMPIONSHIP, GRAND PRIX and related marks are trademarks of Formula One
Licensing B.V.

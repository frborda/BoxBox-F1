# F1 Live Telemetry

A Windows desktop app that charts **Formula 1 telemetry live** (speed, throttle,
brake, RPM, gear) for one or several cars at once, with **distance on the X
axis** and one series per driver. Built with
[Fast-F1](https://github.com/theOehrly/Fast-F1), PySide6 and pyqtgraph.

![F1 Live Telemetry — multi-window layout during a Safety Car: timing tower, Race 2 chart, lap wheel with live intervals, drivers, race trace, track map with a yellow sector and the timeline](docs/data.png)

**Control hub** — the whole app is driven from one narrow window: data
source, the window catalog (Drivers and Timeline featured), window
profiles and settings. Every view opens in its own window:

![Control hub with the window catalog and profiles](docs/main.png)

**Race mode** — telemetry charts while replaying a race, with timing tower
and track map:

![Race mode replaying a race, with timing tower and track map](docs/screenshot.png)

**Times / Gap** — gap to a reference driver over the whole race, tyre
degradation per stint and the timing tower with pit stops and averages:

![Times / Gap mode with degradation analysis](docs/screenshot-gap.png)

**Quali comparison** — live laps against a target lap, with the cumulative
delta trace and per-sector/microsector delta cards updating in real time:

![Quali comparison mode against a target lap](docs/screenshot-quali.png)

## All-windows model

The visualizer is a compact **control hub** plus independent windows. The
hub only manages the data input (source, Connect, timeline), the driver
selection, the **window catalog** (every view listed with a one-line
description and an open/close toggle), the **window profiles**
(save/apply/delete complete arrangements — e.g. "Race 3 monitors", "Quali
compact"; three factory presets are seeded on first run) and general
settings. Everything else — every chart, the tower, the map, each context
panel — lives in **its own window**: freely placed on any monitor, pinnable
(frameless, always on top) over a broadcast, and each chart carries **its
own controls** (channel, X window), so two Race windows can show different
channels at the same time. Closing a window just hides it; its position,
size and pin are remembered and the whole arrangement is restored on the
next start.

## Views

| Window | Behaviour |
|------|-----------|
| **Race** | Sliding window configurable in laps (½ to 20 or the whole session; default 1), plus free space on the right so the latest values and each series' label stay visible. The X axis is the **track position** ((lap − 1) × lap length + lap meter), so the same corner falls on the same vertical for every car: braking points line up even when a car runs behind. |
| **Race 2** | Fixed X axis from 0 to the last meter of the lap. Each series overwrites ("eats") its own previous-lap line as it advances, with a visible gap ahead of each car's cursor. |
| **Quali** | Comparison of the current lap against a **target lap** (any completed lap of any driver). Three levels: the channel traces (dashed target + live current laps), the **cumulative delta trace** (X = distance, Y = seconds gained/lost vs the target at every meter, X axes linked), and **per-driver cards** (up to 4, in a 2-column grid) with the total lap delta in large type and one row per sector: the sector chip on the left and its 8 microsectors aligned next to it — no horizontal scrolling; green = doing better, red = worse, and the most recently crossed microsector is highlighted. |
| **Times / Gap** | Gap chart against a reference driver (marked "(ref)" in the legend; X = **track position**, with "total distance / L\<lap\> +\<meters\>" ticks and a vertical line at every lap boundary; window configurable from ½ to 20 laps or the whole session; Y = seconds, **+ = slower than the reference at the same position, − = faster**) plus comparison tables ordered by track position: summary (P1..Pn, current lap, last, best, S1-S3, gap), lap times per driver, microsector deltas (24 splits, green/red cells) and per-corner minimum speeds. Sectors and microsectors are **rolling** (current lap in real time; until crossed, the value from one lap ago, dimmed) and each driver's most recently completed microsector is highlighted. |
| **Race trace** | Classic race-trace: each driver's cumulative gap against a **selectable reference** (the leader by default, or any driver), with **one point per official microsector** so the effect of every corner shows up, not just the per-lap cut. X = laps (configurable: last N laps or all), Y = seconds (configurable ±s or auto; losing time = the line drops). SC/VSC/flag periods are shaded in the background. |

## Data sources

The visualizer consumes two sources; going live against the F1 stream is the
**capturer's** job (its own executable, next section):

- **Replay (Fast-F1 historical)** — replays any real session (2018 onwards) as
  if it were live, with a speed multiplier (x1 to x25). The first load of a
  session downloads data (may take a few minutes); it is cached afterwards.
- **Capture (live / imported)** — follows a capture file written by the
  **capturer** with minimal delay (new lines are decoded as soon as they hit
  the disk, ~50 ms), whether it is a real live capture in progress or an
  imported one replayed as live. The visualizer **manages the capturer**:
  pressing Connect attaches immediately if a capture file is already
  growing; otherwise it **opens the capturer automatically** (if it is not
  already running — a heartbeat file tells) and stays waiting, connecting
  **by itself the moment data starts flowing** (start a live capture or an
  import in the capturer). The wait can be cancelled with the same button. The live client (SignalR Core against
  `livetiming.formula1.com/signalrcore`) decodes `CarData.z` (speed, RPM,
  gear, throttle, brake, DRS at ~4 Hz per car) and `Position.z`, integrates
  speed to obtain distance and takes lap numbers from `TimingData`. **Data
  only flows while an official session is running**, and the full stream
  requires an **F1TV subscription token** (sign in from the capturer). Every
  message is recorded to `%LOCALAPPDATA%\f1telem\recordings\`. The timeline
  lets you seek back anywhere in the session while the capture keeps
  growing, and the red **LIVE** button jumps back to the latest data.

(For development, `F1TELEM_DEV_SOURCES=1` adds the synthetic Demo and the
direct Live client to the source list.)

## Capturer

A companion app that ONLY captures the live stream to a file, so the
visualizer (this app, even multiple instances) can follow it live or rewind
without touching the network connection. It is a **separate executable**
(`F1TelemCapture.exe`, its own `capture\` folder with an independent
`_internal`) so the main app can be updated **without stopping a running
capture** — the auto-updater replaces the visualizer and leaves the capturer
untouched while it keeps recording (it updates the next time it is closed).

```powershell
F1TelemCapture.exe        # or: .\capture.ps1  ·  python -m f1telem --capture
```

### Import a recorded capture (replay as live)

The capturer's **Import capture…** button replays any recorded `.jsonl`
**as if the stream were arriving live**, transparently for the main app — it
follows the new file with its *Capture (recorded live)* source and cannot
tell real from imported. The model mirrors a real live session: you pick
**where real-time playback starts** (`hh:mm:ss`, e.g. `00:01:30`), the
**whole history from the race start up to that point is delivered
instantly** (the main app always gets the complete picture, exactly like
when it hooks into an ongoing live capture), and from there the data flows
chronologically at real speed — no pause, no rewind, just like a live feed.
Seeking around the received data is done in the main app's own timeline, as
always. Playback uses a single working file (`import_live.jsonl`),
overwritten on each import and removed on exit — replaying never piles up
new capture files.

It shows the output file, connection status and data counters, and offers
**Sign in with F1TV…** (browser flow, token shared with Fast-F1). Then open
the main app and pick the *Capture (recorded live)* source: it automatically
follows the most recent capture file.

### F1TV sign-in: setup and login procedure

**What you need**

- An active **F1TV Access/Pro/Premium subscription** (the live stream needs
  it; without a token the app still connects but data may be partial).
- A Chromium browser (Chrome/Edge/Brave) **on the same machine as the
  capturer** — the login hands the token to the app through `localhost`.
- The **companion extension** installed once (next section). The official
  *FastF1 Companion* extension is outdated — Chrome 130+ blocks its request
  to `localhost` (Local Network Access), which shows up as *"Could not
  connect to the local FastF1 application"* — so this repo ships its own
  drop-in replacement.

**Install the extension (once)**

1. Locate the `extension` folder: next to `F1LiveTelemetry.exe` in the
   release, or at the repo root.
2. Open `chrome://extensions` in Chrome, enable **Developer mode**
   (top-right toggle) and click **Load unpacked**.
3. Pick the `extension` folder — "F1 Live Telemetry Companion" appears in
   the list. Keep the folder where it is (Chrome loads it from there).
4. If you have the old *FastF1 Companion*, **disable it** in
   `chrome://extensions` — both react to the same sign-in URL and would
   race each other.
5. If the app's sign-in opens a different browser, either make Chrome your
   default browser or copy the sign-in URL shown in the capturer's status
   line into Chrome manually.

**Login procedure**

1. In the capturer press **Sign in with F1TV…** — the app starts a local
   listener and opens the sign-in URL in your browser (up to 15 minutes;
   keep the capturer open).
2. The extension takes you to the **Formula 1 account login** — sign in
   with your F1TV credentials (2FA included if you use it).
3. When the login lands on *my account*, the extension opens its **Connect**
   page and delivers the token to the app automatically — it declares
   `targetAddressSpace: "loopback"` (the Chrome 130+ requirement) and the
   app's local server answers with `Access-Control-Allow-Private-Network:
   true`, the two halves of the Local Network Access handshake.
4. The capturer's **F1TV** line switches to *"token found (authenticated)"*.
   Done — the token is stored in Fast-F1's `f1auth.json`, shared with
   anything else that uses Fast-F1, and reused on the next runs until it
   expires (then just sign in again).

**If the automatic delivery fails**

- The extension's Connect page offers **Open in the app**: the browser
  shows its native *"Open F1 Live Telemetry?"* dialog and hands the token
  straight to the executable through the `f1telemetry://` link (the
  capturer registers it per-user on start, no admin needed) — the running
  capturer picks it up within seconds.
- It also shows the token with a **Copy** button: copy it, then press
  **Paste token…** in the capturer and paste.
- Without any extension, **Paste token…** always works: sign in at
  `f1tv.formula1.com`, open DevTools (F12) → **Application** → **Cookies**
  → `https://f1tv.formula1.com`, copy the **value** of the `login-session`
  cookie and paste it (the raw subscription JWT is accepted too).

![Capturer recording the live stream during a race, authenticated with F1TV](docs/capture.png)

## Features

- **One window per view** (see *All-windows model* above): every view opens
  from the hub's catalog into its own window with a 📌 **pin** button
  (frameless, always-on-top, immovable — ideal over a broadcast). Any
  combination can be assembled across monitors; every open window keeps
  refreshing. The arrangement is **persistent** — geometry, pin and open
  state are reapplied exactly on the next start. The Times/Gap tables and
  the Quali cards can additionally be popped out of their parent window
  with their own ⧉ button.
- **Window profiles**: the hub saves the complete current arrangement
  (which windows are open, where, sizes, pins) under a name and reapplies
  it with one click; factory presets **Race / Quali / Strategy** are
  created on first run.
- **Session strip** (banner above the charts, detachable like everything
  else): meeting/session name, live **track-status badge**
  (clear/yellow/SC/VSC/red), **LAP n/total** (races) and the **session clock**
  (`ExtrapolatedClock`), plus the latest race-control message colored by
  flag.
- **Race control panel**: chronological log of every official message —
  flags, SC/VSC deployments, investigations and penalties — with session
  timestamps and lap numbers, colored by flag.
- **Tyre strategy panel**: one bar per driver (ordered by position) with the
  stints colored by compound (F1 convention) and the stint length inside,
  clipped to the race distance, with a dashed line at the leader's current
  lap. The tower also shows each car's **current compound and tyre age**
  next to the driver code. Live data comes from `TimingAppData`; replay uses
  Fast-F1 laps.
- **Lap wheel**: a circular lap view — north is the start/finish line,
  south is half distance, and each car sits at its lap-fraction angle in
  real time (smoothed). The ring is split into the three sectors (official
  boundaries once derived), carries every numbered corner as a tick, paints
  **yellow-flag sectors** like the track map and marks the **pit lane** as a
  dashed arc once the first stop locates it. An inner **interval ring**
  draws an arc between each pair of consecutive cars with the live gap in
  seconds (1 decimal), rotating and re-scaling as the race evolves. Pick a
  driver in **Pit sim** and a dashed **PIT ghost** marks where they would
  drop if they pitted now — using the Pit strategy window value — with the
  projected position and margin in the header.
- **Pit strategy panel (Ventana de Box)**: the real cost of a pit stop is
  not the pit-lane time — braking in and accelerating out also lose time.
  The app measures a **box window on track** (from 2 microsectors before
  the pit entry to 2 after the pit exit) and compares a pitting car's
  crossing (stop **normalized to a standard 3 s**) against the clean
  reference: the average of the **last 3 clean laps (track clear, no
  pitting) of the race's top 5**. The value auto-updates as stops happen,
  and can be **edited manually with a lock** so the automatic estimate
  never overwrites it. Below it, a live **rejoin projection** per driver:
  if they pit now (3 s stop), the predicted position and the car/margin
  they would come out behind.
- **Pit lane panel**: who is in the pit lane **right now**, the compound
  they entered on, and two live clocks — total time in the lane and time
  stationary (speed 0, from telemetry). The tower also shows each driver's
  **last pit visit**: lap, time spent in the lane and time stopped (in
  yellow while the car is still in the lane). Live detection uses the
  official `InPit` flag; replay pairs Fast-F1 `PitInTime`/`PitOutTime`.
- **Notification manager**: popup toasts (bottom-right, auto-dismiss) and a
  log panel for session events — pit in / pit out (with lane and stopped
  times), session fastest lap, **car stopped on track** (speed 0 outside
  the pits, sustained), yellow flag, safety car, virtual safety car, red
  flag and stewards' **penalties**. Each category can be toggled
  individually and popups can be disabled; on connect, pre-existing history
  sets a baseline silently (no notification flood), and timeline seeks
  never re-announce past events.
- **Weather panels**: current values (air, track, wind, rain) plus a
  **weather evolution chart** — air/track temperature above and wind speed
  below, with rain shaded — where the X axis is the **leader's race lap**
  during races or the minutes since the session start otherwise.
- **Tower font size**: A− / A+ buttons in the tower header scale its font
  and row heights (persisted).
- **Capturer status in the hub**: a live line under the Connect button —
  not running / running idle / capturing / importing, with the write rate
  in MB/min — so you always know what the capturer is doing.
- **Overlay opacity**: pinned windows gain an opacity slider (55–100%) in
  their title bar — a semi-transparent tower or session strip over the
  broadcast; per-window and persisted.
- **Timeline previews**: hovering the lap ruler or dragging the scrubber
  shows "L14 · 32:05 · SAFETY CAR" before releasing, and clicking inside a
  flag/SC band jumps straight to the start of that incident.
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
- **Timing tower**: broadcast-style rows —
  position and driver code on the team color, positions gained/lost, tyre,
  speed, **LAST/BEST pills** (purple = session best,
  green = personal best), interval to the car ahead and gap to the leader
  (or "+nL" when lapped), plus the **mini-sector dashes** with the sector
  times below (official feed segments when live; computed against personal
  and session bests elsewhere). Each row also shows the pit-stop count
  ("P n" next to the interval) and an **AVG5/AVG10** column (average of the
  last 5/10 laps, excluding pit in/out laps). The header shows the leader's
  lap, a track-status badge and the weather. Narrow panels drop the outer
  blocks first — widen the splitter to see everything. Gaps only compute
  with real positions: at the
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
- **Tyres and strategy**: the "By lap" table tints every cell by compound,
  adds the tyre age in parentheses and marks pit-stop laps with "P" (live:
  `TimingAppData` stints and official pit-stop counter; replay: Fast-F1).
- **Flags and Safety Car**: background bands on the gap chart and the
  timeline, and the tower's header badge while yellow/SC/VSC/red is active.
  While a marshal sector is under yellow flag, that stretch of the **track
  map is painted yellow** (replay: race control messages + Fast-F1 marshal
  sectors).
- **Official mini-sectors** (Times/Gap → "Official µ" tab): the colored
  dashes of the official timing feed, per driver and in track order —
  purple = session best, green = personal best, yellow = completed without
  improving, blue = pit lane. Carried by the Live and Capture sources
  (existing capture files already contain them).
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
3. Open the windows you need from the hub's catalog (or apply a profile);
   each chart window carries its own channel / X-window selectors. In
   **Quali comparison**, pick the target driver and lap in the window's own
   toolbar and press **Set**.

## Windows executable

```powershell
.\build.ps1
```

Produces `dist\F1LiveTelemetry\` with **two executables** —
`F1LiveTelemetry.exe` (visualizer) and `capture\F1TelemCapture.exe`
(capturer, its own `_internal`) — plus `dist\F1LiveTelemetry-win64.zip`,
ready to upload as the GitHub release asset. No Python required.

If **Inno Setup 6** is installed (`winget install JRSoftware.InnoSetup`),
the build also produces `dist\F1LiveTelemetry-setup.exe`: a proper installer
that installs into `%LOCALAPPDATA%\Programs\F1LiveTelemetry` (no admin
required, so the auto-updater keeps working), creates Start-menu and
optional desktop shortcuts, registers the `f1telemetry://` protocol for the
F1TV sign-in extension and provides an uninstaller.

## Automatic updates

Both the visualizer and the capturer check the
[latest GitHub release](https://github.com/frborda/F1-Live-Telemetry/releases)
shortly after startup, and on demand via the **vX.Y.Z** button in the status
bar (bottom-right corner of each window). When a newer version exists, a
dialog shows its release notes and offers **Download and install**, with the
**main app and the capturer selectable separately**: the zip is downloaded to
`%LOCALAPPDATA%\f1telem\updates`, verified against the sha256 digest
published by GitHub, and an unattended script applies what you picked — the
main app is swapped once it closes and relaunched (automatic rollback if the
copy fails; see `update.log`), while the capturer — its own executable and
folder — is **never interrupted**: if it is recording, the installer waits in
the background (up to 60 min) and replaces it as soon as it is closed.
Updating only the capturer doesn't even close the main app.
**Skip this version** silences that release; the startup check can be turned
off from the same dialog (`updates.check_on_startup` in `config.json`).
Running from source only opens the releases page.

Publishing a release: bump `__version__` in `src/f1telem/__init__.py`, run
`.\build.ps1`, create a GitHub release tagged `vX.Y.Z` and upload
`dist\F1LiveTelemetry-win64.zip` (keep that asset name).

## Tests

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.venv\Scripts\python tests\smoke.py         # full app with the demo source + live decoder
.venv\Scripts\python tests\replay_check.py  # real Fast-F1 integration (downloads data)
.venv\Scripts\python tests\updater_check.py # updater: versions, zip layout, install script
.venv\Scripts\python tests\sector_bounds_check.py # official sectors: decode, bounds, anchoring
.venv\Scripts\python tests\auth_check.py    # F1TV sign-in: token parsing + PNA header
.venv\Scripts\python tests\import_check.py  # import player + transparent rewind
```

## Technical notes

- Live distance is integrated trapezoidally from speed; lap length is
  estimated as the median of observed laps (in replay it is computed exactly
  from Fast-F1 data).
- Lap/sector/microsector times are interpolated from the crossing instant of
  24 distance marks per lap. With ~4-5 Hz telemetry the accuracy is about
  ±0.1 s — good for comparisons, not official timing. When official sector
  times are available (replay: Fast-F1 laps; live/capture: the timing feed),
  the app locates the **real S1/S2 boundaries** on track — interpolating
  where each car was at the instant it set each sector time, median across
  laps and drivers — and anchors the marks to them; each microsector is 1/8
  of its sector. Each lap's marks are also scaled to the length it really
  integrated between finish-line crossings. On top of that, **as soon as
  each official sector/lap time is published, the tables show that exact
  value** — interpolation only covers what is not timed yet (the rolling
  current lap and microsectors). In replay all timed laps therefore match
  the official timing to the millisecond (verified against a full
  qualifying: 22/22 classification positions identical); live, official
  values arrive seconds after each crossing, and the official S1 is also
  used to re-anchor each lap's frame, cutting the error the feed latency
  introduces in the interpolated values. Without official sector times,
  sectors fall back to distance thirds of the lap. The gap between cars is the time difference
  when passing the same track position, with each lap anchored to its own
  finish-line crossing.
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

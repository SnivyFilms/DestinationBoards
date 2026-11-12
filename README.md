# Destination Boards

A passenger information display that shows upcoming trains for Amtrak and MTA station codes.

## Features
- Pulls data from `https://backend-unified.mylirr.org/locations` (API version 3.0)
- Adds Amtrak trains from `https://asm-backend.transitdocs.com/gtfs/amtrak`
- Resolves station codes to names using GTFS static feeds
- Refreshes every minute (configurable)
- Lists railroad, intended arrival, platform, destination, expected arrival, and status
- Scrolls the stop list for each train
- Shows time of day

## Requirements
- Python 3.10+
- Tkinter (usually bundled with Python)

## Quick Start

```bash
pip install -r requirements.txt
python destination_board.py
```

The desktop board automatically reads `destination-board.json` from the project
directory. Set `station_code` to the station code to display, `train_count` to
the number of rows, and `refresh_seconds` to the interval between automatic
feed fetches. If the file is absent, it retains the interactive prompts.

The legacy `run_board.py` and `run_penn_board.py` launchers still work, but they now open the same unified interactive board.

The older station-specific modules (`StamfordDestinationBoard.py`, `PennStationDestinationBoard.py`, and `station_names.py`) are kept only as compatibility shims.

## Raspberry Pi / RGB matrix mode

`pi_board.py` is the appliance-oriented runner. It has no Tkinter requirement,
reads `destination-board.json` (or `/boot/destination-board.json` on a Pi),
and exposes `GET /api/config` and `POST /api/config` on port 8080 so a local
web client can change the station and colors. Copy
`destination-board.example.json` to the SD card's boot partition and change
`station_code` to either operator's code; the existing NYK/NYP alias handling
will include both MTA and Amtrak at New York Penn.
Set `control_token` before exposing the control port beyond a trusted LAN;
clients then send `Authorization: Bearer <token>` for configuration changes.

Run a hardware-independent smoke test with:

```bash
python pi_board.py --config destination-board.example.json --once
```

To preview the color board as a desktop window, use Tkinter mode:

```bash
python pi_board.py --config destination-board.json --renderer preview
```

This opens a color window and automatically refreshes using `refresh_seconds`.
The column headings are shown above each train row, and
`show_stopping_pattern` controls whether the stopping pattern is shown on the
line immediately below each train.
MBTA stopping patterns are loaded from the trip's ordered stop list rather than
displaying only the route name. Patterns shorter than the preview width remain
static; longer patterns scroll horizontally.
The default renderer prints to the terminal; `--renderer matrix` is reserved
for an actual Raspberry Pi RGB panel.

The console renderer is intentionally the default. For a physical RGB matrix,
install Pillow and the panel vendor's driver (for example
`rpi-rgb-led-matrix`), then run with `--renderer matrix`. A 64x32 HUB75
panel is a practical starting point; a Raspberry Pi Zero 2 W is sufficient
for one panel, while a Pi 4/5 is preferable if the control page and several
feeds will run together. VRE and Tri-Rail trip-update URLs are configurable
because those agencies publish vehicle feeds and can change their endpoints.

For unattended startup, copy `destination-board.service` to
`/etc/systemd/system/`, install the project at `/opt/destination-board`, then
run `sudo systemctl enable --now destination-board`.

## Notes
- If the API has no real-time update for a stop, the display falls back to scheduled time.
- Platforms are shown from the first available field among `sign_track`, `avps_track_id`, and `t2s_track`.
- Station names are loaded from:
  - Metro-North: `https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip` (uses `stop_code`)
  - LIRR: `https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip` (uses `stop_code`)
  - Amtrak: `https://content.amtrak.com/content/gtfs/GTFS.zip` (uses `stop_id`)
- Station code aliases live in `destination_board.py` under `STATION_CODE_ALIASES` (e.g., `2SM` -> `STM` for Amtrak).
- Verified shared MTA/Amtrak station aliases are based on MTA Radar's station
  data (`https://radar.mta.info/status`) and Amtrak GTFS:
  `NYK/NYP`, `2BP/BRP`, `0HM/CRT`, `2NH/NHV`, `2SS/STS`,
  `2NR/NRO`, `0PO/POU`, `2SM/STM`, and `0YK/YNY`. New Haven Union
  Station and New Haven State Street remain separate stations.
- Shared station names are also matched from the current MTA and Amtrak GTFS
  station lists, so aliases remain useful even when an agency uses a different
  code. Cross-feed rows with the same train number and scheduled time are
  collapsed to one display entry.
- Amtrak route identifiers are rendered as service names, including
  `Northeast Regional`, `Acela`, `Empire Service`, `Keystone Service`,
  `Vermonter`, and `Lake Shore Limited`.
- Shared station labels are canonicalized for display. For example, MTA
  `Penn Station` and Amtrak `Ny Moynihan Train Hall At Penn Station` both
  display as `New York Penn`.

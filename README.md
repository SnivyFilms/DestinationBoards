# Destination Boards

A Tkinter passenger information display that shows upcoming trains for Amtrak and MTA station codes.

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

When the app starts, it prompts for:
- a station code
- how many lines you want displayed

The legacy `run_board.py` and `run_penn_board.py` launchers still work, but they now open the same unified interactive board.

The older station-specific modules (`StamfordDestinationBoard.py`, `PennStationDestinationBoard.py`, and `station_names.py`) are kept only as compatibility shims.

## Notes
- If the API has no real-time update for a stop, the display falls back to scheduled time.
- Platforms are shown from the first available field among `sign_track`, `avps_track_id`, and `t2s_track`.
- Station names are loaded from:
  - Metro-North: `https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip` (uses `stop_code`)
  - LIRR: `https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip` (uses `stop_code`)
  - Amtrak: `https://content.amtrak.com/content/gtfs/GTFS.zip` (uses `stop_id`)
- Station code aliases live in `destination_board.py` under `STATION_CODE_ALIASES` (e.g., `2SM` -> `STM` for Amtrak).

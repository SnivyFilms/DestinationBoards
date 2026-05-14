# Destination Boards

A Tkinter passenger information display that shows upcoming trains serving Stamford, CT and New York Penn Station.

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

## Quick Start (Stamford)

```bash
pip install -r requirements.txt
python run_board.py
```

## Quick Start (New York Penn Station)

```bash
pip install -r requirements.txt
python run_penn_board.py
```

## Options (Stamford)

```bash
python run_board.py --station 2SM --count 5 --refresh 60
```

- `--station`: station code to filter on (default: `2SM`)
- `--count`: number of trains to display
- `--refresh`: refresh interval in seconds

## Options (New York Penn Station)

```bash
python run_penn_board.py --station NYK --count 15 --refresh 60
```

## Notes
- If the API has no real-time update for a stop, the display falls back to scheduled time.
- Platforms are shown from the first available field among `sign_track`, `avps_track_id`, and `t2s_track`.
- Station names are loaded from:
  - Metro-North: `https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip` (uses `stop_code`)
  - LIRR: `https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip` (uses `stop_code`)
  - Amtrak: `https://content.amtrak.com/content/gtfs/GTFS.zip` (uses `stop_id`)
- Station code aliases live in `StamfordDestinationBoard.py` under `STATION_CODE_ALIASES` (e.g., `2SM` -> `STM` for Amtrak).

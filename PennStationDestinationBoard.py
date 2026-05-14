from __future__ import annotations

import argparse

from StamfordDestinationBoard import (
    BoardApp,
    DEFAULT_REFRESH_SECONDS,
    DEFAULT_TRAIN_COUNT,
)

LIRR_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip"
DEFAULT_STATION_CODE = "NYK"
TITLE_TEXT = "NEW YORK PENN STATION"


def main() -> None:
    import tkinter as tk

    parser = argparse.ArgumentParser(description="New York Penn Station destination board")
    parser.add_argument("--station", default=DEFAULT_STATION_CODE, help="Station code (default: NYK)")
    parser.add_argument("--count", type=int, default=DEFAULT_TRAIN_COUNT, help="Number of trains to display")
    parser.add_argument("--refresh", type=int, default=DEFAULT_REFRESH_SECONDS, help="Refresh interval in seconds")
    args = parser.parse_args()

    root = tk.Tk()
    BoardApp(
        root,
        args.station,
        args.count,
        args.refresh,
        title_text=TITLE_TEXT,
        mta_gtfs_url=LIRR_GTFS_URL,
        mta_source_label="LIRR",
    )
    root.mainloop()


if __name__ == "__main__":
    main()



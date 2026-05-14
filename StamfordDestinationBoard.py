from __future__ import annotations

import argparse
import datetime as dt
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING

import requests
from google.transit import gtfs_realtime_pb2

from station_names import StationNameResolver

if TYPE_CHECKING:
    import tkinter as tk

API_URL = "https://backend-unified.mylirr.org/locations"
API_HEADERS = {"Accept-Version": "3.0"}
AMTRAK_RT_URL = "https://asm-backend.transitdocs.com/gtfs/amtrak"
MNR_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip"
AMTRAK_GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"

DEFAULT_STATION_CODE = "2SM"
DEFAULT_TRAIN_COUNT = 15
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_SCROLL_MS = 180

STATION_CODE_ALIASES = {
    "2SM": ["STM"],
    "NYK": ["NYP"],
    "NYP": ["NYK"],
}

HEADER_TEXT = "RAILROAD  INTENDED  PLATFORM  DESTINATION             EXPECTED   STATUS"


@dataclass
class DisplayTrain:
    train_num: str
    destination: str
    platform: str
    scheduled_ts: Optional[int]
    expected_ts: Optional[int]
    status_text: str
    stops_text: str
    source: str


def _format_time(ts: Optional[int]) -> str:
    if not ts:
        return "--:--"
    text = dt.datetime.fromtimestamp(ts).strftime("%I:%M %p")
    return text.lstrip("0")


def _compute_status(scheduled_ts: Optional[int], expected_ts: Optional[int]) -> str:
    if not scheduled_ts or not expected_ts:
        return "SCHEDULED"
    delta = expected_ts - scheduled_ts
    minutes = int(round(delta / 60.0))
    if abs(minutes) <= 1:
        return "ON TIME"
    if minutes > 1:
        return f"LATE +{minutes}m"
    return f"EARLY {abs(minutes)}m"


def _pick_platform(stop: Dict[str, Any]) -> str:
    for key in ("sign_track", "avps_track_id", "t2s_track"):
        value = stop.get(key)
        if value:
            return str(value)
    return "TBD"


def _expected_timestamp(stop: Dict[str, Any], status: Dict[str, Any]) -> Optional[int]:
    sched = stop.get("sched_time")
    actual = stop.get("act_time") or stop.get("act_arrive_time") or stop.get("act_depart_time")
    if actual:
        return int(actual)
    otp = status.get("otp")
    if sched and otp is not None:
        return int(sched) + int(otp)
    if sched:
        return int(sched)
    return None


def _fetch_trains() -> List[Dict[str, Any]]:
    response = requests.get(API_URL, headers=API_HEADERS, timeout=12)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        return data.get("data", []) or data.get("trains", []) or []
    return data


def _station_code_candidates(station_code: str) -> List[str]:
    code = station_code.upper().strip()
    aliases = STATION_CODE_ALIASES.get(code, [])
    return [code, *aliases]


def _fetch_amtrak_feed() -> gtfs_realtime_pb2.FeedMessage:
    response = requests.get(AMTRAK_RT_URL, timeout=12)
    response.raise_for_status()
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(response.content)
    return feed


def _parse_amtrak_train_number(trip_id: str) -> str:
    if not trip_id:
        return ""
    if "_" in trip_id:
        return trip_id.split("_")[-1]
    return trip_id


def _train_display_entries(
    trains: Iterable[Dict[str, Any]],
    station_code: str,
    now_ts: int,
    name_resolver: StationNameResolver,
    source_label: str = "MNR",
) -> List[DisplayTrain]:
    results: List[DisplayTrain] = []
    station_codes = set(_station_code_candidates(station_code))

    for train in trains:
        details = train.get("details", {})
        stops = details.get("stops", []) or []
        stop_match = next((s for s in stops if s.get("code") in station_codes), None)
        if not stop_match:
            continue

        destination = details.get("headsign") or details.get("summary") or train.get("train_num") or "Unknown"
        scheduled_ts = stop_match.get("sched_time")
        status = train.get("status", {}) or {}
        expected_ts = _expected_timestamp(stop_match, status)
        platform = _pick_platform(stop_match)

        status_text = _compute_status(scheduled_ts, expected_ts)
        stop_codes = [s.get("code") for s in stops if s.get("code")]
        stop_names = [name_resolver.resolve_mta(code) for code in stop_codes]
        stops_text = "Stops: " + " - ".join(stop_names) if stop_names else "Stops: (no data)"

        if expected_ts and expected_ts < now_ts - 300:
            continue

        results.append(
            DisplayTrain(
                train_num=str(train.get("train_num", "")),
                destination=str(destination),
                platform=platform,
                scheduled_ts=int(scheduled_ts) if scheduled_ts else None,
                expected_ts=int(expected_ts) if expected_ts else None,
                status_text=status_text,
                stops_text=stops_text,
                source=source_label,
            )
        )

    results.sort(key=lambda item: item.expected_ts or item.scheduled_ts or 0)
    return results


def _train_display_entries_amtrak(
    feed: gtfs_realtime_pb2.FeedMessage,
    station_code: str,
    now_ts: int,
    name_resolver: StationNameResolver,
) -> List[DisplayTrain]:
    results: List[DisplayTrain] = []
    station_codes = set(_station_code_candidates(station_code))

    for entity in feed.entity:
        if not entity.HasField("trip_update"):
            continue
        trip_update = entity.trip_update
        if not trip_update.stop_time_update:
            continue

        stop_updates = [u for u in trip_update.stop_time_update if u.stop_id]
        stop_ids = [u.stop_id.upper() for u in stop_updates]
        if not any(code in station_codes for code in stop_ids):
            continue

        match_update = next((u for u in stop_updates if u.stop_id.upper() in station_codes), None)
        if not match_update:
            continue

        arrival = match_update.arrival
        departure = match_update.departure
        expected_ts = arrival.time or departure.time or 0
        scheduled_ts = None
        if arrival.delay and expected_ts:
            scheduled_ts = expected_ts - arrival.delay
        elif departure.delay and expected_ts:
            scheduled_ts = expected_ts - departure.delay

        stop_names = [name_resolver.resolve_amtrak(stop_id) for stop_id in stop_ids]
        stops_text = "Stops: " + " - ".join(stop_names) if stop_names else "Stops: (no data)"

        destination = "Unknown"
        if stop_ids:
            destination = name_resolver.resolve_amtrak(stop_ids[-1])

        train_num = _parse_amtrak_train_number(trip_update.trip.trip_id)
        status_text = _compute_status(scheduled_ts, expected_ts or None)

        if expected_ts and expected_ts < now_ts - 300:
            continue

        results.append(
            DisplayTrain(
                train_num=train_num,
                destination=destination,
                platform="TBD",
                scheduled_ts=int(scheduled_ts) if scheduled_ts else None,
                expected_ts=int(expected_ts) if expected_ts else None,
                status_text=status_text,
                stops_text=stops_text,
                source="AMTRAK",
            )
        )

    results.sort(key=lambda item: item.expected_ts or item.scheduled_ts or 0)
    return results


class StopScroller:
    def __init__(self, label: Any, width: int) -> None:
        self.label = label
        self.width = width
        self.text = ""
        self.index = 0

    def set_text(self, text: str) -> None:
        self.text = text or ""
        self.index = 0
        self._render()

    def tick(self) -> None:
        if len(self.text) <= self.width:
            return
        self.index = (self.index + 1) % len(self.text)
        self._render()

    def set_width(self, width: int) -> None:
        self.width = max(10, width)
        self._render()

    def _render(self) -> None:
        if len(self.text) <= self.width:
            self.label.config(text=self.text)
            return
        padded = self.text + "   "
        start = self.index
        end = start + self.width
        if end <= len(padded):
            view = padded[start:end]
        else:
            view = padded[start:] + padded[: end - len(padded)]
        self.label.config(text=view)


class BoardApp:
    def __init__(
        self,
        root: Any,
        station_code: str,
        train_count: int,
        refresh_seconds: int,
        title_text: str,
        mta_gtfs_url: str = MNR_GTFS_URL,
        mta_source_label: str = "MNR",
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.root = root
        self.station_code = station_code
        self.train_count = train_count
        self.refresh_seconds = refresh_seconds
        self.last_updated = 0
        self.last_error = ""
        self.name_resolver = StationNameResolver(mta_gtfs_url, AMTRAK_GTFS_URL)
        self.last_entries: List[DisplayTrain] = []
        self.mta_source_label = mta_source_label
        self.base_height = 720
        self.base_fonts = {
            "title": 26,
            "header": 16,
            "main": 16,
            "sub": 12,
            "clock": 18,
            "status": 12,
        }
        self.font_title = tkfont.Font(family="Courier", size=self.base_fonts["title"], weight="bold")
        self.font_header = tkfont.Font(family="Courier", size=self.base_fonts["header"], weight="bold")
        self.font_main = tkfont.Font(family="Courier", size=self.base_fonts["main"])
        self.font_sub = tkfont.Font(family="Courier", size=self.base_fonts["sub"])
        self.font_clock = tkfont.Font(family="Courier", size=self.base_fonts["clock"], weight="bold")
        self.font_status = tkfont.Font(family="Courier", size=self.base_fonts["status"])

        self.root.title(f"{title_text} Destination Board")
        self.root.configure(bg="black")
        self.root.geometry("1280x720")

        self.title_label = tk.Label(
            root,
            text=title_text,
            font=self.font_title,
            fg="#ffb000",
            bg="black",
        )
        self.title_label.pack(pady=(16, 6))

        self.header_label = tk.Label(
            root,
            text=HEADER_TEXT,
            font=self.font_header,
            fg="#ffb000",
            bg="black",
        )
        self.header_label.pack(pady=(0, 8))

        self.rows_frame = tk.Frame(root, bg="black")
        self.rows_frame.pack(fill=tk.BOTH, expand=True)

        self.row_labels: List[Any] = []
        self.stop_scrollers: List[StopScroller] = []
        self.stop_width = 86

        for _ in range(self.train_count):
            line_label = tk.Label(
                self.rows_frame,
                text="--      --:--   TBD      Loading...             --:--    SCHEDULED",
                font=self.font_main,
                fg="#ffb000",
                bg="black",
                anchor="w",
                justify="left",
            )
            line_label.pack(fill=tk.X, padx=24)
            stops_label = tk.Label(
                self.rows_frame,
                text="Stops: (loading)",
                font=self.font_sub,
                fg="#ffb000",
                bg="black",
                anchor="w",
                justify="left",
            )
            stops_label.pack(fill=tk.X, padx=36, pady=(0, 6))
            self.row_labels.append(line_label)
            self.stop_scrollers.append(StopScroller(stops_label, self.stop_width))

        self.clock_label = tk.Label(
            root,
            text="",
            font=self.font_clock,
            fg="#ffb000",
            bg="black",
        )
        self.clock_label.pack(pady=(8, 12))

        self.status_label = tk.Label(
            root,
            text="",
            font=self.font_status,
            fg="#ffb000",
            bg="black",
        )
        self.status_label.pack(pady=(0, 8))

        self._schedule_updates()
        self._on_resize()
        self.root.bind("<Configure>", self._on_resize)

    def _compute_column_widths(self) -> Dict[str, int]:
        width_px = max(self.root.winfo_width(), 800)
        char_px = max(self.font_main.measure("0"), 8)
        total_chars = max(60, int(width_px / char_px))
        spacing = 2 * 5
        fixed = 8 + 8 + 8 + 8 + 12
        remaining = max(10, total_chars - fixed - spacing)
        return {
            "railroad": 8,
            "intended": 8,
            "platform": 8,
            "destination": remaining,
            "expected": 8,
            "status": 12,
        }

    def _compute_stop_width(self) -> int:
        width_px = max(self.root.winfo_width(), 800)
        char_px = max(self.font_sub.measure("0"), 8)
        total_chars = max(50, int(width_px / char_px))
        return max(20, total_chars - 4)

    def _update_fonts(self) -> None:
        height_px = max(self.root.winfo_height(), self.base_height)
        scale = max(0.8, min(1.6, height_px / self.base_height))
        self.font_title.configure(size=int(self.base_fonts["title"] * scale))
        self.font_header.configure(size=int(self.base_fonts["header"] * scale))
        self.font_main.configure(size=int(self.base_fonts["main"] * scale))
        self.font_sub.configure(size=int(self.base_fonts["sub"] * scale))
        self.font_clock.configure(size=int(self.base_fonts["clock"] * scale))
        self.font_status.configure(size=int(self.base_fonts["status"] * scale))

    def _format_line(self, railroad: str, sched: str, platform: str, dest: str, expected: str, status: str) -> str:
        widths = self._compute_column_widths()
        return (
            f"{railroad:<{widths['railroad']}}  "
            f"{sched:>{widths['intended']}}  "
            f"{platform:^{widths['platform']}}  "
            f"{dest:<{widths['destination']}}  "
            f"{expected:>{widths['expected']}}  "
            f"{status:<{widths['status']}}"
        )

    def _format_header(self) -> str:
        return self._format_line("RAILROAD", "INTENDED", "PLATFORM", "DESTINATION", "EXPECTED", "STATUS")

    def _on_resize(self, *_: Any) -> None:
        self._update_fonts()
        self.header_label.config(text=self._format_header())
        self.stop_width = self._compute_stop_width()
        for scroller in self.stop_scrollers:
            scroller.set_width(self.stop_width)
        if self.last_entries:
            self._render_entries(self.last_entries)

    def _schedule_updates(self) -> None:
        self.root.after(200, self._tick_clock)
        self.root.after(DEFAULT_SCROLL_MS, self._tick_scrollers)
        self.root.after(500, self._refresh_data)

    def _tick_clock(self) -> None:
        now = dt.datetime.now().strftime("%I:%M:%S %p").lstrip("0")
        self.clock_label.config(text=f"TIME: {now}")
        self.root.after(200, self._tick_clock)

    def _tick_scrollers(self) -> None:
        for scroller in self.stop_scrollers:
            scroller.tick()
        self.root.after(DEFAULT_SCROLL_MS, self._tick_scrollers)

    def _refresh_data(self) -> None:
        now_ts = int(time.time())
        try:
            self.name_resolver.ensure_loaded()
            trains = _fetch_trains()
            amtrak_feed = _fetch_amtrak_feed()
            entries = _train_display_entries(
                trains,
                self.station_code,
                now_ts,
                self.name_resolver,
                source_label=self.mta_source_label,
            )
            entries.extend(
                _train_display_entries_amtrak(amtrak_feed, self.station_code, now_ts, self.name_resolver)
            )
            entries.sort(key=lambda item: item.expected_ts or item.scheduled_ts or 0)
            self._render_entries(entries)
            self.last_error = ""
            self.last_updated = now_ts
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
        self._update_status()
        self.root.after(self.refresh_seconds * 1000, self._refresh_data)

    def _render_entries(self, entries: List[DisplayTrain]) -> None:
        self.last_entries = entries
        for idx, label in enumerate(self.row_labels):
            if idx < len(entries):
                entry = entries[idx]
                sched = _format_time(entry.scheduled_ts)
                expected = _format_time(entry.expected_ts)
                dest = (entry.destination or "").strip()
                line = self._format_line(
                    entry.source,
                    sched,
                    entry.platform,
                    dest,
                    expected,
                    entry.status_text,
                )
                label.config(text=line)
                self.stop_scrollers[idx].set_text(entry.stops_text)
            else:
                label.config(
                    text=self._format_line("--", "--:--", "TBD", "(no trains)", "--:--", "SCHEDULED")
                )
                self.stop_scrollers[idx].set_text("Stops: (none)")

    def _update_status(self) -> None:
        if self.last_error:
            self.status_label.config(text=f"Last update failed: {self.last_error}")
        else:
            updated = _format_time(self.last_updated)
            self.status_label.config(text=f"Last updated: {updated}")


def main() -> None:
    import tkinter as tk

    parser = argparse.ArgumentParser(description="Stamford destination board")
    parser.add_argument("--station", default=DEFAULT_STATION_CODE, help="Station code (default: 2SM)")
    parser.add_argument("--count", type=int, default=DEFAULT_TRAIN_COUNT, help="Number of trains to display")
    parser.add_argument("--refresh", type=int, default=DEFAULT_REFRESH_SECONDS, help="Refresh interval in seconds")
    args = parser.parse_args()

    root = tk.Tk()
    app = BoardApp(root, args.station, args.count, args.refresh, title_text="STAMFORD, CT")
    root.mainloop()


if __name__ == "__main__":
    main()


"""Raspberry Pi station board service.

The service deliberately keeps feed collection separate from rendering.  It can
run without an LED panel for development and accepts configuration from an SD
card or a small HTTP endpoint on the local network.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable, List, Optional, Dict
from urllib.parse import urlparse

import requests

from destination_board import (
    DisplayTrain,
    STATION_DISPLAY_NAMES,
    StationNameResolver,
    _compute_status,
    _deduplicate_display_entries,
    _fetch_amtrak_feed,
    _format_time,
    _train_display_entries,
    _train_display_entries_amtrak,
)


DEFAULT_CONFIG_PATH = Path("/boot/destination-board.json")
LOCAL_CONFIG_PATH = Path("destination-board.json")
DEFAULT_COLORS = {
    "background": "#000000",
    "text": "#ffb000",
    "on_time": "#00ff66",
    "late": "#ff3333",
    "early": "#33aaff",
    "muted": "#777777",
}
STATION_TITLES = {
    "NYK": "New York Penn",
    "NYP": "New York Penn",
    "PVD": "Providence",
    "BOS": "Boston South Station",
    "BON": "Boston South Station",
    "2NH": "New Haven",
    "NHV": "New Haven",
    "2SS": "New Haven State Street",
    "STS": "New Haven State Street",
    "2SM": "Stamford",
    "STM": "Stamford",
    "2NR": "New Rochelle",
    "NRO": "New Rochelle",
    "2BP": "Bridgeport",
    "BRP": "Bridgeport",
    "0PO": "Poughkeepsie",
    "POU": "Poughkeepsie",
    "0YK": "Yonkers",
    "YNY": "Yonkers",
}
MBTA_STOP_IDS = {
    "BOS": "NEC-2287",
    "BON": "NEC-2287",
    "PVD": "NEC-1851-03",
}
BOARD_WIDTHS = {
    "railroad": 8,
    "service": 24,
    "scheduled": 9,
    "platform": 8,
    "destination": 28,
    "expected": 9,
    "status": 12,
}


def _board_header() -> str:
    return (
        f"{'RAILROAD':<{BOARD_WIDTHS['railroad']}} "
        f"{'SERVICE TYPE':<{BOARD_WIDTHS['service']}} "
        f"{'SCHEDULED':>{BOARD_WIDTHS['scheduled']}} "
        f"{'PLATFORM':^{BOARD_WIDTHS['platform']}} "
        f"{'FINAL DESTINATION':<{BOARD_WIDTHS['destination']}} "
        f"{'EXPECTED':>{BOARD_WIDTHS['expected']}} "
        f"{'STATUS':<{BOARD_WIDTHS['status']}}"
    )


@dataclass
class BoardConfig:
    station_code: str = "NYK"
    train_count: int = 8
    refresh_seconds: int = 60
    matrix_width: int = 64
    matrix_height: int = 32
    brightness: int = 70
    colors: Dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLORS))
    control_host: str = "0.0.0.0"
    control_port: int = 8080
    control_token: str = ""
    providers: List[str] = field(default_factory=lambda: ["mta", "amtrak", "mbta"])
    mbta_api_base: str = "https://api-v3.mbta.com"
    vre_trip_updates_url: str = ""
    trirail_trip_updates_url: str = ""
    show_stopping_pattern: bool = True

    @classmethod
    def load(cls, path: Path) -> "BoardConfig":
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as handle:
            values = json.load(handle)
        if not isinstance(values, dict):
            raise ValueError(f"{path} must contain a JSON object")
        known = {key: value for key, value in values.items() if key in cls.__dataclass_fields__}
        config = cls(**known)
        config.colors = {**DEFAULT_COLORS, **(values.get("colors") or {})}
        config.station_code = config.station_code.strip().upper()
        if not config.station_code:
            raise ValueError("station_code cannot be empty")
        if config.train_count < 1 or config.refresh_seconds < 5:
            raise ValueError("train_count must be positive and refresh_seconds must be at least 5")
        return config

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")


def find_config(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit)
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH
    return LOCAL_CONFIG_PATH


def _station_title(code: str, resolver: StationNameResolver) -> str:
    normalized = (code or "").strip().upper()
    if normalized in STATION_TITLES:
        return STATION_TITLES[normalized]
    if normalized in STATION_DISPLAY_NAMES:
        return STATION_DISPLAY_NAMES[normalized]
    return resolver.canonical_name(
        resolver.resolve_amtrak(normalized) or resolver.resolve_mta(normalized) or normalized
    )


def _normalize_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _canonical_mbta_name(value: str) -> str:
    normalized = _normalize_name(value)
    if normalized in {"southstation", "southstationcommuterrail"}:
        return "Boston South Station"
    return value


def _format_entry_line(entry: DisplayTrain) -> str:
    intended = entry.scheduled_ts or entry.expected_ts
    service = entry.service_name or entry.source
    if entry.train_num:
        service = f"{service} ({entry.train_num})"
    return (
        f"{entry.source[:8]:<{BOARD_WIDTHS['railroad']}} "
        f"{service[:24]:<{BOARD_WIDTHS['service']}} "
        f"{_format_time(intended):>{BOARD_WIDTHS['scheduled']}} "
        f"{entry.platform[:8]:^{BOARD_WIDTHS['platform']}} "
        f"{entry.destination[:28]:<{BOARD_WIDTHS['destination']}} "
        f"{_format_time(entry.expected_ts):>{BOARD_WIDTHS['expected']}} "
        f"{entry.status_text[:12]:<{BOARD_WIDTHS['status']}}"
    )


def _feed_entries(feed: Any, station_code: str, now_ts: int, resolver: StationNameResolver) -> List[DisplayTrain]:
    """Convert a standard GTFS-Realtime TripUpdate feed into board rows."""
    entries: List[DisplayTrain] = []
    station_codes = {code.upper() for code in resolver.related_station_codes(station_code)}
    for entity in getattr(feed, "entity", []):
        if not entity.HasField("trip_update"):
            continue
        trip = entity.trip_update
        updates = [item for item in trip.stop_time_update if item.stop_id]
        match = next((item for item in updates if item.stop_id.upper() in station_codes), None)
        if match is None:
            continue
        expected = 0
        scheduled = 0
        if match.HasField("arrival"):
            expected = match.arrival.time
            scheduled = expected - match.arrival.delay if match.arrival.delay else expected
        if not expected and match.HasField("departure"):
            expected = match.departure.time
            scheduled = expected - match.departure.delay if match.departure.delay else expected
        if expected and expected < now_ts - 300:
            continue
        destination = trip.trip.trip_id or "Unknown"
        entries.append(
            DisplayTrain(
                train_num=trip.trip.trip_id or entity.id,
                destination=destination,
                platform="TBD",
                scheduled_ts=scheduled or None,
                expected_ts=expected or None,
                status_text=_compute_status(scheduled or None, expected or None),
                stops_text="Stops: " + " - ".join(item.stop_id for item in updates),
                source="GTFS-RT",
            )
        )
    return sorted(entries, key=lambda row: row.expected_ts or row.scheduled_ts or 0)


class FeedService:
    def __init__(self, config: BoardConfig) -> None:
        self.config = config
        self.resolver = StationNameResolver()

    def fetch(self) -> List[DisplayTrain]:
        now_ts = int(time.time())
        self.resolver.ensure_loaded()
        entries: List[DisplayTrain] = []
        if "mta" in self.config.providers:
            response = requests.get(
                "https://backend-unified.mylirr.org/locations",
                headers={"Accept-Version": "3.0"},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            trains = payload.get("data", []) if isinstance(payload, dict) else payload
            entries.extend(
                _train_display_entries(
                    trains or [], self.config.station_code, now_ts, self.resolver,
                    source_label=self.resolver.mta_family_for_code(self.config.station_code),
                )
            )
        if "amtrak" in self.config.providers:
            feed = _fetch_amtrak_feed()
            if feed is not None:
                entries.extend(_train_display_entries_amtrak(feed, self.config.station_code, now_ts, self.resolver))
        if "mbta" in self.config.providers:
            entries.extend(self._fetch_mbta())
        for provider, url in (
            ("vre", self.config.vre_trip_updates_url),
            ("trirail", self.config.trirail_trip_updates_url),
        ):
            if provider not in self.config.providers or not url:
                continue
            response = requests.get(url, timeout=12)
            response.raise_for_status()
            try:
                from google.transit import gtfs_realtime_pb2
            except ModuleNotFoundError as exc:
                raise RuntimeError("gtfs-realtime-bindings is required for VRE/Tri-Rail") from exc
            feed = gtfs_realtime_pb2.FeedMessage()
            feed.ParseFromString(response.content)
            entries.extend(_feed_entries(feed, self.config.station_code, now_ts, self.resolver))
        return _deduplicate_display_entries(entries)[: self.config.train_count]

    def _fetch_mbta(self) -> List[DisplayTrain]:
        stop_id = MBTA_STOP_IDS.get(self.config.station_code)
        if not stop_id:
            stop_id = self._find_mbta_stop()
        if not stop_id:
            return []
        params = {
            "filter[stop]": stop_id,
            "include": "trip,route",
            "sort": "departure_time",
            "page[limit]": str(self.config.train_count),
        }
        response = requests.get(f"{self.config.mbta_api_base}/predictions", params=params, timeout=12)
        response.raise_for_status()
        payload = response.json()
        included = {item.get("id"): item for item in payload.get("included", [])}
        trip_ids = [
            ((item.get("relationships") or {}).get("trip") or {}).get("data", {}).get("id")
            for item in payload.get("data", [])
        ]
        stop_patterns = self._fetch_mbta_stop_patterns([trip_id for trip_id in trip_ids if trip_id])
        entries: List[DisplayTrain] = []
        for item in payload.get("data", []):
            attributes = item.get("attributes") or {}
            relationships = item.get("relationships") or {}
            trip_id = ((relationships.get("trip") or {}).get("data") or {}).get("id", item.get("id", "?"))
            trip = included.get(trip_id, {})
            trip_attrs = trip.get("attributes") or {}
            route_id = ((relationships.get("route") or {}).get("data") or {}).get("id", "")
            route = included.get(route_id, {})
            route_attrs = route.get("attributes") or {}
            stop_id = ((relationships.get("stop") or {}).get("data") or {}).get("id", "")
            stop = included.get(stop_id, {})
            stop_attrs = stop.get("attributes") or {}
            service_name = str(
                route_attrs.get("long_name")
                or route_attrs.get("short_name")
                or route_id
                or "MBTA"
            )
            expected_text = attributes.get("departure_time") or attributes.get("arrival_time")
            expected_ts = _timestamp(expected_text)
            status = str(attributes.get("status") or "").upper().replace("_", " ")
            if not status:
                status = _compute_status(expected_ts, expected_ts)
            entries.append(DisplayTrain(
                train_num=str(trip_attrs.get("name") or trip_id),
                destination=_canonical_mbta_name(
                    str(trip_attrs.get("headsign") or route_attrs.get("long_name") or "Unknown")
                ),
                platform=str(
                    attributes.get("platform")
                    or stop_attrs.get("platform_code")
                    or stop_attrs.get("platform_name")
                    or "TBD"
                ),
                scheduled_ts=expected_ts,
                expected_ts=expected_ts,
                status_text=status,
                stops_text="Stops: " + " - ".join(stop_patterns.get(trip_id, [])),
                source="MBTA",
                service_name=service_name,
            ))
        return entries

    def _fetch_mbta_stop_patterns(self, trip_ids: List[str]) -> Dict[str, List[str]]:
        if not trip_ids:
            return {}
        response = requests.get(
            f"{self.config.mbta_api_base}/trips",
            params={
                "filter[id]": ",".join(sorted(set(trip_ids))),
                "include": "stops",
                "page[limit]": str(len(set(trip_ids))),
            },
            timeout=12,
        )
        response.raise_for_status()
        payload = response.json()
        stops = {
            item.get("id"): _canonical_mbta_name((item.get("attributes") or {}).get("name", item.get("id", "")))
            for item in payload.get("included", [])
            if item.get("type") == "stop"
        }
        patterns: Dict[str, List[str]] = {}
        for trip in payload.get("data", []):
            ids = [
                stop.get("id")
                for stop in ((trip.get("relationships") or {}).get("stops") or {}).get("data", [])
            ]
            patterns[trip.get("id", "")] = [stops[stop_id] for stop_id in ids if stop_id in stops]
        return patterns

    def _find_mbta_stop(self) -> str:
        response = requests.get(
            f"{self.config.mbta_api_base}/stops",
            params={"filter[route_type]": "2", "page[limit]": "1000"},
            timeout=12,
        )
        response.raise_for_status()
        target = _normalize_name(_station_title(self.config.station_code, self.resolver))
        for stop in response.json().get("data", []):
            name = _normalize_name((stop.get("attributes") or {}).get("name", ""))
            if name and (name == target or name in target or target in name):
                return str(stop.get("id", ""))
        return ""


def _timestamp(value: Any) -> Optional[int]:
    if not value:
        return None
    try:
        from datetime import datetime
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError):
        return None


class ConsoleRenderer:
    def render(self, config: BoardConfig, entries: Iterable[DisplayTrain], error: str = "") -> None:
        print("\033[2J\033[H", end="")
        title = _station_title(config.station_code, FeedService(config).resolver)
        print(f"{title}   {time.strftime('%I:%M:%S %p').lstrip('0')}")
        print("-" * 120)
        print(_board_header())
        for entry in entries:
            intended = entry.scheduled_ts or entry.expected_ts
            service = entry.service_name or entry.source
            if entry.train_num:
                service = f"{service} ({entry.train_num})"
            print(f"{entry.source:<8} {service[:24]:<24} {_format_time(intended):>9} "
                  f"{entry.platform[:8]:^8} {entry.destination[:28]:<28} {_format_time(entry.expected_ts):>9} "
                  f"{entry.status_text:<12}")
            if config.show_stopping_pattern:
                print(f"           {entry.stops_text[:110]}")
        if error:
            print(f"\nFeed error: {error}")
        else:
            print(f"\nLast updated at {time.strftime('%I:%M %p').lstrip('0')}")


class PreviewRenderer:
    """Tkinter color preview for testing the board on a normal computer."""

    def __init__(self, config: BoardConfig, service: FeedService) -> None:
        import tkinter as tk

        self.tk = tk
        self.config = config
        self.service = service
        self.root = tk.Tk()
        self.root.title(f"{config.station_code} Destination Board Preview")
        self.root.configure(bg=config.colors["background"])
        self.root.geometry("1280x720")
        self.title = tk.Label(
            self.root,
            text=_station_title(config.station_code, service.resolver),
            font=("Courier", 24, "bold"),
            fg=config.colors["text"],
            bg=config.colors["background"],
        )
        self.title.pack(pady=(15, 8))
        self.clock = tk.Label(
            self.root,
            text="",
            font=("Courier", 16, "bold"),
            fg=config.colors["text"],
            bg=config.colors["background"],
        )
        self.clock.pack()
        self.board_font = ("Courier New", 13)
        self.header = tk.Label(
            self.root,
            text=_board_header(),
            font=("Courier New", 13, "bold"),
            fg=config.colors["text"],
            bg=config.colors["background"],
            anchor="w",
        )
        self.header.pack(fill="x", padx=20)
        self.rows = tk.Frame(self.root, bg=config.colors["background"])
        self.rows.pack(fill="both", expand=True, padx=20, pady=10)
        self.labels = []
        self.stop_labels = []
        self.stop_texts = ["" for _ in range(config.train_count)]
        self.stop_offsets = [0 for _ in range(config.train_count)]
        for _ in range(config.train_count):
            label = tk.Label(self.rows, text="Loading...", anchor="w", justify="left",
                             font=self.board_font, fg=config.colors["text"],
                             bg=config.colors["background"])
            label.pack(fill="x", pady=(2, 0))
            self.labels.append(label)
            stop_label = tk.Label(self.rows, text="", anchor="w", justify="left",
                                  font=("Courier", 10), fg=config.colors["muted"],
                                  bg=config.colors["background"])
            if config.show_stopping_pattern:
                stop_label.pack(fill="x", pady=(0, 3))
            self.stop_labels.append(stop_label)
        self._tick_stops()
        self.status = tk.Label(
            self.root,
            text="Fetching live data...",
            font=("Courier", 11),
            fg=config.colors["muted"],
            bg=config.colors["background"],
        )
        self.status.pack(pady=(0, 10))

    def start(self) -> None:
        self._tick_clock()
        self._refresh()
        self.root.mainloop()

    def _tick_clock(self) -> None:
        self.clock.config(text=time.strftime("%I:%M:%S %p").lstrip("0"))
        self.root.after(500, self._tick_clock)

    def _refresh(self) -> None:
        try:
            entries = self.service.fetch()
            self._render(entries)
            self.status.config(
                text=f"Last updated at {time.strftime('%I:%M %p').lstrip('0')}",
                fg=self.config.colors["muted"],
            )
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            self.status.config(text=f"Feed error: {exc}", fg=self.config.colors["late"])
        self.root.after(self.config.refresh_seconds * 1000, self._refresh)

    def _render(self, entries: Iterable[DisplayTrain]) -> None:
        entries = list(entries)
        for label, entry in zip(self.labels, entries):
            intended = entry.scheduled_ts or entry.expected_ts
            service = entry.service_name or entry.source
            if entry.train_num:
                service = f"{service} ({entry.train_num})"
            line = _format_entry_line(entry)
            color = self.config.colors["late"] if "LATE" in entry.status_text else (
                self.config.colors["early"] if "EARLY" in entry.status_text
                else self.config.colors["on_time"]
            )
            label.config(text=line, fg=color)
        for index, entry in enumerate(entries[:len(self.stop_labels)]):
            self.stop_texts[index] = entry.stops_text.removeprefix("Stops: ")
            self.stop_labels[index].config(
                text=(self.stop_texts[index][:145] if self.config.show_stopping_pattern else "")
            )
        for label in self.labels[len(entries):]:
            label.config(text="")
        for label in self.stop_labels[len(entries):]:
            label.config(text="")
        for index in range(len(entries), len(self.stop_texts)):
            self.stop_texts[index] = ""
            self.stop_offsets[index] = 0

    def _tick_stops(self) -> None:
        width = 145
        for index, label in enumerate(self.stop_labels):
            text = self.stop_texts[index]
            if not self.config.show_stopping_pattern or not text:
                label.config(text="")
                continue
            if len(text) <= width:
                label.config(text=text)
                continue
            padded = text + "     "
            start = self.stop_offsets[index] % len(padded)
            visible = (padded + padded)[start:start + width]
            label.config(text=visible)
            self.stop_offsets[index] = (self.stop_offsets[index] + 1) % len(padded)
        self.root.after(180, self._tick_stops)


class RgbMatrixRenderer:
    """Optional HUB75 renderer for rpi-rgb-led-matrix panels."""

    def __init__(self, config: BoardConfig) -> None:
        try:
            from PIL import Image, ImageDraw, ImageFont
            from rgbmatrix import RGBMatrix, RGBMatrixOptions
        except ImportError as exc:
            raise RuntimeError(
                "matrix rendering requires Pillow and rpi-rgb-led-matrix"
            ) from exc
        self._image_type = Image
        self._draw_type = ImageDraw
        self._font_type = ImageFont
        options = RGBMatrixOptions()
        options.rows = config.matrix_height
        options.cols = config.matrix_width
        options.brightness = config.brightness
        options.gpio_slowdown = 2
        self.matrix = RGBMatrix(options=options)
        self.config = config
        self.font = ImageFont.load_default()

    def render(self, config: BoardConfig, entries: Iterable[DisplayTrain], error: str = "") -> None:
        image = self._image_type.new("RGB", (config.matrix_width, config.matrix_height), config.colors["background"])
        draw = self._draw_type.Draw(image)
        text_color = config.colors["text"]
        draw.text((1, 0), f"{config.station_code} {time.strftime('%H:%M')}", fill=text_color, font=self.font)
        for index, entry in enumerate(entries):
            row = 1 + index * 8
            if row + 7 >= config.matrix_height:
                break
            if "LATE" in entry.status_text:
                color = config.colors["late"]
            elif "EARLY" in entry.status_text:
                color = config.colors["early"]
            else:
                color = config.colors["on_time"]
            line = f"{entry.source[:4]} {entry.train_num} {_format_time(entry.expected_ts)} {entry.destination}"
            stops = entry.stops_text.removeprefix("Stops: ")
            draw.text((1, row), line[:20], fill=color, font=self.font)
            draw.text((1, row + 7), stops[:20], fill=config.colors["muted"], font=self.font)
        if error:
            draw.text((1, config.matrix_height - 6), "FEED ERROR", fill=config.colors["late"], font=self.font)
        self.matrix.SetImage(image)


class ConfigHandler(BaseHTTPRequestHandler):
    service: "PiBoard"  # assigned by factory

    def do_GET(self) -> None:
        if urlparse(self.path).path == "/api/config":
            self._json(asdict(self.service.config))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        if urlparse(self.path).path != "/api/config":
            self.send_error(404)
            return
        if self.service.config.control_token and self.headers.get("Authorization") != (
            "Bearer " + self.service.config.control_token
        ):
            self.send_error(401, "Authorization token required")
            return
        length = int(self.headers.get("Content-Length", "0"))
        values = json.loads(self.rfile.read(length))
        if "station_code" in values:
            self.service.config.station_code = str(values["station_code"]).strip().upper()
        if "brightness" in values:
            self.service.config.brightness = max(1, min(100, int(values["brightness"])))
        if "colors" in values:
            self.service.config.colors = {**self.service.config.colors, **dict(values["colors"])}
        self.service.config.save(self.service.config_path)
        self._json(asdict(self.service.config))

    def _json(self, value: Any) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_: Any) -> None:
        return


class PiBoard:
    def __init__(self, config: BoardConfig, config_path: Path, renderer: ConsoleRenderer) -> None:
        self.config = config
        self.config_path = config_path
        self.renderer = renderer
        self.service = FeedService(config)

    def run(self) -> None:
        handler = type("BoardConfigHandler", (ConfigHandler,), {})
        handler.service = self
        server = ThreadingHTTPServer((self.config.control_host, self.config.control_port), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        while True:
            error = ""
            try:
                entries = self.service.fetch()
            except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
                entries = []
                error = str(exc)
            self.renderer.render(self.config, entries, error)
            time.sleep(self.config.refresh_seconds)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the station board without requiring a desktop environment.")
    parser.add_argument("--config", help="JSON configuration path; defaults to /boot/destination-board.json")
    parser.add_argument("--once", action="store_true", help="Fetch and render one update, then exit")
    parser.add_argument("--renderer", choices=("console", "preview", "matrix"), default="console")
    args = parser.parse_args()
    path = find_config(args.config)
    config = BoardConfig.load(path)
    if args.renderer == "preview":
        PreviewRenderer(config, FeedService(config)).start()
        return
    renderer = ConsoleRenderer() if args.renderer == "console" else RgbMatrixRenderer(config)
    board = PiBoard(config, path, renderer)
    if args.once:
        try:
            board.renderer.render(config, board.service.fetch())
        except (OSError, requests.RequestException, RuntimeError, ValueError) as exc:
            board.renderer.render(config, [], str(exc))
        return
    board.run()


if __name__ == "__main__":
    main()

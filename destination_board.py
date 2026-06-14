from __future__ import annotations

import datetime as dt
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING, cast

import requests

if TYPE_CHECKING:
    import tkinter as tk

API_URL = "https://backend-unified.mylirr.org/locations"
API_HEADERS = {"Accept-Version": "3.0"}
AMTRAK_RT_URL = "https://asm-backend.transitdocs.com/gtfs/amtrak"
MNR_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfsmnr.zip"
LIRR_GTFS_URL = "https://rrgtfsfeeds.s3.amazonaws.com/gtfslirr.zip"
AMTRAK_GTFS_URL = "https://content.amtrak.com/content/gtfs/GTFS.zip"
DEFAULT_MTA_GTFS_URLS = (MNR_GTFS_URL, LIRR_GTFS_URL)

DEFAULT_STATION_CODE = "NYK"
DEFAULT_TRAIN_COUNT = 15
DEFAULT_REFRESH_SECONDS = 60
DEFAULT_SCROLL_MS = 180
DEFAULT_TITLE_TEXT = "DESTINATION BOARD"

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


@dataclass
class StationNameResolver:
    mnr_gtfs_url: str = MNR_GTFS_URL
    lirr_gtfs_url: str = LIRR_GTFS_URL
    amtrak_gtfs_url: str = AMTRAK_GTFS_URL
    cache_ttl_seconds: int = 6 * 60 * 60
    _mnr_names: Dict[str, str] = field(default_factory=dict, init=False)
    _lirr_names: Dict[str, str] = field(default_factory=dict, init=False)
    _mta_names: Dict[str, str] = field(default_factory=dict, init=False)
    _amtrak_names: Dict[str, str] = field(default_factory=dict, init=False)
    _last_loaded: float = field(default=0.0, init=False)

    def ensure_loaded(self) -> None:
        now = time.time()
        if self._last_loaded and now - self._last_loaded < self.cache_ttl_seconds:
            return

        mnr_names = self._load_mta_names(self.mnr_gtfs_url)
        lirr_names = self._load_mta_names(self.lirr_gtfs_url)
        amtrak_names = self._load_amtrak_names()

        self._mnr_names = mnr_names
        self._lirr_names = lirr_names
        self._mta_names = {**mnr_names, **lirr_names}
        self._amtrak_names = amtrak_names

        if mnr_names or lirr_names or amtrak_names:
            self._last_loaded = now

    def resolve_mta(self, code: str) -> str:
        if not code:
            return ""
        code = code.upper()
        return self._mta_names.get(code, code)

    def resolve_amtrak(self, code: str) -> str:
        if not code:
            return ""
        code = code.upper()
        return self._amtrak_names.get(code, code)

    def related_station_codes(self, code: str) -> List[str]:
        code = (code or "").upper().strip()
        if not code:
            return []

        candidates: List[str] = []
        seen: set[str] = set()

        def add(value: str) -> None:
            value = (value or "").upper().strip()
            if value and value not in seen:
                seen.add(value)
                candidates.append(value)

        add(code)
        for alias in STATION_CODE_ALIASES.get(code, []):
            add(alias)

        if not (self._mta_names or self._amtrak_names):
            return candidates

        names: set[str] = set()
        for candidate in candidates:
            mta_name = self._mta_names.get(candidate)
            if mta_name:
                names.add(_normalize_station_name(mta_name))
            amtrak_name = self._amtrak_names.get(candidate)
            if amtrak_name:
                names.add(_normalize_station_name(amtrak_name))

        if not names:
            return candidates

        for source_map in (self._mta_names, self._amtrak_names):
            for station_code, station_name in source_map.items():
                normalized_name = _normalize_station_name(station_name)
                if any(_station_names_related(normalized_name, known_name) for known_name in names):
                    add(station_code)

        return candidates

    def mta_family_for_code(self, code: str) -> str:
        code = (code or "").upper().strip()
        if not code:
            return "MTA"
        in_mnr = code in self._mnr_names
        in_lirr = code in self._lirr_names
        if in_mnr and not in_lirr:
            return "MNR"
        if in_lirr and not in_mnr:
            return "LIRR"
        return "MTA"

    def _load_mta_names(self, url: str) -> Dict[str, str]:
        return _load_gtfs_stop_names(url=url, code_field="stop_code")

    def _load_amtrak_names(self) -> Dict[str, str]:
        return _load_gtfs_stop_names(url=self.amtrak_gtfs_url, code_field="stop_id")


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


def _as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
        return _as_int(actual)
    otp = status.get("otp")
    if sched and otp is not None:
        sched_ts = _as_int(sched)
        otp_seconds = _as_int(otp)
        if sched_ts is not None and otp_seconds is not None:
            return sched_ts + otp_seconds
        return None
    if sched:
        return _as_int(sched)
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


def _normalize_station_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def _station_name_tokens(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", (name or "").lower()))


def _station_names_related(a: str, b: str) -> bool:
    normalized_a = _normalize_station_name(a)
    normalized_b = _normalize_station_name(b)
    if not normalized_a or not normalized_b:
        return False
    if normalized_a == normalized_b:
        return True
    if normalized_a in normalized_b or normalized_b in normalized_a:
        return True

    tokens_a = _station_name_tokens(a)
    tokens_b = _station_name_tokens(b)
    if not tokens_a or not tokens_b:
        return False

    shared = tokens_a & tokens_b
    return bool(shared) and (shared == tokens_a or shared == tokens_b)


def _fetch_amtrak_feed() -> Optional[Any]:
    try:
        from google.transit import gtfs_realtime_pb2
    except ModuleNotFoundError:
        return None

    try:
        response = requests.get(AMTRAK_RT_URL, timeout=12)
        response.raise_for_status()
        feed_class = getattr(gtfs_realtime_pb2, "FeedMessage", None)
        if feed_class is None:
            return None
        feed = feed_class()
        feed.ParseFromString(response.content)
        return feed
    except Exception:
        return None


def _parse_amtrak_train_number(trip_id: str) -> str:
    if not trip_id:
        return ""
    if "_" in trip_id:
        return trip_id.split("_")[-1]
    return trip_id


def _load_gtfs_stop_names(url: str, code_field: str) -> Dict[str, str]:
    try:
        response = requests.get(url, timeout=25)
        response.raise_for_status()
    except Exception:
        return {}

    try:
        import csv
        import io
        import zipfile

        with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
            with zf.open("stops.txt") as stops_file:
                reader = csv.DictReader(io.TextIOWrapper(stops_file, encoding="utf-8"))
                names: Dict[str, str] = {}
                for row in reader:
                    code = (row.get(code_field) or "").strip().upper()
                    name = (row.get("stop_name") or "").strip()
                    if code and name:
                        names[code] = name
                return names
    except Exception:
        return {}


def _train_display_entries(
    trains: Iterable[Dict[str, Any]],
    station_code: str,
    now_ts: int,
    name_resolver: StationNameResolver,
    source_label: str = "MTA",
) -> List[DisplayTrain]:
    results: List[DisplayTrain] = []
    station_codes = set(name_resolver.related_station_codes(station_code) or _station_code_candidates(station_code))

    for train in trains:
        details: Dict[str, Any] = train.get("details") or {}
        raw_stops = details.get("stops") or []  # type: ignore[assignment]
        stops = raw_stops if isinstance(raw_stops, list) else []  # type: ignore[assignment]
        stop_match: Optional[Dict[str, Any]] = next((s for s in stops if s.get("code") in station_codes), None)
        if stop_match is None:
            continue

        destination = details.get("headsign") or details.get("summary") or train.get("train_num") or "Unknown"
        scheduled_ts = stop_match.get("sched_time")
        status = train.get("status", {}) or {}
        expected_ts = _expected_timestamp(stop_match, status)
        platform = _pick_platform(stop_match)

        if expected_ts and expected_ts < now_ts - 300:
            continue

        stop_codes = [s.get("code") for s in stops if s.get("code")]
        stop_names = [name_resolver.resolve_mta(code) for code in stop_codes]
        stops_text = "Stops: " + " - ".join(stop_names) if stop_names else "Stops: (no data)"

        results.append(
            DisplayTrain(
                train_num=str(train.get("train_num", "")),
                destination=str(destination),
                platform=platform,
                scheduled_ts=int(scheduled_ts) if scheduled_ts else None,  # type: ignore[arg-type]
                expected_ts=int(expected_ts) if expected_ts else None,
                status_text=_compute_status(scheduled_ts, expected_ts),
                stops_text=stops_text,
                source=source_label,
            )
        )

    results.sort(key=lambda item: item.expected_ts or item.scheduled_ts or 0)
    return results


def _train_display_entries_amtrak(
    feed: Any,
    station_code: str,
    now_ts: int,
    name_resolver: StationNameResolver,
) -> List[DisplayTrain]:
    results: List[DisplayTrain] = []
    station_codes = set(name_resolver.related_station_codes(station_code) or _station_code_candidates(station_code))

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

        if expected_ts and expected_ts < now_ts - 300:
            continue

        results.append(
            DisplayTrain(
                train_num=train_num,
                destination=destination,
                platform="TBD",
                scheduled_ts=int(scheduled_ts) if scheduled_ts else None,
                expected_ts=int(expected_ts) if expected_ts else None,
                status_text=_compute_status(scheduled_ts, expected_ts or None),
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
        name_resolver: Optional[StationNameResolver] = None,
        mta_source_label: Optional[str] = None,
    ) -> None:
        import tkinter as tk
        import tkinter.font as tkfont

        self.tk = tk
        self.root = root
        self.station_code = station_code
        self.train_count = train_count
        self.refresh_seconds = refresh_seconds
        self.last_updated: int = 0
        self.last_error = ""
        self.last_notice = ""
        self.name_resolver = name_resolver or StationNameResolver()
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

        self.root.title(f"{title_text}")
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
        self.rows_frame.pack(fill="both", expand=True)

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
            line_label.pack(fill="x", padx=24)
            stops_label = tk.Label(
                self.rows_frame,
                text="Stops: (loading)",
                font=self.font_sub,
                fg="#ffb000",
                bg="black",
                anchor="w",
                justify="left",
            )
            stops_label.pack(fill="x", padx=36, pady=(0, 6))
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
            mta_label = self.mta_source_label or self.name_resolver.mta_family_for_code(self.station_code)
            trains = _fetch_trains()
            amtrak_feed = _fetch_amtrak_feed()
            self.last_notice = (
                "Amtrak feed unavailable right now; showing MTA data only."
                if amtrak_feed is None
                else ""
            )
            entries = _train_display_entries(
                trains,
                self.station_code,
                now_ts,
                self.name_resolver,
                source_label=mta_label,
            )
            if amtrak_feed is not None:
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
        elif self.last_notice:
            updated = _format_time(cast(int, self.last_updated))
            self.status_label.config(text=f"Last updated: {updated} | {self.last_notice}")  # type: ignore[arg-type]
        else:
            updated = _format_time(cast(int, self.last_updated))
            self.status_label.config(text=f"Last updated: {updated}")


def _prompt_station_code(default: str = DEFAULT_STATION_CODE) -> str:
    prompt = f"Enter a station code (Amtrak or MTA) [{default}]: "
    try:
        value = input(prompt).strip()
    except EOFError:
        return default
    return value or default


def _prompt_train_count(default: int = DEFAULT_TRAIN_COUNT) -> int:  # type: ignore[assignment]
    prompt = f"How many lines would you like displayed? [{default}]: "
    while True:
        try:
            value = input(prompt).strip()
        except EOFError:
            return default
        if not value:
            return default
        try:
            count = int(value)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if count <= 0:
            print("Please enter a positive number.")
            continue
        return count


def _title_for_station(station_code: str, resolver: StationNameResolver) -> str:
    code = station_code.upper().strip()
    station_name = resolver.resolve_amtrak(code)
    if station_name == code:
        station_name = resolver.resolve_mta(code)
    if station_name == code:
        return f"{code} {DEFAULT_TITLE_TEXT}"
    return f"{station_name} {DEFAULT_TITLE_TEXT}"


def main() -> None:
    import tkinter as tk

    station_code = _prompt_station_code()
    train_count = _prompt_train_count()

    resolver = StationNameResolver()
    resolver.ensure_loaded()
    title_text = _title_for_station(station_code, resolver)

    root = tk.Tk()
    BoardApp(
        root,
        station_code,
        train_count,
        DEFAULT_REFRESH_SECONDS,
        title_text=title_text,
        name_resolver=resolver,
    )
    root.mainloop()


if __name__ == "__main__":
    main()


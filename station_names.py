from __future__ import annotations

import csv
import io
import time
import zipfile
from dataclasses import dataclass, field
from typing import Dict

import requests


@dataclass
class StationNameResolver:
    mta_gtfs_url: str
    amtrak_gtfs_url: str
    cache_ttl_seconds: int = 6 * 60 * 60
    _mta_names: Dict[str, str] = field(default_factory=dict, init=False)
    _amtrak_names: Dict[str, str] = field(default_factory=dict, init=False)
    _last_loaded: float = field(default=0.0, init=False)

    def ensure_loaded(self) -> None:
        now = time.time()
        if self._last_loaded and now - self._last_loaded < self.cache_ttl_seconds:
            return
        self._mta_names = self._load_mta_names()
        self._amtrak_names = self._load_amtrak_names()
        self._last_loaded = now

    def resolve_mta(self, code: str) -> str:
        if not code:
            return ""
        return self._mta_names.get(code.upper(), code)

    def resolve_amtrak(self, code: str) -> str:
        if not code:
            return ""
        return self._amtrak_names.get(code.upper(), code)

    def _load_mta_names(self) -> Dict[str, str]:
        return _load_gtfs_stop_names(
            url=self.mta_gtfs_url,
            code_field="stop_code",
        )

    def _load_amtrak_names(self) -> Dict[str, str]:
        return _load_gtfs_stop_names(
            url=self.amtrak_gtfs_url,
            code_field="stop_id",
        )


def _load_gtfs_stop_names(url: str, code_field: str) -> Dict[str, str]:
    response = requests.get(url, timeout=25)
    response.raise_for_status()

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


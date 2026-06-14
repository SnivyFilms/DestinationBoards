from __future__ import annotations
from destination_board import StationNameResolver as _UnifiedStationNameResolver, _load_gtfs_stop_names
class StationNameResolver:
    def __init__(self, mta_gtfs_url: str, amtrak_gtfs_url: str, cache_ttl_seconds: int = 6 * 60 * 60) -> None:
        self._resolver = _UnifiedStationNameResolver(
            mnr_gtfs_url=mta_gtfs_url,
            lirr_gtfs_url=mta_gtfs_url,
            amtrak_gtfs_url=amtrak_gtfs_url,
            cache_ttl_seconds=cache_ttl_seconds,
        )
    def ensure_loaded(self) -> None:
        self._resolver.ensure_loaded()
    def resolve_mta(self, code: str) -> str:
        return self._resolver.resolve_mta(code)
    def resolve_amtrak(self, code: str) -> str:
        return self._resolver.resolve_amtrak(code)
    def __getattr__(self, name: str):
        return getattr(self._resolver, name)
__all__ = ["StationNameResolver", "_load_gtfs_stop_names"]

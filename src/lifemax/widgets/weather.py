"""Weather widget powered by Open-Meteo + ipapi.co (both keyless / free tier)."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import get_settings

logger = logging.getLogger(__name__)

_GEO_TTL_SECONDS = 6 * 60 * 60  # 6 hours
_WEATHER_TTL_SECONDS = 10 * 60  # 10 minutes

# https://open-meteo.com/en/docs (WMO weather codes)
_WMO_CODE_LABELS: dict[int, str] = {
    0: "Clear",
    1: "Mostly clear",
    2: "Partly cloudy",
    3: "Overcast",
    45: "Fog",
    48: "Rime fog",
    51: "Light drizzle",
    53: "Drizzle",
    55: "Heavy drizzle",
    61: "Light rain",
    63: "Rain",
    65: "Heavy rain",
    66: "Freezing rain",
    67: "Heavy freezing rain",
    71: "Light snow",
    73: "Snow",
    75: "Heavy snow",
    77: "Snow grains",
    80: "Light showers",
    81: "Showers",
    82: "Violent showers",
    85: "Snow showers",
    86: "Heavy snow showers",
    95: "Thunderstorm",
    96: "Thunderstorm w/ hail",
    99: "Severe thunderstorm",
}


@dataclass(slots=True)
class _Geo:
    latitude: float
    longitude: float
    city: str
    region: str
    country: str
    fetched_at: float


@dataclass(slots=True)
class _WeatherCache:
    data: dict[str, Any]
    fetched_at: float


class WeatherWidget:
    """Async weather fetcher with light TTL caching."""

    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        self._client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        self._owns_client = http_client is None
        self._geo: _Geo | None = None
        self._weather: _WeatherCache | None = None
        self._lock = asyncio.Lock()
        # Health vitals — read by widgets/health.py. Updated on each fetch.
        self.last_fetch_at: float = 0.0  # monotonic; set on success or fallback
        self.last_fetch_error: str | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _resolve_geo(self) -> _Geo:
        if (
            self._settings.latitude is not None
            and self._settings.longitude is not None
        ):
            return _Geo(
                latitude=float(self._settings.latitude),
                longitude=float(self._settings.longitude),
                city="",
                region="",
                country="",
                fetched_at=time.time(),
            )
        now = time.time()
        if self._geo is not None and (now - self._geo.fetched_at) < _GEO_TTL_SECONDS:
            return self._geo
        try:
            resp = await self._client.get("https://ipapi.co/json/")
            resp.raise_for_status()
            data = resp.json()
            geo = _Geo(
                latitude=float(data["latitude"]),
                longitude=float(data["longitude"]),
                city=str(data.get("city", "")),
                region=str(data.get("region_code") or data.get("region", "")),
                country=str(data.get("country_code") or data.get("country", "")),
                fetched_at=now,
            )
            self._geo = geo
            return geo
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            logger.warning("ipapi geolocation failed: %s", exc)
            # Sensible NYC fallback so the dashboard never goes dark.
            return _Geo(
                latitude=40.7128,
                longitude=-74.0060,
                city="New York",
                region="NY",
                country="US",
                fetched_at=now,
            )

    async def get(self) -> dict[str, Any]:
        """Return the latest weather snapshot, refreshing if stale."""
        async with self._lock:
            now = time.time()
            if (
                self._weather is not None
                and (now - self._weather.fetched_at) < _WEATHER_TTL_SECONDS
            ):
                return self._weather.data
            geo = await self._resolve_geo()
            params = {
                "latitude": geo.latitude,
                "longitude": geo.longitude,
                "current": "temperature_2m,weather_code,wind_speed_10m,relative_humidity_2m",
                "temperature_unit": "fahrenheit",
                "wind_speed_unit": "mph",
                "timezone": "auto",
            }
            try:
                resp = await self._client.get(
                    "https://api.open-meteo.com/v1/forecast", params=params
                )
                resp.raise_for_status()
                payload = resp.json()
                current = payload.get("current", {}) or {}
                code = int(current.get("weather_code", 0))
                snapshot = {
                    "temperature_f": current.get("temperature_2m"),
                    "humidity": current.get("relative_humidity_2m"),
                    "wind_mph": current.get("wind_speed_10m"),
                    "code": code,
                    "label": _WMO_CODE_LABELS.get(code, "—"),
                    "city": geo.city,
                    "region": geo.region,
                    "country": geo.country,
                    "lat": geo.latitude,
                    "lon": geo.longitude,
                    "updated_at": payload.get("current", {}).get("time"),
                }
                self._weather = _WeatherCache(data=snapshot, fetched_at=now)
                self.last_fetch_at = time.monotonic()
                self.last_fetch_error = None
                return snapshot
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                logger.warning("open-meteo fetch failed: %s", exc)
                fallback = {
                    "temperature_f": None,
                    "humidity": None,
                    "wind_mph": None,
                    "code": None,
                    "label": "Unavailable",
                    "city": geo.city,
                    "region": geo.region,
                    "country": geo.country,
                    "lat": geo.latitude,
                    "lon": geo.longitude,
                    "updated_at": None,
                }
                self._weather = _WeatherCache(data=fallback, fetched_at=now)
                self.last_fetch_at = time.monotonic()
                self.last_fetch_error = f"{type(exc).__name__}: {exc}"
                return fallback

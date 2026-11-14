"""Geo-grid math: N x N lat/lng points around a center."""
from __future__ import annotations

import math

EARTH_KM = 6371.0088


def offset(lat: float, lng: float, north_km: float, east_km: float) -> tuple[float, float]:
    """Move a point north/east by km (equirectangular, accurate for city-size grids)."""
    dlat = math.degrees(north_km / EARTH_KM)
    dlng = math.degrees(east_km / (EARTH_KM * math.cos(math.radians(lat))))
    return lat + dlat, lng + dlng


def spacing_from(grid_size: int, spacing_km: float | None, radius_km: float | None) -> float:
    if spacing_km and spacing_km > 0:
        return float(spacing_km)
    if radius_km and radius_km > 0 and grid_size > 1:
        return 2 * float(radius_km) / (grid_size - 1)
    return 1.0


def make_grid(lat: float, lng: float, grid_size: int, spacing_km: float) -> list[dict]:
    """Row 0 is the northern edge, col 0 the western edge. The center point is (n//2, n//2)."""
    if grid_size < 1 or grid_size % 2 == 0:
        raise ValueError('gridSize must be an odd number (3, 5, 7, 9...)')
    half = grid_size // 2
    pts = []
    for r in range(grid_size):
        for c in range(grid_size):
            plat, plng = offset(lat, lng, (half - r) * spacing_km, (c - half) * spacing_km)
            pts.append({'gridRow': r, 'gridCol': c, 'pointIndex': r * grid_size + c,
                        'lat': round(plat, 6), 'lng': round(plng, 6),
                        'isCenter': r == half and c == half})
    return pts


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * EARTH_KM * math.asin(math.sqrt(h))

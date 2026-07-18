"""Local solar-position calculations without network or third-party dependencies."""

from __future__ import annotations

import math
from datetime import datetime


def solar_elevation(observed_at: datetime, *, latitude: float, longitude: float) -> float:
    """Return apparent solar elevation in degrees using NOAA's standard approximation."""

    if observed_at.tzinfo is None or observed_at.utcoffset() is None:
        raise ValueError("observed_at must be timezone-aware")
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180")

    local = observed_at
    day = local.timetuple().tm_yday
    hour = local.hour + local.minute / 60 + local.second / 3600
    gamma = 2 * math.pi / 365 * (day - 1 + (hour - 12) / 24)
    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )
    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )
    utc_offset = local.utcoffset()
    if utc_offset is None:  # Guard for custom tzinfo implementations.
        raise ValueError("observed_at must have a valid UTC offset")
    offset_hours = utc_offset.total_seconds() / 3600
    time_offset = equation_of_time + 4 * longitude - 60 * offset_hours
    true_solar_minutes = (hour * 60 + time_offset) % 1440
    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    latitude_radians = math.radians(latitude)
    cosine_zenith = math.sin(latitude_radians) * math.sin(declination) + math.cos(
        latitude_radians
    ) * math.cos(declination) * math.cos(hour_angle)
    zenith = math.acos(max(-1.0, min(1.0, cosine_zenith)))
    elevation = 90 - math.degrees(zenith)

    # NOAA atmospheric-refraction correction; irrelevant below the horizon.
    if elevation > 85:
        refraction = 0.0
    elif elevation > 5:
        tangent = math.tan(math.radians(elevation))
        refraction = (58.1 / tangent - 0.07 / tangent**3 + 0.000086 / tangent**5) / 3600
    elif elevation > -0.575:
        refraction = (
            1735
            + elevation * (-518.2 + elevation * (103.4 + elevation * (-12.79 + elevation * 0.711)))
        ) / 3600
    else:
        refraction = -20.772 / math.tan(math.radians(elevation)) / 3600
    return elevation + refraction


def is_production_window(
    observed_at: datetime,
    *,
    latitude: float,
    longitude: float,
    minimum_elevation_degrees: float = 10.0,
) -> bool:
    """Production is expected only strictly above the configured elevation."""

    return (
        solar_elevation(observed_at, latitude=latitude, longitude=longitude)
        > minimum_elevation_degrees
    )

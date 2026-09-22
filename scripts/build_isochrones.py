"""
Step 1: Geocode Richmond Yacht Club and generate drive-time isochrones
(15/30/45/60 min by default — see config.py) using openrouteservice.

Output: data/isochrones.geojson
  - One polygon feature per band.
  - Each feature's properties include "minutes" (int) for later styling
    and joining.

Note on ORS isochrones: each returned polygon represents the FULL area
reachable within that time (i.e. the 30-min polygon already contains the
15-min polygon), not an exclusive ring. We keep them this way and handle
"which band does this zip belong to" logic downstream in build_dataset.py.
"""

import sys
import json
from pathlib import Path
from geopy.geocoders import Nominatim
import openrouteservice

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config


def geocode_ryc():
    geolocator = Nominatim(user_agent="ryc_dinner_map")
    location = geolocator.geocode(config.RYC_ADDRESS)
    if location is None:
        raise RuntimeError(
            f"Could not geocode address: {config.RYC_ADDRESS!r}. "
            "Double check the address in config.py."
        )
    print(f"Geocoded RYC to: {location.latitude}, {location.longitude}")
    return location.latitude, location.longitude


def build_isochrones(lat, lon):
    if not config.ORS_API_KEY:
        raise RuntimeError(
            "ORS_API_KEY environment variable is not set. "
            "See README.md for how to get a free key and set it."
        )

    client = openrouteservice.Client(key=config.ORS_API_KEY)

    # ORS wants coordinates as [lon, lat], and ranges in SECONDS.
    ryc_coords = [[lon, lat]]
    ranges_seconds = [m * 60 for m in config.DRIVE_TIME_BANDS_MIN]

    print(f"Requesting isochrones for bands (min): {config.DRIVE_TIME_BANDS_MIN}")
    response = client.isochrones(
        locations=ryc_coords,
        profile="driving-car",
        range=ranges_seconds,
        attributes=["area"],
    )

    # Attach a clean "minutes" property to each feature for easy styling
    # and joining later (ORS gives us "value" in seconds by default).
    for feature in response["features"]:
        seconds = feature["properties"]["value"]
        feature["properties"]["minutes"] = int(seconds / 60)

    return response


def main():
    lat, lon = geocode_ryc()

    # Save RYC's real coordinates so the dashboard can place the marker
    # accurately, instead of guessing from the zip data later.
    with open(config.RYC_LOCATION_PATH, "w") as f:
        json.dump({"lat": lat, "lon": lon}, f)

    isochrones = build_isochrones(lat, lon)

    with open(config.ISOCHRONES_PATH, "w") as f:
        json.dump(isochrones, f)

    print(f"Saved {len(isochrones['features'])} isochrone bands to {config.ISOCHRONES_PATH}")


if __name__ == "__main__":
    main()

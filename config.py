"""
Central settings for the pipeline. Edit values here rather than
hunting through each script.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Richmond Yacht Club address, geocoded by build_isochrones.py.
RYC_ADDRESS = "351 Brickyard Cove Road, Richmond, CA 94807"

# Drive-time bands (minutes). Everything downstream reads this list.
DRIVE_TIME_BANDS_MIN = [15, 30, 45, 60]

# openrouteservice key (see README). Optional for build_dataset.py: without
# it, zips are banded from the isochrone polygons instead of routed times.
ORS_API_KEY = os.environ.get("ORS_API_KEY")

# Private inputs -- data/raw/ is gitignored and must never be published.
MEMBERS_RAW_PATH = ROOT / "data" / "raw" / "members_export.csv"

# Pipeline intermediates (safe to commit, no member data).
ISOCHRONES_PATH = ROOT / "data" / "isochrones.geojson"
RYC_LOCATION_PATH = ROOT / "data" / "ryc_location.json"
DRIVE_TIME_CACHE_PATH = ROOT / "data" / "cache" / "zip_drive_times.csv"

# Public outputs, served by GitHub Pages from docs/.
PUBLIC_DATA_DIR = ROOT / "docs" / "data"

# Census vintage for ZCTA and county boundaries.
CENSUS_YEAR = 2020

# Privacy: age and membership-type detail is suppressed for any zip with
# fewer members than this, so no individual's age can be read off the map.
MIN_CELL_SIZE = 5

# In isochrone fallback mode, a zip is assigned the smallest band that
# covers at least this share of its area.
ISOCHRONE_OVERLAP_THRESHOLD = 0.25

# PO-box-only zips have no Census ZCTA polygon. Map each to the ZCTA that
# contains its post office so those members are still counted.
PO_BOX_ZIP_TO_ZCTA = {
    "94807": "94801",  # Richmond
    "94978": "94930",  # Fairfax
    "94979": "94963",  # San Geronimo
    "94035": "94043",  # Moffett Field -> Mountain View
}

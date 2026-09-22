# Richmond Yacht Club — Galley Catchment Study

A drive-time trade-area analysis of Richmond Yacht Club membership, built to
help the board decide whether expanding the Galley (dining room) is worth the
investment. It applies retail site-selection methods: drive-time trade areas,
member penetration by band, and a distance-decay demand model.

**Live dashboard:** published from `docs/` via GitHub Pages.

## How it works

```
data/raw/members_export.csv  (private, gitignored)
        │
scripts/build_isochrones.py  → data/isochrones.geojson, data/ryc_location.json
scripts/build_dataset.py     → docs/data/{zips.geojson, rings.geojson, summary.json}
        │
docs/index.html              static dashboard (Leaflet), served by GitHub Pages
```

- **Drive time:** openrouteservice isochrones (15/30/45/60 min). With
  `ORS_API_KEY` set, `build_dataset.py` also routes the clubhouse to every zip
  for exact minutes and road miles, cached in `data/cache/`. Without a key it
  falls back to banding zips by isochrone area overlap.
- **Geography:** 2020 Census ZCTAs and counties via `pygris`. PO-box zips are
  mapped to their containing ZCTA in `config.PO_BOX_ZIP_TO_ZCTA`.
- **Privacy:** only aggregated counts are published. Age and membership-type
  detail is suppressed for zips with fewer than `MIN_CELL_SIZE` (5) members.
  The raw roster lives in `data/raw/` and is excluded from git.

## Running it

```
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export ORS_API_KEY="..."             # free key: openrouteservice.org
python scripts/build_isochrones.py   # only when bands or the club location change
python scripts/build_dataset.py      # after every new roster export
python -m http.server -d docs 8000   # preview at http://localhost:8000
```

To update the roster, save the club's export as `data/raw/members_export.csv`
(columns: Age, Member Type, Member Status, City, State, Zipcode) and rerun
`build_dataset.py`.

---
Analysis & cartography: Cameron Gibson, MS GIS.

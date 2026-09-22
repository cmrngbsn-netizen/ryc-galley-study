"""
Build the public dataset for the dashboard from the private membership
export. Run from the project root:

    python scripts/build_dataset.py

Inputs:  data/raw/members_export.csv (private), data/isochrones.geojson,
         data/ryc_location.json (both from build_isochrones.py)
Outputs: docs/data/zips.geojson, docs/data/rings.geojson,
         docs/data/summary.json -- aggregated only, with small cells
         suppressed (see config.MIN_CELL_SIZE).

Each roster row is one membership (which may be a family), not one person.

Drive time per zip:
  - With ORS_API_KEY set, each zip's representative point is routed from
    the club via the ORS Matrix API (minutes + road miles), cached in
    data/cache/ so reruns don't spend API quota.
  - Zips that can't be routed (or every zip, without a key) get the
    smallest isochrone band covering at least ISOCHRONE_OVERLAP_THRESHOLD
    of their area.
"""

import sys
import json
import time
import datetime
from pathlib import Path

import pandas as pd
import geopandas as gpd
from pygris import zctas, counties

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

BBOX_PADDING_DEGREES = 0.15  # ~10 miles around the 60-min isochrone
EQUAL_AREA_CRS = "EPSG:3310"  # California Albers, for area overlap
SIMPLIFY_TOLERANCE_M = 40
MATRIX_BATCH_SIZE = 50
AGE_GROUPS = [(18, 39, "Under 40"), (40, 49, "40–49"), (50, 59, "50–59"),
              (60, 69, "60–69"), (70, 79, "70–79"), (80, 200, "80+")]


# ---------------------------------------------------------------- memberships

def load_memberships():
    # The club export has a "Table 1" title row and a blank row above the data.
    df = pd.read_csv(config.MEMBERS_RAW_PATH, skiprows=1, dtype=str).dropna(how="all")
    df = df.rename(columns={"Age": "age", "Member Type": "member_type",
                            "State": "state", "Zipcode": "zip_raw"})
    df["state"] = df["state"].fillna("").str.strip().str.upper()
    raw_zip = df["zip_raw"].fillna("").str.strip()
    # ZIP+4 ("94510-1234") -> 5-digit; PO-box zips -> containing ZCTA.
    df["zip_code"] = raw_zip.str[:5].str.zfill(5).replace(config.PO_BOX_ZIP_TO_ZCTA)
    age = pd.to_numeric(df["age"], errors="coerce")
    quality = {
        "age_missing": int(age.isna().sum()),
        "age_invalid": int((age < 18).sum()),  # e.g. 0 used as a placeholder
        "zip_plus4_normalized": int(raw_zip.str.contains("-").sum()),
        "po_box_remapped": int(raw_zip.str[:5].isin(config.PO_BOX_ZIP_TO_ZCTA).sum()),
    }
    df["age"] = age.where(age >= 18)
    df["member_type"] = df["member_type"].str.strip().str.title()
    return df[["zip_code", "state", "age", "member_type"]], quality


# ---------------------------------------------------------------- geography

def load_isochrones():
    iso = gpd.read_file(config.ISOCHRONES_PATH)[["minutes", "geometry"]]
    return iso.sort_values("minutes").reset_index(drop=True)


def load_zips_and_counties(iso):
    minx, miny, maxx, maxy = iso.total_bounds
    p = BBOX_PADDING_DEGREES
    bbox = (minx - p, miny - p, maxx + p, maxy + p)

    zips = zctas(cb=True, year=config.CENSUS_YEAR, subset_by=bbox)
    zip_field = next(c for c in zips.columns if c.startswith("ZCTA5CE"))
    zips = zips.rename(columns={zip_field: "zip_code"})[["zip_code", "geometry"]].to_crs(4326)

    cty = counties(cb=True, year=config.CENSUS_YEAR, subset_by=bbox)
    cty = cty[["NAME", "geometry"]].rename(columns={"NAME": "county"}).to_crs(4326)

    # County by representative point (always inside the polygon, unlike a centroid).
    pts = zips.copy()
    pts["geometry"] = zips.representative_point()
    pts = gpd.sjoin(pts, cty, predicate="within", how="left").drop_duplicates("zip_code")
    zips["county"] = pts["county"].values
    rp = zips.to_crs(EQUAL_AREA_CRS).representative_point().to_crs(4326)
    zips["pt_lon"], zips["pt_lat"] = rp.x.round(5), rp.y.round(5)
    return zips.reset_index(drop=True)


# ---------------------------------------------------------------- drive times

def routed_drive_times(zips, ryc):
    """Minutes and road miles from the club to each zip via ORS Matrix, cached."""
    import openrouteservice

    cache = pd.DataFrame(columns=["zip_code", "drive_min", "distance_mi"])
    if config.DRIVE_TIME_CACHE_PATH.exists():
        cache = pd.read_csv(config.DRIVE_TIME_CACHE_PATH, dtype={"zip_code": str})

    todo = zips[~zips["zip_code"].isin(cache["zip_code"])]
    if len(todo):
        client = openrouteservice.Client(key=config.ORS_API_KEY)
        rows = []
        print(f"Routing {len(todo)} zips via ORS Matrix...")
        for start in range(0, len(todo), MATRIX_BATCH_SIZE):
            batch = todo.iloc[start:start + MATRIX_BATCH_SIZE]
            locations = [[ryc["lon"], ryc["lat"]]] + batch[["pt_lon", "pt_lat"]].values.tolist()
            resp = client.distance_matrix(
                locations=locations, sources=[0],
                destinations=list(range(1, len(locations))),
                profile="driving-car", metrics=["duration", "distance"], units="mi",
            )
            for zc, dur, dist in zip(batch["zip_code"], resp["durations"][0], resp["distances"][0]):
                rows.append({"zip_code": zc,
                             "drive_min": None if dur is None else round(dur / 60, 1),
                             "distance_mi": None if dist is None else round(dist, 1)})
            time.sleep(1.5)  # free-tier rate limit
        cache = pd.concat([cache, pd.DataFrame(rows)], ignore_index=True)
        config.DRIVE_TIME_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        cache.to_csv(config.DRIVE_TIME_CACHE_PATH, index=False)

    return zips.merge(cache, on="zip_code", how="left")


def isochrone_bands(zips, iso):
    """Smallest band whose isochrone covers enough of the zip's area."""
    z = zips.to_crs(EQUAL_AREA_CRS)
    iso_eq = iso.to_crs(EQUAL_AREA_CRS)
    zip_area = z.geometry.area
    band = pd.Series([None] * len(z), dtype=object)
    for _, row in iso_eq.iterrows():  # ascending minutes
        share = z.geometry.intersection(row.geometry).area / zip_area
        band[(share >= config.ISOCHRONE_OVERLAP_THRESHOLD) & band.isna()] = row["minutes"]
    return band.values


def assign_bands(zips, iso, ryc):
    bands = sorted(config.DRIVE_TIME_BANDS_MIN)
    zips["iso_band"] = isochrone_bands(zips, iso)
    if config.ORS_API_KEY:
        zips = routed_drive_times(zips, ryc)
    else:
        zips["drive_min"] = None
        zips["distance_mi"] = None
    routed_band = zips["drive_min"].apply(
        lambda m: next((b for b in bands if m <= b), None) if pd.notna(m) else None)
    zips["band_source"] = zips["drive_min"].notna().map({True: "routed", False: "isochrone"})
    zips["band"] = routed_band.where(zips["drive_min"].notna(), zips["iso_band"])
    return zips.drop(columns="iso_band")


def band_outlines(zips, smooth_m=800, min_part_km2=3):
    """Display rings built from the zips themselves: for each band, dissolve
    every zip whose routed time is within it (cumulative), then close small
    gaps, drop holes and slivers, and simplify. The rings therefore agree
    exactly with the zip band assignments used everywhere else."""
    from shapely.geometry import MultiPolygon, Polygon

    z = zips[zips["band"].notna()].to_crs(EQUAL_AREA_CRS)
    rows = []
    for b in sorted(config.DRIVE_TIME_BANDS_MIN):
        geom = z[z["band"] <= b].union_all().buffer(smooth_m).buffer(-smooth_m)
        parts = geom.geoms if isinstance(geom, MultiPolygon) else [geom]
        parts = [Polygon(p.exterior).simplify(150) for p in parts if p.area >= min_part_km2 * 1e6]
        rows.append({"minutes": b, "geometry": MultiPolygon(parts) if len(parts) > 1 else parts[0]})
    return gpd.GeoDataFrame(rows, crs=EQUAL_AREA_CRS).to_crs(4326)


# ---------------------------------------------------------------- aggregation

def suppress(value, n):
    return None if n < config.MIN_CELL_SIZE or pd.isna(value) else round(float(value), 1)


def aggregate_by_zip(ms):
    g = ms.groupby("zip_code")
    agg = pd.DataFrame({
        "memberships": g.size(),
        "regular": g["member_type"].apply(lambda s: (s == "Regular").sum()),
        "associate": g["member_type"].apply(lambda s: (s == "Associate").sum()),
        "n_aged": g["age"].count(),
        "median_age": g["age"].median(),
    }).reset_index()
    small = agg["memberships"] < config.MIN_CELL_SIZE
    # Why a zip shows no age: privacy suppression vs. too few recorded ages.
    agg["age_note"] = None
    agg.loc[small, "age_note"] = "suppressed"
    agg.loc[~small & (agg["n_aged"] < config.MIN_CELL_SIZE), "age_note"] = "insufficient"
    agg["median_age"] = [suppress(a, n) for a, n in zip(agg["median_age"], agg["n_aged"])]
    agg.loc[small, ["regular", "associate", "median_age"]] = None
    return agg.drop(columns="n_aged")


def band_label(band, bands):
    i = bands.index(band)
    return f"{0 if i == 0 else bands[i - 1]}–{band} min"


def age_group(age):
    if pd.isna(age):
        return "Not recorded"
    return next(label for lo, hi, label in AGE_GROUPS if lo <= age <= hi)


def build_summary(ms, zips_out, ryc, quality):
    bands = sorted(config.DRIVE_TIME_BANDS_MIN)
    zip_band = dict(zip(zips_out["zip_code"], zips_out["band"]))

    ms = ms.copy()
    in_ca = ms["state"].isin({"CA", "CALIFORNIA"})
    ms["band"] = ms["zip_code"].map(zip_band)
    ms.loc[~in_ca, "band"] = None

    total = len(ms)
    band_rows, cumulative = [], 0
    for b in bands:
        sub = ms[ms["band"] == b]
        cumulative += len(sub)
        n_aged = sub["age"].count()
        band_rows.append({
            "band": b,
            "label": band_label(b, bands),
            "memberships": len(sub),
            "share": round(len(sub) / total, 4),
            "cumulative": cumulative,
            "cumulative_share": round(cumulative / total, 4),
            "regular": int((sub["member_type"] == "Regular").sum()),
            "associate": int((sub["member_type"] == "Associate").sum()),
            "median_age": suppress(sub["age"].median(), n_aged),
            "share_70_plus": suppress(100 * (sub["age"] >= 70).sum() / n_aged if n_aged else None, n_aged),
            "ages_recorded": int(n_aged),
            "zips_with_memberships": int(sub["zip_code"].nunique()),
        })

    # Age mix by broad distance group. These cells span dozens of zips, so
    # they don't identify anyone; cells under MIN_CELL_SIZE are still nulled.
    ms["reach"] = ms["band"].map(lambda b: "Within 30 min" if b in (15, 30)
                                 else "30–60 min" if b in (45, 60) else "Beyond 60 min")
    ms["age_group"] = ms["age"].map(age_group)
    order = [a[2] for a in AGE_GROUPS] + ["Not recorded"]
    ct = pd.crosstab(ms["age_group"], ms["reach"]).reindex(order, fill_value=0)
    age_mix = [{"age_group": ag, "reach": r, "memberships": None if v < config.MIN_CELL_SIZE else int(v)}
               for ag, row in ct.iterrows() for r, v in row.items()]

    within = ms[ms["band"].notna()]
    county = (within.merge(zips_out[["zip_code", "county"]], on="zip_code", how="left")
              .groupby("county").size().sort_values(ascending=False))

    top = (zips_out[zips_out["memberships"] >= config.MIN_CELL_SIZE]
           .sort_values("memberships", ascending=False)
           .head(15)[["zip_code", "county", "band", "drive_min", "distance_mi", "memberships"]])

    member_zips = zips_out[zips_out["memberships"] > 0]
    small = member_zips[member_zips["memberships"] < config.MIN_CELL_SIZE]
    quality.update({
        "zips_suppressed": int(len(small)),
        "memberships_in_suppressed_zips": int(small["memberships"].sum()),
        "zips_isochrone_fallback": int((member_zips["band_source"] == "isochrone").sum()),
    })

    return {
        "generated": datetime.date.today().isoformat(),
        "club": {"name": "Richmond Yacht Club", "lat": ryc["lat"], "lon": ryc["lon"]},
        "drive_time_method": "routed" if config.ORS_API_KEY else "isochrone_overlap",
        "min_cell_size": config.MIN_CELL_SIZE,
        "totals": {
            "all_memberships": total,
            "california": int(in_ca.sum()),
            "out_of_state": int((~in_ca).sum()),
            "within_60": int(len(within)),
            "ca_beyond_60": int(in_ca.sum() - len(within)),
            "median_age_all": float(ms["age"].median()),
            "regular": int((ms["member_type"] == "Regular").sum()),
            "associate": int((ms["member_type"] == "Associate").sum()),
        },
        "data_quality": quality,
        "bands": band_rows,
        "age_mix": age_mix,
        "counties": [{"county": k, "memberships": int(v)} for k, v in county.items()],
        "top_zips": json.loads(top.to_json(orient="records")),
    }


# ---------------------------------------------------------------- main

def main():
    ms, quality = load_memberships()
    iso = load_isochrones()
    ryc = json.loads(config.RYC_LOCATION_PATH.read_text())

    print("Fetching Census ZCTA and county boundaries...")
    zips = load_zips_and_counties(iso)
    if not config.ORS_API_KEY:
        print("ORS_API_KEY not set -- banding zips by isochrone overlap only.")
    zips = assign_bands(zips, iso, ryc)

    ca = ms[ms["state"].isin({"CA", "CALIFORNIA"})]
    zips = zips.merge(aggregate_by_zip(ca), on="zip_code", how="left")
    zips["memberships"] = zips["memberships"].fillna(0).astype(int)

    # Flag Bay Area membership zips with no ZCTA polygon -- likely PO boxes
    # that need an entry in config.PO_BOX_ZIP_TO_ZCTA.
    unmatched = sorted(set(ca["zip_code"]) - set(zips["zip_code"]))
    near = [z for z in unmatched if z[:3] in {"940", "941", "944", "945", "946", "947", "948", "949"}]
    if near:
        print(f"WARNING: Bay Area zips with no ZCTA (add to PO_BOX_ZIP_TO_ZCTA?): {near}")

    # Publish zips inside 60 min, plus any farther zip in the window with memberships.
    out = zips[zips["band"].notna() | (zips["memberships"] > 0)].copy()
    for col in ["band", "drive_min", "distance_mi"]:  # object dtype would serialize as strings
        out[col] = pd.to_numeric(out[col], errors="coerce")
    summary = build_summary(ms, out, ryc, quality)

    out = out.to_crs(EQUAL_AREA_CRS)
    out["geometry"] = out.simplify(SIMPLIFY_TOLERANCE_M, preserve_topology=True)
    out = out.to_crs(4326)

    config.PUBLIC_DATA_DIR.mkdir(parents=True, exist_ok=True)
    cols = ["zip_code", "county", "band", "band_source", "drive_min", "distance_mi",
            "memberships", "regular", "associate", "median_age", "age_note",
            "pt_lon", "pt_lat", "geometry"]
    out[cols].to_file(config.PUBLIC_DATA_DIR / "zips.geojson", driver="GeoJSON",
                      COORDINATE_PRECISION=5)
    band_outlines(zips).to_file(config.PUBLIC_DATA_DIR / "rings.geojson", driver="GeoJSON",
                                 COORDINATE_PRECISION=5)
    (config.PUBLIC_DATA_DIR / "isochrones.geojson").unlink(missing_ok=True)
    (config.PUBLIC_DATA_DIR / "summary.json").write_text(json.dumps(summary, indent=2))

    t = summary["totals"]
    print(f"\nMethod: {summary['drive_time_method']}")
    print(f"{t['all_memberships']} memberships | {t['california']} CA | {t['within_60']} within 60 min "
          f"| {t['ca_beyond_60']} CA beyond | {t['out_of_state']} out of state")
    for b in summary["bands"]:
        print(f"  {b['label']:>10}: {b['memberships']:>4}  (cumulative {b['cumulative_share']:.0%})")
    print("Data quality:", summary["data_quality"])
    print(f"Wrote {len(out)} zips to {config.PUBLIC_DATA_DIR}")


if __name__ == "__main__":
    main()

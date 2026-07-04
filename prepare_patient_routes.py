"""
Niram Palliative Care — patient data cleaning + weekly cluster assignment
--------------------------------------------------------------------------
Reads the raw MASTER_DATA_PATIENTS.csv export, cleans/normalizes it, splits
patients into visit-frequency tiers, and runs geographic clustering within
each tier so the ambulance visits the same geographic group on the same
fixed weekday every cycle.

Outputs (in /mnt/user-data/outputs/):
  1. patients_cleaned.csv      - full cleaned roster with new columns
  2. patients_active.geojson   - active patients only, ready for the map
  3. hospital_depot.geojson    - single depot point (EDIT THE COORDINATES BELOW)
  4. needs_review.csv          - patients with missing/unclear mobility data

Run again any time the roster changes (new enrollments, deaths, discharges)
to regenerate the day assignments.
"""

import csv
import json
import math
import random
from collections import defaultdict

# ---------------------------------------------------------------------------
# CONFIG — edit these for your setup
# ---------------------------------------------------------------------------

INPUT_CSV = "/mnt/user-data/uploads/MASTER_DATA_PATIENTS.csv"
OUTPUT_DIR = "/mnt/user-data/outputs"

# TODO: replace with Niram's actual hospital / base coordinates once you have them
HOSPITAL_NAME = "Niram Palliative Care Center"
HOSPITAL_LAT = 13.3350   # placeholder — Tumkur town center approx
HOSPITAL_LON = 77.1010   # placeholder — Tumkur town center approx

# Fixed weekdays available to each visit-frequency tier. Cluster count per
# tier = number of days listed here, so each cluster maps 1:1 to a day and
# that mapping stays stable cycle after cycle.
WEEKLY_DAYS = ["Monday", "Wednesday", "Friday"]        # bedbound / most urgent
FORTNIGHTLY_DAYS = ["Tuesday", "Thursday"]              # chairbound
MONTHLY_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]  # mobile

# Soft guidance only (script will warn, not hard-stop, if exceeded)
MAX_STOPS_PER_DAY = 10

RANDOM_SEED = 42

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def normalize_mobility(raw):
    r = (raw or "").strip().lower().replace(" ", "")
    if r in ("bedbound",):
        return "Bedbound"
    if r in ("chairbound",):
        return "Chairbound"
    if r in ("mobile",):
        return "Mobile"
    return "Unknown"


def normalize_case(raw):
    r = (raw or "").strip().upper()
    if r == "CA":
        return "CA"
    if r == "NON CA":
        return "NON CA"
    return "Unknown"


def frequency_tier_for(mobility):
    return {
        "Bedbound": "Weekly",
        "Chairbound": "Fortnightly",
        "Mobile": "Monthly",
    }.get(mobility, "Needs Review")


# ---------------------------------------------------------------------------
# Minimal k-means (no external deps beyond numpy), flat-earth corrected
# ---------------------------------------------------------------------------

def project_xy(lat, lon, ref_lat):
    """Equirectangular projection scaled to km, adequate for a ~50km span."""
    R = 6371.0
    x = math.radians(lon) * math.cos(math.radians(ref_lat)) * R
    y = math.radians(lat) * R
    return x, y


def kmeans(points, k, iters=100, seed=RANDOM_SEED):
    """points: list of (x, y). Returns list of cluster indices, same order as points."""
    rnd = random.Random(seed)
    n = len(points)
    if n == 0:
        return []
    k = max(1, min(k, n))
    centroids = [points[i] for i in rnd.sample(range(n), k)]

    assignments = [0] * n
    for _ in range(iters):
        changed = False
        for i, p in enumerate(points):
            dists = [(p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 for c in centroids]
            best = dists.index(min(dists))
            if assignments[i] != best:
                assignments[i] = best
                changed = True
        new_centroids = []
        for c_idx in range(k):
            members = [points[i] for i in range(n) if assignments[i] == c_idx]
            if members:
                mx = sum(m[0] for m in members) / len(members)
                my = sum(m[1] for m in members) / len(members)
                new_centroids.append((mx, my))
            else:
                new_centroids.append(centroids[c_idx])
        centroids = new_centroids
        if not changed:
            break
    return assignments, centroids


def assign_days_to_clusters(patients_subset, ref_lat, day_list):
    """Cluster a list of patient dicts geographically and label each with a
    fixed weekday, ordered west-to-east so labeling is stable and intuitive."""
    if not patients_subset:
        return {}

    points = [project_xy(p["_lat"], p["_lon"], ref_lat) for p in patients_subset]
    k = len(day_list)
    assignments, centroids = kmeans(points, k)

    # order cluster indices by centroid longitude (x) so day labels read
    # west -> east consistently, rather than in arbitrary k-means order
    cluster_order = sorted(range(len(centroids)), key=lambda c: centroids[c][0])
    cluster_to_day = {cluster_idx: day_list[rank] for rank, cluster_idx in enumerate(cluster_order)}

    day_assignment = {}
    for p, cluster_idx in zip(patients_subset, assignments):
        day_assignment[p["_row_id"]] = cluster_to_day[cluster_idx]
    return day_assignment


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def main():
    with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    cleaned = []
    for idx, r in enumerate(rows):
        mobility = normalize_mobility(r.get("TYPE_OF_PATIENT", ""))
        case_type = normalize_case(r.get("CASE", ""))
        status = "Deceased" if (r.get("DATE_OF_DEATH") or "").strip() else "Active"
        tier = frequency_tier_for(mobility)

        try:
            lat = float(r["latitude"])
            lon = float(r["longitude"])
        except (ValueError, KeyError):
            lat, lon = None, None

        rec = dict(r)
        rec["_row_id"] = idx
        rec["MOBILITY_NORMALIZED"] = mobility
        rec["CASE_NORMALIZED"] = case_type
        rec["STATUS"] = status
        rec["FREQUENCY_TIER"] = tier
        rec["_lat"] = lat
        rec["_lon"] = lon
        cleaned.append(rec)

    active = [r for r in cleaned if r["STATUS"] == "Active" and r["_lat"] is not None]
    needs_review = [r for r in cleaned if r["FREQUENCY_TIER"] == "Needs Review" and r["STATUS"] == "Active"]

    ref_lat = HOSPITAL_LAT

    # cluster each tier separately, skipping "Needs Review" (handled manually)
    tier_days = {
        "Weekly": WEEKLY_DAYS,
        "Fortnightly": FORTNIGHTLY_DAYS,
        "Monthly": MONTHLY_DAYS,
    }
    day_assignment = {}
    for tier, day_list in tier_days.items():
        subset = [r for r in active if r["FREQUENCY_TIER"] == tier]
        assignment = assign_days_to_clusters(subset, ref_lat, day_list)
        day_assignment.update(assignment)

    for r in active:
        r["ASSIGNED_DAY"] = day_assignment.get(r["_row_id"], "")
    for r in needs_review:
        r["ASSIGNED_DAY"] = "UNASSIGNED — review mobility status"

    # ---- warnings ----
    day_counts = defaultdict(int)
    for r in active:
        if r["ASSIGNED_DAY"]:
            day_counts[r["ASSIGNED_DAY"]] += 1
    print("Stops per assigned day:")
    for day, count in sorted(day_counts.items()):
        flag = "  <-- over soft limit" if count > MAX_STOPS_PER_DAY else ""
        print(f"  {day}: {count}{flag}")
    print(f"\nActive patients: {len(active)} / {len(cleaned)} total")
    print(f"Needs review (unclear mobility): {len(needs_review)}")

    # ---- write cleaned CSV (all patients, active + deceased) ----
    fieldnames = list(rows[0].keys()) + [
        "MOBILITY_NORMALIZED", "CASE_NORMALIZED", "STATUS", "FREQUENCY_TIER", "ASSIGNED_DAY"
    ]
    with open(f"{OUTPUT_DIR}/patients_cleaned.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in cleaned:
            row_out = {k: r.get(k, "") for k in fieldnames}
            row_out["ASSIGNED_DAY"] = day_assignment.get(r["_row_id"], "" if r["STATUS"] == "Active" else "")
            writer.writerow(row_out)

    # ---- write needs_review CSV ----
    with open(f"{OUTPUT_DIR}/needs_review.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in needs_review:
            row_out = {k: r.get(k, "") for k in fieldnames}
            row_out["ASSIGNED_DAY"] = "UNASSIGNED — review mobility status"
            writer.writerow(row_out)

    # ---- write active patients GeoJSON for the map ----
    features = []
    for r in active:
        features.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [r["_lon"], r["_lat"]]},
            "properties": {
                "name": r.get("NAME", ""),
                "registration_number": r.get("REGISTRATION_NUMBER", ""),
                "mobility": r["MOBILITY_NORMALIZED"],
                "case_type": r["CASE_NORMALIZED"],
                "diagnosis": r.get("DIAGNOSIS", ""),
                "address": r.get("ADDRESS", ""),
                "phone": r.get("PHONE_NUMBER", ""),
                "frequency_tier": r["FREQUENCY_TIER"],
                "assigned_day": r["ASSIGNED_DAY"],
            }
        })
    with open(f"{OUTPUT_DIR}/patients_active.geojson", "w", encoding="utf-8") as f:
        json.dump({"type": "FeatureCollection", "features": features}, f, indent=2)

    # ---- write hospital depot GeoJSON ----
    depot = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [HOSPITAL_LON, HOSPITAL_LAT]},
            "properties": {"name": HOSPITAL_NAME, "facility": "Depot"}
        }]
    }
    with open(f"{OUTPUT_DIR}/hospital_depot.geojson", "w", encoding="utf-8") as f:
        json.dump(depot, f, indent=2)

    print(f"\nWrote patients_cleaned.csv, patients_active.geojson, hospital_depot.geojson, needs_review.csv to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()

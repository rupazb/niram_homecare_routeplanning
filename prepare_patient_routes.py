"""
Niram Palliative Care — patient data cleaning + weekly cluster assignment
--------------------------------------------------------------------------
Reads the raw MASTER_DATA_PATIENTS.csv export, cleans/normalizes it, splits
patients into visit-frequency tiers, and runs geographic clustering within
each tier so the ambulance visits the same geographic group on the same
fixed weekday every cycle.

Outputs (in /mnt/user-data/outputs/):
  1. patients_cleaned.csv      - full cleaned roster with new columns
  2. patients_all.geojson      - active + deceased, ready for the map
                                 (status field lets the dashboard toggle
                                 deceased patients on/off; only active
                                 patients carry an assigned_day)
  3. hospital_depot.geojson    - single depot point (EDIT THE COORDINATES BELOW)
  4. needs_review.csv          - patients with missing/unclear mobility data

Run again any time the roster changes (new enrollments, deaths, discharges)
to regenerate the day assignments.
"""

import csv
import json
import math
import random
import io
import urllib.request
from collections import defaultdict

# ---------------------------------------------------------------------------
# CONFIG — edit these for your setup
# ---------------------------------------------------------------------------

# If SHEET_CSV_EXPORT_URL is set, it takes priority over INPUT_CSV — the
# Google Sheet becomes the single source of truth and this script always
# reads whatever is currently in it. Get this URL from the Sheet:
#   File > Share > Publish to web > select the patients tab > CSV > Publish
# (a plain "export?format=csv" link works too, but only if the sheet is
# shared as "Anyone with the link — Viewer"; Publish to web is more reliable).
SHEET_CSV_EXPORT_URL = ""  # e.g. "https://docs.google.com/spreadsheets/d/e/XXXXX/pub?output=csv"
INPUT_CSV = "/mnt/user-data/uploads/MASTER_DATA_PATIENTS.csv"  # fallback if the URL above is blank

# Apps Script Web App URL (see google_apps_script.gs) — used to write the
# computed FREQUENCY_TIER / ASSIGNED_DAY columns back into the Sheet so the
# dashboard can read a fully-scheduled roster straight from Sheets. Leave
# blank to skip write-back (outputs will still be written locally either way).
SHEET_WEBAPP_URL = ""  # e.g. "https://script.google.com/macros/s/XXXXX/exec"

OUTPUT_DIR = "/mnt/user-data/outputs"

# Niram Palliative Care Center — actual hospital/base coordinates
HOSPITAL_NAME = "Niram Palliative Care Center"
HOSPITAL_LAT = 13.374905768194475
HOSPITAL_LON = 77.09982271414759

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


def load_rows():
    """Read patient rows from the live Google Sheet if configured, else the
    local CSV. Returns a list of dicts, same shape either way."""
    if SHEET_CSV_EXPORT_URL:
        print(f"Reading live data from Google Sheet: {SHEET_CSV_EXPORT_URL}")
        with urllib.request.urlopen(SHEET_CSV_EXPORT_URL) as resp:
            text = resp.read().decode("utf-8-sig")
        return list(csv.DictReader(io.StringIO(text)))
    else:
        print(f"Reading local CSV: {INPUT_CSV}")
        with open(INPUT_CSV, newline="", encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))


def push_schedule_to_sheet(cleaned):
    """POST computed FREQUENCY_TIER / ASSIGNED_DAY back to the Sheet via the
    Apps Script Web App, matched by row number (data row 1 = sheet row 2).
    No-op if SHEET_WEBAPP_URL isn't set."""
    if not SHEET_WEBAPP_URL:
        return

    updates = []
    for i, r in enumerate(cleaned):
        updates.append({
            "row": i + 2,  # +1 for 0-index, +1 for header row
            "frequency_tier": r["FREQUENCY_TIER"],
            "assigned_day": r.get("ASSIGNED_DAY", "")
        })

    body = json.dumps({"action": "sync_schedule", "updates": updates}).encode("utf-8")
    req = urllib.request.Request(SHEET_WEBAPP_URL, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            print("Wrote schedule back to Sheet:", resp.read().decode("utf-8")[:200])
    except Exception as e:
        print(f"Warning: could not write schedule back to Sheet ({e}). Local outputs were still generated.")


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
    """points: list of (x, y). Returns (assignments, centroids). Unconstrained —
    used only to get a good starting set of centroids for the balanced pass below."""
    rnd = random.Random(seed)
    n = len(points)
    if n == 0:
        return [], []
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


def capacitated_assign(points, centroids, capacity):
    """Greedy nearest-cluster assignment with a hard cap per cluster, so no
    cluster (day) ends up with more than `capacity` patients. Processes all
    (point, cluster) pairs in order of increasing distance, claiming the
    nearest still-open slot for each point first."""
    k = len(centroids)
    n = len(points)
    pairs = []
    for i, p in enumerate(points):
        for c_idx, c in enumerate(centroids):
            d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2
            pairs.append((d, i, c_idx))
    pairs.sort(key=lambda t: t[0])

    assignments = [-1] * n
    counts = [0] * k
    for d, i, c_idx in pairs:
        if assignments[i] != -1:
            continue
        if counts[c_idx] < capacity:
            assignments[i] = c_idx
            counts[c_idx] += 1
    return assignments


def balanced_kmeans(points, k, refine_iters=4, seed=RANDOM_SEED):
    """Balanced clustering: seed centroids with standard k-means, then
    alternate (a) capacity-constrained assignment and (b) centroid
    recomputation, so cluster sizes stay within +/-1 of each other while
    still respecting geography as much as the balance constraint allows."""
    n = len(points)
    if n == 0:
        return [], []
    k = max(1, min(k, n))
    capacity = math.ceil(n / k)

    _, centroids = kmeans(points, k, seed=seed)
    assignments = capacitated_assign(points, centroids, capacity)

    for _ in range(refine_iters):
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
        assignments = capacitated_assign(points, centroids, capacity)

    return assignments, centroids


def assign_days_to_clusters(patients_subset, ref_lat, day_list):
    """Cluster a list of patient dicts geographically and label each with a
    fixed weekday, ordered west-to-east so labeling is stable and intuitive.
    Uses balanced_kmeans so no single day gets overloaded relative to others."""
    if not patients_subset:
        return {}

    points = [project_xy(p["_lat"], p["_lon"], ref_lat) for p in patients_subset]
    k = len(day_list)
    assignments, centroids = balanced_kmeans(points, k)

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
    rows = load_rows()

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

    # ---- write full patients GeoJSON for the map (active + deceased) ----
    # Deceased patients get no assigned_day (excluded from routing/clustering)
    # but are included so the dashboard can show/hide them via a toggle.
    features = []
    for r in cleaned:
        if r["_lat"] is None or r["_lon"] is None:
            continue
        assigned_day = r["ASSIGNED_DAY"] if r["STATUS"] == "Active" else ""
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
                "status": r["STATUS"],
                "date_of_death": r.get("DATE_OF_DEATH", ""),
                "frequency_tier": r["FREQUENCY_TIER"],
                "assigned_day": assigned_day,
            }
        })
    with open(f"{OUTPUT_DIR}/patients_all.geojson", "w", encoding="utf-8") as f:
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

    print(f"\nWrote patients_cleaned.csv, patients_all.geojson, hospital_depot.geojson, needs_review.csv to {OUTPUT_DIR}")

    push_schedule_to_sheet(cleaned)


if __name__ == "__main__":
    main()

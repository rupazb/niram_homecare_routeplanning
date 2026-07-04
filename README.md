# Niram Ambulance Route Planner - README

Weekly home-visit route planning for Niram Palliative Care Center's single ambulance,
built on the same Leaflet-based architecture as the WWS Locator Tool.

---

## 1. What this system does

Niram has one ambulance and a roster of home-based palliative care patients spread
across Tumkur district. This tool:

1. Cleans the raw patient roster export
2. Splits patients into visit-frequency tiers (weekly / fortnightly / monthly) based
   on mobility status
3. Geographically clusters patients within each tier and assigns each cluster a
   fixed weekday, so the same patients are visited on the same day every cycle
4. On any given day, calls a route optimizer to work out the best order to visit
   that day's patients, starting and ending at the hospital
5. Displays everything on an interactive map with a sidebar showing the ordered
   stop list, distances, and drive times

---

## 2. Architecture overview

```
 Patient records (CSV)      Hospital depot (fixed point)
          |                            |
          +------------+---------------+
                       |
              Data preprocessing
        (clean, normalize, flag active/deceased)
                       |
        +--------------+---------------+
        |                              |
 Weekly scheduler                Route optimizer
 (frequency tier +                (ORS Optimization API
  balanced geographic              = VROOM engine, then
  clustering -> fixed day)         ORS Directions API for
        |                          road geometry + leg distances)
        +--------------+---------------+
                       |
              Dashboard and map
       (day tabs, stop list, route line, ETA/summary)
```

This is a two-stage pipeline:

- **Stage 1 (offline, run when the roster changes)**: `prepare_patient_routes.py`
  cleans the data and assigns each active patient a fixed weekday.
- **Stage 2 (live, runs in the browser)**: `niram_route_dashboard.html` loads that
  prepared data, and for whichever day the user picks, calls the optimizer to
  sequence the visits and draw the route.

Splitting it this way keeps the heavy, infrequent work (clustering the whole
roster) out of the browser, and keeps the browser's job limited to what actually
needs to happen every day: sequencing a handful of stops and drawing a route.

---

## 3. Files

| File | Role |
|---|---|
| `prepare_patient_routes.py` | Cleans the CSV, tiers patients by mobility, runs balanced geographic clustering, writes the outputs below |
| `patients_cleaned.csv` | Full roster (active + deceased) with normalized fields, frequency tier, and assigned day |
| `patients_all.geojson` | Same data as a GeoJSON, used directly by the map |
| `hospital_depot.geojson` | Single fixed point for the hospital / ambulance base |
| `needs_review.csv` | Patients with missing/unclear mobility data — excluded from auto-scheduling until fixed |
| `niram_route_dashboard.html` | The map + dashboard the ambulance team actually uses day to day |

---

## 4. Stage 1 logic: `prepare_patient_routes.py`

### 4.1 Cleaning
- `TYPE_OF_PATIENT` is normalized to `Bedbound` / `Chairbound` / `Mobile` /
  `Unknown` (the raw export has inconsistent casing/spacing, e.g. "Chair bound"
  vs "Chairbound").
- `CASE` is normalized to `CA` / `NON CA` / `Unknown`.
- `STATUS` is derived from whether `DATE_OF_DEATH` is filled in: `Active` or
  `Deceased`. Deceased patients are kept in the dataset (for records and map
  display) but excluded from scheduling and routing.

### 4.2 Frequency tiers
Mobility status drives how often a patient needs a visit:

| Mobility | Tier | Rationale |
|---|---|---|
| Bedbound | Weekly | Highest care need |
| Chairbound | Fortnightly | Moderate need |
| Mobile | Monthly | Lower need, more self-sufficient |
| Unknown | Needs Review | Can't be safely auto-scheduled — goes to `needs_review.csv` for a human to classify |

Each tier has its own fixed set of weekdays it can be assigned to (configurable
at the top of the script):
- Weekly → Monday, Wednesday, Friday
- Fortnightly → Tuesday, Thursday
- Monthly → Monday–Friday (spread across whichever days have room)

### 4.3 Balanced geographic clustering
Within each tier, patients are grouped into clusters — one cluster per available
weekday — using a **capacity-constrained k-means**:

1. Project lat/long to a flat x/y plane in kilometers (longitude scaled by
   `cos(latitude)` so distances aren't distorted — fine for a ~50km span).
2. Run standard k-means to get initial cluster centers.
3. Re-assign patients using a **greedy capacity-constrained pass**: every
   (patient, cluster) pair is sorted by distance, and each patient claims the
   nearest cluster that still has room (`capacity = ceil(patients / days)`).
   This is what prevents one day from ending up with 7 stops while another gets 2.
4. Recompute cluster centers from the new assignment and repeat the constrained
   pass a few times to let geography re-settle within the balance constraint.
5. Clusters are labeled with actual weekdays in west-to-east order (by cluster
   center longitude), so the same geographic group always maps to the same day
   across runs — not an arbitrary label that shuffles every time the script runs.

**Trade-off**: strict balancing sometimes assigns a patient to their
second-nearest cluster instead of the closest one, if the closest is already
full. This keeps ambulance workload even day to day, at the cost of occasionally
non-optimal geographic grouping. If a specific day's actual driving distance
(from Stage 2) turns out unreasonable, loosen the balance (e.g. allow ±1 or ±2
patients of variance) rather than forcing exact equality.

### 4.4 Outputs
The script writes the cleaned CSV, the two GeoJSON files, and the review CSV,
and prints a per-day stop count summary so you can sanity-check the balance
before deploying.

**Re-run this script any time the roster changes** — new enrollments, deaths,
discharges, or corrected mobility statuses. It's fully deterministic (same
random seed) so re-running with the same input reproduces the same clusters.

---

## 5. Stage 2 logic: `niram_route_dashboard.html`

### 5.1 Map and data loading
On load, it fetches `hospital_depot.geojson` and `patients_all.geojson` from the
same folder (same pattern as the WWS tool's `livemap.geojson` fetch). The
hospital renders as a fixed dark marker; patients render as colored circles by
mobility tier, with deceased patients shown in gray and hidden by default (toggle
in the sidebar).

### 5.2 Day selection
Clicking a day tab filters the view: patients scheduled that day are shown at
full opacity, everyone else dims. This uses the `assigned_day` field written by
Stage 1 — no clustering happens in the browser.

### 5.3 Route optimization — two API calls, two jobs
Route optimization is deliberately split across two OpenRouteService endpoints
that each do one thing well:

1. **Optimization endpoint (`/optimization`, VROOM engine)** — given the
   hospital as a fixed start/end point and that day's patients as "jobs," this
   returns the best **visiting order**. It does not return road geometry.
2. **Directions endpoint (`/v2/directions/driving-car/geojson`)** — takes that
   order and requests the full multi-stop route as one call (hospital → patient
   1 → patient 2 → … → hospital). This returns actual road geometry to draw, plus
   **per-leg distance and duration** used to populate the stop list.

This avoids needing to decode ORS's encoded polyline format — the Directions
endpoint returns ready-to-draw GeoJSON, same as the WWS tool already uses for
single-destination routes.

### 5.4 Sidebar
Once optimized, the sidebar shows:
- Each stop in visiting order, with mobility badge, diagnosis, and distance/time
  from the previous stop
- A numbered marker on the map for each stop (1, 2, 3…) matching the sidebar list
- A summary card with total distance, total drive time, and stop count

Clicking a stop in the list pans the map to it and opens its popup.

---

## 6. Setup

1. Get an OpenRouteService API key (free tier covers this scale) and paste it
   into `ORS_API_KEY` near the top of `niram_route_dashboard.html`.
2. Run `prepare_patient_routes.py` against the latest patient CSV. Check the
   printed per-day stop counts look reasonable, and check `needs_review.csv` for
   anyone needing manual classification.
3. Place `hospital_depot.geojson`, `patients_all.geojson`, and
   `niram_route_dashboard.html` together in the same folder and open the HTML
   file (or host it on any static web server).

---

## 7. Known limitations / natural next steps

- **No time windows or priority weighting yet.** The optimizer treats all of a
  day's stops as equally urgent and unconstrained by time. Adding `time_windows`
  or `priority` to the Optimization API call would let bedbound/CA patients be
  visited earlier in the day, or accommodate a family's request for a morning-only
  visit.
- **No manual override in the dashboard.** If the optimized order doesn't match
  local knowledge (a road closure, a family's availability), there's currently no
  drag-to-reorder in the UI — the fix is to re-run with adjusted inputs or
  manually edit `assigned_day` in the CSV.
- **Roster changes require re-running Stage 1.** The dashboard doesn't re-cluster
  live; any new enrollment or death means regenerating and re-uploading the two
  GeoJSON files.
- **Phone numbers in the source CSV render in scientific notation** (an Excel
  formatting artifact) — worth fixing at the data-entry source since accurate
  contact numbers matter for confirming visits ahead of time.
- **API key is client-side.** As currently built, the ORS key sits in plain text
  in the HTML, visible to anyone who views the page source — fine for an internal
  tool on a private network, but worth moving behind a small backend proxy if this
  is ever exposed publicly.

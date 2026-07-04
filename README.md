# Niram Ambulance Route Planner — README

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
        Google Sheet (single source of truth for patient data)
        columns include latitude/longitude, mobility, diagnosis, etc.
         |                                            ^
         | read (Sheets API,                          | write
         | client-side, live)                          | (Apps Script Web App:
         v                                              |  add_patient, sync_schedule)
   Dashboard map                                        |
   (day tabs OR custom                                  |
    selection; new-location                             |
    picker routes to hospital,                           |
    "save to roster" appends -----------------------------
    a row via Apps Script)
         ^
         | ASSIGNED_DAY / FREQUENCY_TIER
         | written back after clustering
         |
   prepare_patient_routes.py  <---- reads the same Sheet (CSV export)
   (clean, tier, balanced geographic clustering -> fixed weekday)
```

The Google Sheet is the single database for patient data. Two separate paths
touch it:

- **Reads are direct and live.** The dashboard calls the Google Sheets API
  (read-only, API key) on load and on demand ("Refresh from Sheet"), so any
  edit made directly in the Sheet — or any row appended through the dashboard
  — shows up on the map immediately, no export/upload step. `prepare_patient_routes.py`
  also reads straight from the Sheet (via its CSV export link) when configured to.
- **Writes go through the Apps Script Web App**, because a plain API key can
  only read a Sheet, not write to it (see §5.6/§7 for why). Two things write:
  the dashboard's "save this location" form (appends a new row), and
  `prepare_patient_routes.py` after it clusters the roster (writes the
  `FREQUENCY_TIER` and `ASSIGNED_DAY` columns back in, matched by row number).

This means only **`ASSIGNED_DAY` needs the offline Python step** — everything
else (mobility, case type, active/deceased status, frequency tier) is
normalized client-side in the dashboard from the raw Sheet columns, mirroring
the same rules the Python script uses, so a brand-new patient added through
the form is immediately usable in Custom selection mode even before Stage 1
has re-run. It just won't have a day assignment yet.

---

## 3. Files

| File | Role |
|---|---|
| `prepare_patient_routes.py` | Reads the live Sheet (or a local CSV fallback), tiers patients by mobility, runs balanced geographic clustering, writes local outputs, and writes `FREQUENCY_TIER`/`ASSIGNED_DAY` back into the Sheet |
| `patients_cleaned.csv` / `patients_all.geojson` | Local snapshots of the same data, handy for offline testing or backup — the dashboard no longer depends on these directly |
| `hospital_depot.geojson` | Single fixed point for the hospital / ambulance base (kept as a small local file since it rarely changes) |
| `needs_review.csv` | Patients with missing/unclear mobility data — excluded from auto-scheduling until fixed |
| `niram_route_dashboard_v3.html` | The map + dashboard: reads patients live from the Sheet, day-based or custom selection routing, new-location routing, Sheet write-back |
| `google_apps_script.gs` | Deployed on the master Google Sheet as a Web App; handles both new-patient appends and the Python script's schedule write-back |

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

## 5. Stage 2 logic: `niram_route_dashboard_v3.html`

### 5.1 Live data from the Sheet
On load (and whenever "Refresh from Sheet" is clicked), the dashboard calls
the Google Sheets API directly — `GET https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}?key={apiKey}`
— and parses the returned rows by header name into the same patient-feature
shape used everywhere else in the app. Mobility, case type, and active/deceased
status are normalized client-side (mirroring the Python script's rules exactly),
so this works correctly even for a patient added seconds ago through the
dashboard's own form. `ASSIGNED_DAY` is read directly from the Sheet if
present — it's the one field that genuinely requires the offline clustering
step, since it depends on balancing across the whole active roster, not a
per-row rule.

Hospital location is still loaded from a small local `hospital_depot.geojson`
— it's one fixed point that essentially never changes, so there's no real
benefit to routing it through the Sheet too.

### 5.2 Map and markers

### 5.3 Day selection
Clicking a day tab filters the view: patients scheduled that day are shown at
full opacity, everyone else dims. This uses the `assigned_day` field written by
Stage 1 — no clustering happens in the browser.

### 5.4 Route optimization — two API calls, two jobs
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

### 5.5 Sidebar
Once optimized, the sidebar shows:
- Each stop in visiting order, with mobility badge, diagnosis, and distance/time
  from the previous stop
- A numbered marker on the map for each stop (1, 2, 3…) matching the sidebar list
- A summary card with total distance, total drive time, and stop count

Clicking a stop in the list pans the map to it and opens its popup.

### 5.6 Custom patient selection
Alongside the day-based schedule, the "Custom selection" tab lets staff
hand-pick any set of active patients from a searchable checklist — useful for
an ad-hoc trip, a rescheduled visit, or testing "what if we combined these
three." Checking or unchecking a patient debounces (700ms) and automatically
re-runs the same optimizer used for day mode — there's no separate code path;
`computeAndDrawRoute()` takes a list of patients regardless of how that list
was chosen. This keeps day-based and custom routing consistent and means any
future improvement to the optimizer (time windows, priority) benefits both
modes at once.

### 5.7 New location — routes to the hospital, optionally saves to the roster
The WWS tool's original "click the map, or type coordinates" behavior is
retained, but repointed: instead of finding the nearest facility of each type,
it now draws a single direct route from the clicked/typed point straight to
Niram Palliative Care Center, and shows the distance/time in the sidebar. This
is useful for quickly checking "how far is this address from the hospital"
before committing it to the roster.

If the location should become a new patient, "Save this location to the
roster" opens a small form (name, mobility, case type, diagnosis, address,
phone). Submitting it POSTs the coordinates and details to a **Google Apps
Script Web App** bound to the master Google Sheet, which appends a new row.

**Important technical note**: a plain Google Sheets API key can only *read* a
sheet that's shared as "anyone with the link" — it cannot write to it. Writing
requires either an OAuth2 sign-in flow or a service account, both heavier than
this needs. The practical, widely-used workaround (used here) is deploying a
small Apps Script bound to the sheet as a Web App: the dashboard POSTs plain
JSON to its URL, and the script appends the row server-side. See
`google_apps_script.gs` for the code and deployment steps. Because the
dashboard calls it with `mode: 'no-cors'` (required for a plain static page
without its own backend), the browser can't read a confirmation back from the
script — the "Saved" message in the UI is optimistic, not a verified
round-trip. The row is still written correctly; if a verified round-trip
matters later, that needs a small server of your own in front of the Sheet
instead of calling the Apps Script URL directly from the browser.

**This does not make clustering live.** A newly saved patient sits in the
Google Sheet (and needs registration number, age, and gender filled in
manually) until someone exports the updated sheet to CSV and re-runs
`prepare_patient_routes.py` — that's what assigns them a frequency tier, a
day, and folds them into `patients_all.geojson`. Until then, the new patient
won't appear in day-based scheduling, though they can still be routed
individually via the "new location" panel.

---

## 6. Setup

1. Put the patient roster into a Google Sheet (a tab named `Patients`, with
   at minimum the same columns as `MASTER_DATA_PATIENTS.csv`, including
   `latitude` and `longitude`). Share it as **Anyone with the link — Viewer**.
2. In Google Cloud Console, enable the **Google Sheets API** on a project and
   create an **API key** restricted to that API. Paste it into
   `GOOGLE_SHEETS_API_KEY` in `niram_route_dashboard_v3.html`, along with the
   spreadsheet's ID (from its URL) into `SPREADSHEET_ID`, and the tab/range
   into `SHEET_RANGE` (e.g. `Patients!A:Z`).
3. Deploy `google_apps_script.gs` on the same Sheet as a Web App (steps are in
   the file's header comment). Paste the resulting URL into `SHEET_WEBAPP_URL`
   in both the dashboard and `prepare_patient_routes.py`.
4. Get an OpenRouteService API key (free tier covers this scale) and paste it
   into `ORS_API_KEY` near the top of `niram_route_dashboard_v3.html`.
5. In `prepare_patient_routes.py`, set `SHEET_CSV_EXPORT_URL` to the Sheet's
   published CSV link (File > Share > Publish to web > the Patients tab > CSV),
   then run the script. Check the printed per-day stop counts, check
   `needs_review.csv` for anyone needing manual classification, and confirm it
   logs that it wrote the schedule back to the Sheet.
6. Place `hospital_depot.geojson` and `niram_route_dashboard_v3.html` together
   in a folder and open the HTML file (or host it on any static web server).
   Patient data itself now comes live from the Sheet — no need to also ship
   `patients_all.geojson` alongside it, though it's still generated locally
   each run as a convenient offline snapshot/backup.

---

## 7. Known limitations / natural next steps

- **No time windows or priority weighting yet.** The optimizer treats all of a
  day's (or a custom selection's) stops as equally urgent and unconstrained by
  time. Adding `time_windows` or `priority` to the Optimization API call would
  let bedbound/CA patients be visited earlier in the day, or accommodate a
  family's request for a morning-only visit.
- **New patients aren't auto-clustered.** Saving a new location shows up on
  the map immediately (via "Refresh from Sheet") and is usable right away in
  Custom selection mode, but it won't get a scheduled weekday until someone
  re-runs `prepare_patient_routes.py`, since day assignment depends on
  balancing across the whole active roster, not a per-row rule.
- **No manual drag-to-reorder within an optimized route.** If the optimized
  order doesn't match local knowledge (a road closure, a family's availability),
  there's currently no in-UI reordering — the fix is to adjust the custom
  selection and re-optimize, or edit the Sheet and re-run Stage 1.
- **Sheet write-back has no read confirmation from the dashboard**, as
  explained in §5.7 — the row is reliably appended, but the UI's "Saved"
  message is optimistic rather than a verified response. Python's write-back
  (§4, `push_schedule_to_sheet`) does get a real response, since it's a normal
  server-to-server call rather than a browser fetch in `no-cors` mode.
- **Phone numbers in the source CSV render in scientific notation** (an Excel
  formatting artifact) — worth fixing at the data-entry source since accurate
  contact numbers matter for confirming visits ahead of time.
- **Both API keys are client-side.** The ORS key and the Google Sheets read-only
  API key both sit in plain text in the dashboard's HTML, visible to anyone who
  views the page source. This is fine for an internal tool on a private
  network. Since the Sheets key is read-only and the sheet itself is already
  shared as link-viewable, the exposure is limited to "anyone could read the
  same data anyway" — but if this is ever exposed publicly, both keys are
  worth moving behind a small backend proxy, and the Sheet's sharing should be
  reconsidered given it contains patient health information.
- **Every dashboard load/refresh re-fetches the whole sheet range.** Fine at
  dozens of patients; if the roster grows into the thousands, narrow
  `SHEET_RANGE` or add pagination rather than pulling the full range every time.

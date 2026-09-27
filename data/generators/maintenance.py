"""Condition-monitoring data for 12 pumps at three customer sites (Kestrel Care remote monitoring).

Injected patterns (ground truth in maintenance/ground_truth.json):
  HF-KP250-03  bearing wear: vibration and bearing temperature trend upward, crossing alarm ~2026-09-12
  GB-KP400-02  cavitation: erratic vibration + pressure fluctuation + flow drop, nightly 01:00-05:00
  CC-KP100-04  sensor fault: vibration flat-lines at 0.0 while the pump runs (2026-09-08 06:00 - 09-10 14:00)
  CC-KP600-01  overload: flow drifts right of BEP, motor current creeps above rated
  GB-KP250-03  misalignment: step change in vibration after coupling replacement on 2026-08-28
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import random
from pathlib import Path

ASSETS = [
    # asset_id, site, customer_id, model, serial, install_date, criticality, foundation, rated_current_a, bep_flow, notes
    ("GB-KP400-01", "GBWD North pump station", "C-1019", "KP-400-S", "KP400-2512-0003", "2025-12-12", "A", "flexible", 132, 950, "Duty pump, north reservoir supply"),
    ("GB-KP400-02", "GBWD North pump station", "C-1019", "KP-400-S", "KP400-2512-0004", "2025-12-12", "A", "flexible", 132, 950, "Duty/standby pair with -01; draws from low-level reservoir at night"),
    ("GB-KP250-03", "GBWD North pump station", "C-1019", "KP-250-S", "KP250-2403-0117", "2024-03-20", "B", "rigid", 41, 210, "Booster pump; coupling replaced 2026-08-28"),
    ("GB-KP250-04", "GBWD North pump station", "C-1019", "KP-250-S", "KP250-2403-0118", "2024-03-20", "B", "rigid", 41, 210, "Booster standby"),
    ("HF-KP250-01", "Harbor Foods Plant 2", "C-1005", "KP-250-S", "KP250-2505-0201", "2025-05-15", "B", "rigid", 41, 210, "CIP supply"),
    ("HF-KP250-02", "Harbor Foods Plant 2", "C-1005", "KP-250-S", "KP250-2505-0202", "2025-05-15", "B", "rigid", 41, 210, "Process water"),
    ("HF-KP250-03", "Harbor Foods Plant 2", "C-1005", "KP-250-S", "KP250-2411-0154", "2024-11-02", "A", "rigid", 41, 210, "Chilled water loop, runs 24/7"),
    ("HF-KP100-04", "Harbor Foods Plant 2", "C-1005", "KP-100-S", "KP100-2501-0077", "2025-01-20", "C", "rigid", 11, 45, "Washdown"),
    ("CC-KP600-01", "Cobalt Chemical main site", "C-1002", "KP-600-M", "KP600-2410-0012", "2024-10-08", "A", "flexible", 198, 180, "Boiler feed"),
    ("CC-KP250X-02", "Cobalt Chemical main site", "C-1002", "KP-250-X", "KP250-2605-0301", "2026-05-22", "A", "rigid", 41, 210, "Acid transfer, Zone 1 (ATEX)"),
    ("CC-KP250X-03", "Cobalt Chemical main site", "C-1002", "KP-250-X", "KP250-2605-0302", "2026-05-22", "A", "rigid", 41, 210, "Acid transfer, Zone 1 (ATEX), standby"),
    ("CC-KP100-04", "Cobalt Chemical main site", "C-1002", "KP-100-S", "KP100-2308-0033", "2023-08-14", "C", "rigid", 11, 45, "Cooling tower make-up"),
]

START = dt.datetime(2026, 8, 16, 0, 0)
HOURS = 30 * 24


def _running(asset_id: str, t: dt.datetime) -> int:
    if asset_id in ("GB-KP250-04", "CC-KP250X-03"):          # standby pumps: weekly test run only
        return 1 if (t.weekday() == 2 and 10 <= t.hour < 12) else 0
    if asset_id == "HF-KP100-04":                              # washdown: two shifts
        return 1 if 6 <= t.hour < 22 else 0
    if asset_id == "HF-KP250-01":                              # CIP: runs 4 h per day
        return 1 if 1 <= t.hour < 5 else 0
    return 1


def build(rng: random.Random, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "assets.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["asset_id", "site", "customer_id", "model", "serial_number", "install_date", "criticality",
                    "foundation", "rated_current_a", "bep_flow_m3h", "notes"])
        w.writerows(ASSETS)

    rows = []
    for asset in ASSETS:
        asset_id, model, foundation, rated, bep = asset[0], asset[3], asset[7], asset[8], asset[9]
        base_vib = {"rigid": 1.5, "flexible": 2.1}[foundation] + rng.uniform(-0.2, 0.2)
        base_temp = rng.uniform(58, 66)
        base_press = {"KP-400-S": 3.8, "KP-250-S": 5.1, "KP-250-X": 4.6, "KP-100-S": 2.9, "KP-600-M": 18.5}[model]
        for h in range(HOURS):
            t = START + dt.timedelta(hours=h)
            run = _running(asset_id, t)
            days = h / 24
            load = 0.82 + 0.1 * math.sin(2 * math.pi * (t.hour - 6) / 24)        # daily demand cycle
            vib = base_vib * (0.95 + 0.1 * load) + rng.gauss(0, 0.08)
            temp = base_temp + 4 * load + rng.gauss(0, 0.6)
            press = base_press * (1 + rng.gauss(0, 0.012))
            flow = bep * load * (1 + rng.gauss(0, 0.02))
            current = rated * (0.72 + 0.18 * load) * (1 + rng.gauss(0, 0.01))

            if asset_id == "HF-KP250-03":                 # bearing wear, accelerating
                wear = max(0.0, days - 8) / 22             # starts ~2026-08-24
                vib += 3.5 * wear ** 1.6
                temp += 27 * wear ** 1.5
            if asset_id == "GB-KP400-02" and 1 <= t.hour < 5:   # cavitation at low reservoir level
                vib += rng.uniform(1.2, 4.2)
                press *= 1 + rng.uniform(-0.12, 0.12)
                flow *= rng.uniform(0.78, 0.9)
            if asset_id == "GB-KP250-03" and t >= dt.datetime(2026, 8, 28, 14):   # misalignment after coupling job
                vib += 1.7
                temp += 3
            if asset_id == "CC-KP600-01":                 # drifting right of BEP -> overload
                drift = min(days / 30, 1.0)
                flow *= 1 + 0.22 * drift
                current *= 1 + 0.26 * drift
            if asset_id == "CC-KP100-04" and dt.datetime(2026, 9, 8, 6) <= t < dt.datetime(2026, 9, 10, 14):
                vib = 0.0                                  # sensor flat-line

            if not run:
                vib, flow, current = 0.0, 0.0, 0.0
                press = 0.3 + rng.uniform(0, 0.1)
                temp = 24 + rng.uniform(-1, 1)
            if rng.random() < 0.0008 and run:              # rare single-sample electrical noise spike
                vib = rng.uniform(55, 80)
            rows.append([t.strftime("%Y-%m-%dT%H:00:00Z"), asset_id, run, round(max(vib, 0), 2), round(temp, 1),
                         round(max(press, 0), 2), round(max(flow, 0), 1), round(max(current, 0), 1)])

    with (out_dir / "telemetry.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "asset_id", "running", "vibration_mm_s", "bearing_temp_c", "discharge_pressure_bar",
                    "flow_m3h", "motor_current_a"])
        w.writerows(rows)

    work_orders = [
        ("WO-24011", "GB-KP250-03", "2025-09-14", "PM", "Regrease bearings (15 g LUB-EP2 each), check alignment: OK", "LUB-EP2 x1", "R. Silva", 1.0),
        ("WO-24088", "HF-KP250-03", "2025-10-02", "PM", "Annual inspection; alignment within tolerance", "", "K. Osei", 2.0),
        ("WO-24130", "CC-KP600-01", "2025-11-19", "CM", "Replaced mechanical seal after leakage at DE seal", "MS-400 x1", "D. Petrov", 7.5),
        ("WO-24177", "GB-KP400-01", "2026-01-15", "PM", "Post-commissioning check at 500 h: realigned (0.07 mm offset corrected)", "", "R. Silva", 3.0),
        ("WO-24210", "HF-KP250-03", "2026-02-11", "PM", "Regrease bearings 15 g each", "LUB-EP2 x1", "K. Osei", 0.5),
        ("WO-24261", "CC-KP100-04", "2026-03-05", "CM", "Replaced VS-10 vibration sensor (cable damaged by rodent)", "VS-10 x1", "D. Petrov", 1.0),
        ("WO-24302", "GB-KP400-02", "2026-04-22", "CM", "Night-time noise complaint from operators; suction strainer cleaned, noise persisted intermittently", "", "R. Silva", 4.0),
        ("WO-24355", "HF-KP250-03", "2026-06-02", "PM", "Regrease bearings 15 g each; noted slight rise in bearing temperature (68 C)", "LUB-EP2 x1", "K. Osei", 0.5),
        ("WO-24390", "CC-KP250X-02", "2026-06-10", "PM", "ATEX 2-week inspection after commissioning: OK", "", "D. Petrov", 2.0),
        ("WO-24411", "GB-KP250-04", "2026-07-08", "PM", "Standby pump test run, OK", "", "R. Silva", 0.5),
        ("WO-24466", "CC-KP600-01", "2026-08-03", "PM", "Operations increased boiler feed demand; pump now running at higher flow", "", "D. Petrov", 0.0),
        ("WO-24490", "GB-KP250-03", "2026-08-28", "CM", "Coupling elastomer element worn; replaced CPL-250 coupling. Alignment check skipped - laser tool out for calibration", "CPL-250 x1", "T. Brooks", 3.0),
        ("WO-24502", "HF-KP250-03", "2026-09-05", "CM", "Operator reports hum from pump; vibration trend reviewed remotely, bearing wear suspected - parts requested", "", "K. Osei", 0.0),
        ("WO-24517", "GB-KP400-02", "2026-09-09", "CM", "Pitting observed on impeller eye during borescope inspection", "", "R. Silva", 2.5),
    ]
    with (out_dir / "work_orders.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["work_order_id", "asset_id", "date", "type", "description", "parts_used", "technician",
                    "downtime_hours"])
        w.writerows(work_orders)

    truth = {
        "HF-KP250-03": {"issue": "bearing_wear", "evidence": "vibration and bearing temperature rising together; "
                        "crosses 4.5 mm/s alarm around 2026-09-12", "action": "replace BRG-6309 bearings; check lubrication"},
        "GB-KP400-02": {"issue": "cavitation", "evidence": "nightly 01:00-05:00 erratic vibration, pressure "
                        "fluctuation > 8%, flow drop; impeller-eye pitting (WO-24517)",
                        "action": "raise minimum reservoir level / check NPSHa; clean strainer; keep flow above 380 m3/h"},
        "CC-KP100-04": {"issue": "sensor_fault", "evidence": "vibration exactly 0.0 while running=1 from 2026-09-08 06:00 "
                        "to 2026-09-10 14:00", "action": "replace/repair VS-10 sensor"},
        "CC-KP600-01": {"issue": "overload_right_of_bep", "evidence": "flow drifting to ~120% BEP and motor current "
                        "~20-26% above normal after demand increase (WO-24466)", "action": "throttle to duty point or resize"},
        "GB-KP250-03": {"issue": "misalignment", "evidence": "step increase of ~1.7 mm/s after coupling replacement "
                        "2026-08-28 without alignment check (WO-24490)", "action": "laser alignment (SVC-ALIGN)"},
    }
    (out_dir / "ground_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")

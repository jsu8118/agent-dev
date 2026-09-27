# Vibration & Condition Monitoring Guide for Kestrel Pumps

Document CM-GUIDE-01, revision A (2025-09). For reliability engineers and field technicians.

## 1. What we measure

* **RMS velocity (mm/s), 10–1000 Hz, on the bearing housings** — the standard severity measure for rotating machines.
* **Bearing housing temperature (°C)**.
* Process data: discharge pressure (bar), flow (m³/h), motor current (A).

Kestrel VS-10 vibration sensors report RMS velocity every minute; the plant historian stores hourly averages.

## 2. Severity zones (simplified from ISO 10816-3 / ISO 20816-3)

| Machine class | Foundation | Zone A/B (good) | Zone B/C (alert) | Zone C/D (alarm) |
|---|---|---|---|---|
| 15–75 kW (e.g. KP-100, KP-250) | Rigid | 1.4 | **2.8** | **4.5** |
| 15–300 kW (e.g. KP-400, KP-600) | Flexible | 2.3 | **3.5** (Kestrel alert) | **7.1** |

Zone D values mean damage is likely: plan an immediate shutdown. Product-specific limits in each IOM manual take precedence over this table.

> These thresholds are simplified for training purposes. Always use the limits in the product IOM manual.

## 3. Reading trends (more important than single values)

* An increase of **more than 25% per week** in RMS velocity is suspicious even inside Zone B.
* **Bearing wear:** vibration and bearing temperature rise *together* over days to weeks. The rate usually accelerates near the end of life.
* **Cavitation:** vibration becomes noisy/erratic and correlates with **discharge-pressure fluctuation** and reduced flow; it often appears at specific times (e.g., low tank level at night, high flow demand).
* **Misalignment:** step change after maintenance (coupling or motor work), high 2× component.
* **Imbalance:** step change after impeller work or damage, high 1× component.
* **Motor/electrical overload:** motor current rises while flow rises beyond the duty point; bearing temperature may stay normal.

## 4. Sensor faults (do not treat as machine faults)

* **Flat-line at exactly 0.0 mm/s** while the pump is running (current > 0): sensor or cable failure.
* Isolated spikes above 50 mm/s lasting a single sample: electrical noise.
* Readings frozen at the same non-zero value for more than 6 hours: stale data from the gateway.

When a sensor fault is suspected, create a corrective work order for the sensor (VS-10) — and do not let the missing data hide a real machine problem: check the process data.

## 5. Response procedure

| Condition | Action | Target |
|---|---|---|
| Zone B/C crossed (alert) | Create a corrective work order; increase monitoring to daily; check lubrication and alignment | within 1 week |
| Zone C/D crossed (alarm) | Notify the shift supervisor; plan a controlled shutdown; prepare spare bearings/seal | within 24 hours |
| Trend > 25%/week inside Zone B | Investigate root cause; schedule inspection | within 2 weeks |
| Sensor fault | Replace/repair sensor; verify with a handheld meter | within 1 week |

## 6. Typical root causes by frequency (Kestrel fleet data 2023–2025)

| Root cause | Share of vibration work orders |
|---|---|
| Bearing wear / lubrication | 34% |
| Misalignment | 22% |
| Cavitation / suction problems | 17% |
| Operation far from BEP | 11% |
| Looseness / foundation | 9% |
| Impeller damage / imbalance | 7% |

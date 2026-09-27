# KP-250 End-Suction Centrifugal Pump — Installation, Operation & Maintenance Manual

Document IOM-KP250, revision C (2026-01). Applies to KP-250-S (standard) and KP-250-X (ATEX explosion-proof). Where KP-250-X differs, it is marked **[X]**.

## 1. Safety

1.1 Read this manual before installing, operating, or servicing the pump. Only trained personnel may work on the pump.

1.2 Before any maintenance: stop the pump, **isolate and lock out the motor supply (lockout/tagout)**, close suction and discharge valves, and drain the casing. The casing may contain hot or hazardous liquid under pressure.

1.3 Never operate the pump **dry** (without liquid). The mechanical seal is lubricated by the pumped liquid; dry running destroys the seal faces within **30 seconds** and voids the warranty.

1.4 Never operate against a closed discharge valve for more than **30 seconds**; the liquid overheats and may flash to vapor.

1.5 **[X]** KP-250-X units are certified for Zone 1 hazardous areas. Any fault on a KP-250-X in a hazardous area must be reported to Kestrel immediately (SOP-SUP-007) and the unit must not be restarted until inspected. Only Kestrel-certified technicians may open the motor terminal box.

## 2. Specifications

| Parameter | Value |
|---|---|
| Flow range | 60 – 320 m³/h |
| Best efficiency point (BEP) | 210 m³/h at 52 m head |
| Maximum head | 80 m |
| Motor | 22 kW, 4-pole, 1,480 rpm (50 Hz) / 1,780 rpm (60 Hz) |
| Maximum liquid temperature | 120 °C (standard seal MS-250) |
| Maximum working pressure | 16 bar |
| Mechanical seal | MS-250 cartridge seal kit (silicon carbide / carbon faces, EPDM elastomers) |
| Bearings | Pump end: BRG-6309 deep groove ball bearing; drive end: BRG-6309 |
| Impeller | IMP-250-A (bronze, standard) or IMP-250-D (duplex stainless, for seawater and mild chemicals) |
| NPSH required (NPSHr) at BEP | 3.2 m |
| Weight (pump + motor on baseplate) | 385 kg |

## 3. Installation

3.1 **Foundation.** Mount the baseplate on a rigid concrete foundation at least 1.5 × the weight of the pump unit. Grout the baseplate after leveling (level within 0.2 mm/m).

3.2 **Alignment.** Align pump and motor shafts after grouting and again after connecting the pipes. Tolerances:

| Check | Maximum deviation |
|---|---|
| Parallel (radial) offset | 0.05 mm |
| Angular misalignment | 0.05 mm per 100 mm of coupling diameter |

Re-check alignment after the first **500 operating hours**, then annually. Kestrel offers laser alignment (SVC-ALIGN).

3.3 **Suction piping.** Keep the suction line as short and straight as possible: at least **5 × pipe diameter** of straight pipe before the pump inlet. Use an eccentric reducer with the **flat side up** to avoid air pockets. Install a suction strainer (FLT-SUC-100) during commissioning and check it after the first 24 hours.

3.4 **NPSH margin.** The NPSH available (NPSHa) at the site must exceed NPSHr by at least **1.0 m or 30% (whichever is greater)** across the operating range. Insufficient margin causes cavitation (see §7.2).

3.5 **Discharge piping.** Support pipes independently; the pump flanges must not carry pipe loads (pipe strain causes misalignment and vibration). Install a check valve and an isolation valve on the discharge.

## 4. Commissioning

1. Flush the piping before connecting the pump.
2. **Prime** the pump: fill the casing and suction line completely with liquid and vent all air.
3. Jog the motor briefly to check the **direction of rotation** (arrow on the casing). Wrong rotation gives low flow and noise.
4. Start with the discharge valve **almost closed**, then open it slowly within 30 seconds to reach the duty point.
5. Check for leaks, vibration, bearing temperature, and motor current in the first hour.
6. **Minimum continuous flow:** 25% of BEP flow (≈ 53 m³/h). Running below this for long periods causes recirculation, heating, and vibration.

## 5. Operating limits

| Parameter | Normal | Alert (investigate within 1 week) | Alarm / shutdown |
|---|---|---|---|
| Bearing housing vibration (RMS velocity, 10–1000 Hz) | ≤ **2.8 mm/s** | 2.8 – 4.5 mm/s | > **4.5 mm/s** |
| Bearing housing temperature | ≤ 80 °C | 80 – 95 °C | > **95 °C** |
| Motor current | ≤ rated (41 A at 400 V) | 100 – 110% rated | > 110% rated |
| Discharge pressure fluctuation | ± 3% | ± 3 – 8% | > ± 8% (cavitation likely) |

A steady **upward trend** in vibration together with rising bearing temperature over days or weeks is the classic signature of **bearing wear**; plan a bearing replacement before the alarm limit is reached.

## 6. Maintenance schedule

| Interval | Task |
|---|---|
| Daily | Check for leaks, unusual noise, and discharge pressure |
| Weekly | Record vibration and bearing temperature (or review condition-monitoring data) |
| Every **2,000 operating hours** | Regrease both bearings with **15 g of LUB-EP2** each. Do not over-grease: excess grease raises bearing temperature |
| Every 4,000 hours or annually | Check alignment; inspect coupling (CPL-250) elements; inspect mechanical seal for leakage |
| Every 16,000 hours | Replace bearings (BRG-6309 × 2); replace mechanical seal kit (MS-250); inspect impeller and wear rings |
| After any dry-run event | Replace the mechanical seal kit (MS-250) before restarting |

## 7. Troubleshooting

### 7.1 Low flow or low head

| Probable cause | Corrective action |
|---|---|
| Wrong direction of rotation | Swap two motor phases (or correct the VFD output phase order) |
| Air in suction line / pump not primed | Vent and re-prime; check suction joints for air leaks |
| Clogged impeller or suction strainer | Clean the strainer (FLT-SUC-100) and impeller |
| Worn impeller wear rings | Replace wear rings; clearance must not exceed 0.6 mm |
| Speed too low (VFD) | Check the controller's maximum frequency setting (KC-1 parameter P03) |

### 7.2 Cavitation

**Symptoms:** crackling noise "like gravel passing through the pump", fluctuating discharge pressure, reduced flow, vibration at high frequency, and pitting on the impeller eye.

| Probable cause | Corrective action |
|---|---|
| Clogged suction strainer | Clean FLT-SUC-100; check differential pressure across the strainer |
| Suction valve partly closed | Open the suction valve fully (never throttle the suction side) |
| Liquid temperature too high (vapor pressure) | Reduce temperature or increase suction head |
| Low level in the suction tank | Raise the minimum tank level |
| Operating far right of BEP (flow too high) | Throttle the discharge or reduce speed to move toward BEP |

Cavitation damage caused by insufficient NPSHa at the site is excluded from warranty (Policy WAR-001 §3.2).

### 7.3 High vibration

| Probable cause | Typical signature | Corrective action |
|---|---|---|
| Shaft misalignment | High 2× running speed, axial vibration | Re-align (§3.2) |
| Worn bearings | Rising trend + rising bearing temperature; bearing defect frequencies | Replace BRG-6309 bearings; check lubrication |
| Impeller imbalance / damage | High 1× running speed | Inspect, balance or replace impeller |
| Cavitation | Broadband high-frequency noise, pressure fluctuation | See §7.2 |
| Loose foundation bolts / soft foot | Vibration varies with bolt torque | Re-torque to 210 N·m; shim |
| Pipe strain | Vibration changes when pipes are unbolted | Correct pipe supports |
| Operation far from BEP | Vibration drops when flow approaches BEP | Adjust duty point |

### 7.4 Mechanical seal leakage

A mechanical seal in good condition shows **no visible leakage**. Any steady drip after the first 4 hours of run-in indicates a problem.

| Probable cause | Corrective action |
|---|---|
| Dry running (heat-checked faces, burned elastomers) | Replace MS-250; find and fix the cause (priming, low suction level); dry-run damage is not covered by warranty |
| Abrasive solids in the liquid | Use the flushed seal option; install a strainer |
| Thermal shock (cold liquid on a hot pump) | Warm up gradually |
| Incorrect installation of the seal | Reinstall per the seal kit instructions (seal setting dimension 42.5 mm) |
| Elastomer not compatible with the liquid | Check the compatibility chart; use FKM elastomers for oils and many solvents |

If the leaking liquid is hazardous, hot, or chemical: **stop the pump, isolate it, and escalate per SOP-SUP-007.**

### 7.5 Bearing overheating

| Probable cause | Corrective action |
|---|---|
| **Over-greasing** (most common after maintenance) | Remove excess grease; run for 2 hours with the vent plug removed |
| Under-lubrication | Regrease per §6 |
| Misalignment or pipe strain | Correct (§3.2, §3.5) |
| Wrong bearing clearance (C3 required) | Replace with correct bearing |

### 7.6 Motor overload / high current

| Probable cause | Corrective action |
|---|---|
| Operating at high flow beyond the end of the curve | Throttle discharge to the duty point |
| Liquid density or viscosity higher than specified | Check the fluid; resize motor if needed |
| Impeller rubbing | Check for foreign objects and bearing wear |
| Supply voltage imbalance > 2% | Check the electrical supply |

## 8. Spare parts

| SKU | Description | Recommended stock per installed pump |
|---|---|---|
| MS-250 | Mechanical seal cartridge kit | 1 |
| BRG-6309 | Deep groove ball bearing, C3 | 2 |
| IMP-250-A | Impeller, bronze | 0 (1 per 5 pumps) |
| IMP-250-D | Impeller, duplex stainless steel | 0 (1 per 5 pumps) |
| CPL-250 | Coupling with elastomer element | 1 element set |
| GSK-KIT-250 | Gasket kit (casing, cover, flanges) | 1 |
| LUB-EP2 | Bearing grease cartridge, 400 g | 2 |
| FLT-SUC-100 | Suction strainer, 100 mesh | 1 |

## 9. Tightening torques

| Joint | Torque |
|---|---|
| Casing cover bolts M16 | 150 N·m |
| Baseplate foundation bolts M20 | 210 N·m |
| Impeller nut M24 | 180 N·m |
| Coupling hub set screws | 45 N·m |

## 10. Warranty

See Kestrel Standard Limited Warranty (WAR-001): pumps are covered for 24 months from ship date; wear parts for 6 months.

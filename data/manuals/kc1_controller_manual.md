# KC-1 Variable-Frequency Pump Controller — User Manual

Document UM-KC1, revision D (2026-02). Firmware 4.x. The KC-2 smart controller uses the same fault codes (KC-2 adds Modbus/TCP and cloud telemetry).

## 1. Safety

* Dangerous voltage remains on the DC bus for **5 minutes** after disconnecting power. Wait 5 minutes and verify with a meter before opening the enclosure.
* Faults marked **SAFETY** below require the equipment to be isolated and inspected before any reset beyond the first.

## 2. Ratings

| Parameter | KC-1 |
|---|---|
| Supply | 380–480 V AC, 3-phase, ±10% |
| Motor power | up to 22 kW |
| Output current | 45 A continuous, 150% for 60 s |
| Ambient temperature | −10 to +40 °C (derate 2% per °C above 40 °C, max 50 °C) |
| Enclosure | IP55 |
| Cooling fan | Replaceable module KC-1-FAN (expected life 30,000 h) |

## 3. Key parameters

| Code | Parameter | Default | Notes |
|---|---|---|---|
| P01 | Motor rated current | 41 A | Set from the motor nameplate |
| P02 | Minimum frequency | 25 Hz | Keeps flow above the pump's minimum continuous flow |
| P03 | Maximum frequency | 50 Hz | 60 Hz for 60 Hz motors |
| P04 | Acceleration time | 10 s | 0 → max frequency |
| P05 | Deceleration time | 15 s | Too short causes F03 |
| P10 | PID setpoint | 4.0 bar | Pressure control mode |
| P11 | PID proportional gain | 1.2 | |
| P12 | PID integral time | 3.0 s | |
| P20 | Dry-run protection | On | Uses power-at-speed curve |
| P21 | Dry-run delay | 10 s | |
| P30 | Modbus address | 1 | |
| P31 | Modbus timeout | 5 s | 0 = disabled |

## 4. Fault codes

| Code | Name | Probable causes | Remedy |
|---|---|---|---|
| F01 | Overcurrent during acceleration | Acceleration time too short; mechanically blocked pump | Increase P04; check the pump turns freely |
| F02 | Overcurrent at constant speed | Sudden load change; impeller rubbing | Check the pump and the process |
| F03 | DC bus overvoltage | Deceleration too fast (regeneration) | Increase P05; fit a braking resistor |
| F04 | Undervoltage | Supply dip or phase problem | Check the supply and fuses |
| F05 | Drive overtemperature | Blocked air inlet, **failed cooling fan**, ambient > 40 °C, dirty heatsink | Clean the heatsink and filters; check the fan (replace **KC-1-FAN** if it does not run); reduce ambient temperature |
| F06 | Motor overload (I²t) | Pump running beyond the end of its curve; P01 set too low | Check the duty point and P01 |
| F07 | Ground fault — **SAFETY** | Insulation failure in motor or cable; water ingress | **Isolate. Do not reset more than once.** Megger-test motor and cable; escalate per SOP-SUP-007 if it recurs |
| F08 | Input phase loss | Blown fuse, loose supply terminal | Check the supply |
| F09 | Output phase loss | Loose motor terminal, damaged cable | Check the motor cable and terminals |
| F10 | **Dry-run protection** | No liquid at the pump inlet; closed suction valve; empty tank | Check liquid supply and suction valve; **inspect the mechanical seal before restarting** (dry running damages it) |
| F11 | Sensor signal loss | 4–20 mA signal below 3.6 mA; broken wire | Check sensor wiring and supply (PT-40 transmitter) |
| F12 | PID feedback out of range | Wrong sensor scaling | Check the sensor range parameters |
| F13 | Communication timeout | Modbus master stopped polling | Check the network; P31 |
| F14 | Safe torque off (STO) active | STO circuit open (emergency stop) | Close the STO circuit, then reset |
| F15 | Parameter memory error | Corrupted EEPROM | Restore parameters; contact support if it recurs |
| F16 | Cooling fan failure | Fan blocked or worn out | Replace **KC-1-FAN**; the drive derates to 50% until fixed |
| F17 | Pipe burst detection — **SAFETY** | Sudden pressure drop with flow increase | **Stop the system, isolate, inspect the piping**; escalate per SOP-SUP-007 |
| F18 | Over-pressure | Discharge valve closed; setpoint too high | Check valves and P10 |
| F19 | Motor thermistor trip | Motor overheating; blocked motor fan | Check motor cooling and load |
| F20 | Internal error | Hardware fault | Note the error sub-code and contact Kestrel support |

## 5. Resetting faults

1. Remove the cause.
2. Press **STOP/RESET** for 2 seconds, or send a reset over Modbus (register 40010 = 1).
3. SAFETY faults (F07, F17): only **one** reset is allowed before inspection; a second occurrence locks the drive until a technician enters code 7171.

## 6. Diagnostic log

The controller stores the last 32 faults with timestamp, motor current, DC bus voltage, heatsink temperature, and output frequency. Export via the keypad (menu D5) or Modbus registers 41000–41255.

## 7. Recurring F05 on hot days — application note AN-KC1-05

Enclosures installed in direct sunlight or in plant rooms without ventilation often exceed 40 °C in summer. In a field study of 212 KC-1 units, **68%** of F05 events were resolved by cleaning the heatsink and replacing a worn **KC-1-FAN**; 21% required improving room ventilation; 11% required moving the enclosure out of direct sun. Fans older than 30,000 hours should be replaced proactively.

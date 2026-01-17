# OpenFAN Micro — Home Assistant Integration

> **Status:** `v2.0.0`  
> Major release with **multi-fan support**, **per-fan aliases**, **temperature profiles**, and **per-fan temperature control**.

Custom integration for [OpenFAN Micro](https://github.com/SasaKaranovic/OpenFan-Micro) and OpenFAN (full app) devices.  
Adds LED and 5V/12V switches, stall detection, diagnostics, and **temperature-based fan control** with smoothing and **calibration-gated minimum PWM**.

---

## What's New in v2.0.0

- **Multi-fan support**: Devices with multiple fans (up to 10) now create separate entities per fan
- **Per-fan aliases**: Give each fan a friendly name (e.g., "CPU Fan", "GPU Fan")
- **Temperature profiles**: Built-in profiles (quiet, balanced, aggressive) + custom profile support
- **Per-fan temperature control**: Each fan can have its own temp sensor, curve, and settings
- **Automatic migration**: Existing single-fan configurations are automatically migrated

### Upgrading from v1.x

Your existing configuration will be automatically migrated. Single-fan settings are preserved and moved to the per-fan structure. No action required!

If you experience issues after upgrading:
1. Check **Settings → Devices & Services → OpenFAN Micro → Options**
2. Verify your temperature control settings are intact
3. Download diagnostics and open an issue if problems persist

---

## Features

- **Fan control**: on/off and percentage (0–100%) per fan
- **Multi-fan support**: Automatic detection and entity creation for multi-fan devices
- **Per-fan aliases**: Custom names for each fan without breaking entity IDs
- **RPM sensor** (`sensor.<name>_rpm`) with long-term statistics
- **LED switch** (`switch.<name>_led`) — activity LED on/off
- **12V mode switch** (`switch.<name>_12v_mode`) — on=12V, off=5V
- **Availability gating** — marks device `unavailable` only after N consecutive failures
- **Stall detection** — binary sensor + persistent notification + HA event (per fan)
- **Diagnostics export** — from the integration card
- **Temperature-based control** (piecewise-linear curve) with:
  - moving-average **integration window**
  - **minimum interval** between speed changes
  - **deadband** to avoid flapping
  - **clamped by calibrated minimum PWM** (never drives below min, except when fully off)
- **Temperature profiles**: Built-in (quiet, balanced, aggressive) and custom profiles

---

## Requirements

- OpenFAN Micro or OpenFAN (full app) firmware providing:
  - Fan status: `/api/v0/fan/status` (multi-fan) or `/api/v0/fan/0/status` (single-fan)
  - Fan set: `/api/v0/fan/{index}/set?value=…` or `/api/v0/fan/{index}/pwm?value=…`
  - Device status: `/api/v0/openfan/status` (fields: `act_led_enabled`, `fan_is_12v`)
  - LED & voltage: `/api/v0/led/(enable|disable)`, `/api/v0/fan/voltage/(high|low)?confirm=true`
- Home Assistant **2024.12 or newer** (tested on 2025.x)

---

## Installation

### Option A — HACS (Custom Repository)

1. **HACS → Integrations →** ⋮ **Custom repositories**
2. Add: `https://github.com/bitlisz1/hass-openfan-micro` (Category: **Integration**)
3. Find **OpenFAN Micro** → **Download**
4. **Restart Home Assistant**
5. **Settings → Devices & Services → Add Integration → OpenFAN Micro**, enter the device IP and name

### Option B — Manual

1. Copy `custom_components/openfan_micro/` to your HA config directory
2. **Restart Home Assistant**
3. Add the integration: **Settings → Devices & Services → Add Integration → OpenFAN Micro**

---

## Entities Created

### Single-fan device (OpenFAN Micro)
- `fan.<name>` — main fan entity
- `sensor.<name>_rpm` — RPM sensor
- `switch.<name>_led` — activity LED
- `switch.<name>_12v_mode` — 12V mode
- `binary_sensor.<name>_stall` — stall detection

### Multi-fan device (OpenFAN Full App)
For a device with N fans:
- `fan.<name>` — Fan 1 (index 0, legacy-compatible)
- `fan.<name>_fan_2` — Fan 2
- `fan.<name>_fan_3` — Fan 3
- ... and so on

Each fan also gets its own RPM sensor and stall detector:
- `sensor.<name>_rpm`, `sensor.<name>_fan_2_rpm`, etc.
- `binary_sensor.<name>_stall`, `binary_sensor.<name>_fan_2_stall`, etc.

### Fan Entity Attributes

Each fan entity exposes:
- `fan_index` — index of this fan (0-9)
- `min_pwm`, `min_pwm_calibrated` — calibration state
- `profile` — currently applied profile (if any)
- `temp_control_active` — whether temp control is running
- `temp_entity`, `temp_curve` — temperature control configuration
- `temp_avg`, `last_target_pwm`, `last_applied_pwm` — runtime state
- `temp_update_min_interval`, `temp_deadband_pct` — timing settings

---

## Configuration

### Per-Fan Aliases

Give your fans friendly names via **Options**:

1. **Settings → Devices & Services → OpenFAN Micro → Options**
2. Configure global settings, then click Submit
3. Select a fan to configure
4. Enter an **Alias** (e.g., "CPU Fan")
5. Optionally configure another fan

Aliases change the entity's friendly name but preserve the unique ID for automations.

### Calibrating Minimum PWM

Run once per fan to find the minimum PWM that reliably spins the fan:

```yaml
action: openfan_micro.calibrate_min
data:
  entity_id: fan.your_fan_entity
  from_pct: 5
  to_pct: 40
  step: 2
  rpm_threshold: 120
  margin: 5
```

**Tip:** Re-calibrate after switching 5V/12V mode.

---

## Temperature Control

### Using Profiles (Recommended)

Apply a built-in profile to quickly configure temperature control:

```yaml
action: openfan_micro.apply_profile
data:
  entity_id: fan.cpu_fan
  profile: balanced
```

**Built-in Profiles:**

| Profile | Curve | Integration | Min Interval | Deadband |
|---------|-------|-------------|--------------|----------|
| `quiet` | 45=25, 60=55, 75=100 | 60s | 15s | 5% |
| `balanced` | 45=35, 60=60, 70=100 | 30s | 10s | 3% |
| `aggressive` | 45=40, 55=70, 65=100 | 15s | 5s | 2% |

### Custom Configuration

Configure via Options or services:

```yaml
action: openfan_micro.set_temp_control
data:
  entity_id: fan.cpu_fan
  temp_entity: sensor.cpu_temperature
  temp_curve: "45=35, 60=60, 70=100"
  temp_integrate_seconds: 30
  temp_update_min_interval: 10
  temp_deadband_pct: 3
```

### Saving Custom Profiles

Save your current settings as a reusable profile:

```yaml
action: openfan_micro.save_profile
data:
  entity_id: fan.cpu_fan
  profile: my_silent_profile
```

### Disabling Temperature Control

```yaml
action: openfan_micro.clear_temp_control
data:
  entity_id: fan.cpu_fan
```

---

## All Services

| Service | Description |
|---------|-------------|
| `openfan_micro.led_set` | Enable/disable activity LED |
| `openfan_micro.set_voltage` | Switch 5V/12V supply |
| `openfan_micro.calibrate_min` | Find minimum PWM for reliable spin |
| `openfan_micro.set_temp_control` | Configure temperature-based control |
| `openfan_micro.clear_temp_control` | Disable temperature control |
| `openfan_micro.apply_profile` | Apply a named profile |
| `openfan_micro.save_profile` | Save current settings as custom profile |
| `openfan_micro.list_profiles` | List available profiles |

---

## Lovelace Examples

### Single Fan Card

```yaml
type: vertical-stack
cards:
  - type: entities
    title: CPU Fan
    entities:
      - entity: fan.cpu_fan
        name: Fan
      - entity: sensor.cpu_fan_rpm
        name: RPM
      - entity: switch.cpu_fan_led
        name: LED
      - entity: switch.cpu_fan_12v_mode
        name: 12V Mode
  - type: markdown
    content: |
      **Control State**
      - Profile: **{{ state_attr('fan.cpu_fan','profile') or 'Custom' }}**
      - Calibrated min: **{{ state_attr('fan.cpu_fan','min_pwm') }}%**
      - Temp control: **{{ state_attr('fan.cpu_fan','temp_control_active') }}**
      - Temp average: **{{ (state_attr('fan.cpu_fan','temp_avg') or 0) | round(1) }}°C**
      - Target PWM: **{{ state_attr('fan.cpu_fan','last_target_pwm') }}%**
```

### Multi-Fan Dashboard

```yaml
type: vertical-stack
cards:
  - type: entities
    title: OpenFAN Controller
    entities:
      - entity: fan.openfan
        name: CPU Fan
      - entity: fan.openfan_fan_2
        name: GPU Fan
      - entity: fan.openfan_fan_3
        name: Case Fan
  - type: horizontal-stack
    cards:
      - type: gauge
        entity: sensor.openfan_rpm
        name: CPU
        min: 0
        max: 2500
      - type: gauge
        entity: sensor.openfan_fan_2_rpm
        name: GPU
        min: 0
        max: 2500
      - type: gauge
        entity: sensor.openfan_fan_3_rpm
        name: Case
        min: 0
        max: 2500
```

---

## Stall Detection

The binary sensor turns **on** if PWM > min_pwm and RPM == 0 for N consecutive polls (`stall_consecutive` option, default 3).

When detected:
- Event `openfan_micro_stall` is fired (payload includes `host` and `fan_index`)
- Persistent notification is created in HA

---

## Diagnostics

**Settings → Devices & Services → Integrations → OpenFAN Micro → ⋮ → Download diagnostics**

Includes:
- Config entry options (global and per-fan)
- Coordinator data (rpm, pwm, LED, 12V, stall per fan)
- Per-fan controller states (profile, temp average, target/applied PWM)
- Available profiles (built-in and custom)

---

## Troubleshooting

**Options button missing:** Use the Actions services; refresh browser (Ctrl+F5) or restart HA.

**Fan sticks near minimum:** Check `min_pwm_calibrated: true`. Verify `temp_avg`, `last_target_pwm`. Try reducing `temp_deadband_pct`.

**LED/12V toggles jump back:** Ensure firmware provides `/api/v0/openfan/status`.

**Multi-fan entities not created:** Check that your device reports fan count via `/api/v0/fan/status`. Single-fan fallback is used if detection fails.

**Migration issues:** Download diagnostics and check that `options.fans` structure exists.

### Enable Debug Logging

```yaml
logger:
  default: info
  logs:
    custom_components.openfan_micro: debug
```

---

## Contributing

Issues and PRs are welcome. When reporting bugs, please attach:
- Diagnostics export
- Debug logs

---

## Credits

- OpenFAN Micro hardware & firmware: [Sasa Karanovic](https://github.com/SasaKaranovic/OpenFan-Micro)
- Original HA integration: BeryJu
- This fork: Multi-fan support, profiles, aliases, per-fan temp control, and more

---

## License

See LICENSE in this repository. The integration may include code adapted from the original project; original licenses apply.

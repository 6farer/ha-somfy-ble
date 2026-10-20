# Somfy Bluetooth for Home Assistant

A Home Assistant custom integration that talks to Somfy battery motors over Bluetooth
Low Energy for the jobs their radio protocols don't cover: identifying a motor, jogging,
setting upper and lower limits, changing direction, starting range setup, and reading or
changing motor speed and soft start/stop ramps.

It is meant for calibration with the blind in view. Day-to-day open, close and position
control stays with Zigbee or whatever the motor already uses.

> Not affiliated with or endorsed by Somfy. The Bluetooth protocol is unofficial, built on
> [Melair's research](#credits). Writing limits or settings to a motor is at your own risk.

## Tested hardware

- Somfy Sonesse 28 WF Li-Ion roller shade motors.
- Home Assistant 2026.7 with [ESPHome Bluetooth proxies](https://esphome.io/components/bluetooth_proxy.html)
  (ESP32-S3 and ESP32-C3) in active mode.

Other Somfy Bluetooth motors may work. Reports are welcome in
[issues](https://github.com/6farer/ha-somfy-ble/issues).

## Requirements

- Home Assistant's [Bluetooth integration](https://www.home-assistant.io/integrations/bluetooth/)
  with an adapter or an active ESPHome Bluetooth proxy within a room or two of each motor.
  Motors heard at about −90 dBm or weaker mostly time out connecting.
- Each motor's Bluetooth address and its six-character printed PIN. The PIN is hex, even
  when it is all digits.

## Install

### HACS

1. HACS > ⋮ > Custom repositories, add `https://github.com/6farer/ha-somfy-ble`, category
   Integration.
2. Install **Somfy Bluetooth** and restart Home Assistant.

### Manual

Copy `custom_components/somfy_ble/` to `/config/custom_components/somfy_ble/` and restart
Home Assistant.

## Setup

Settings > Devices & services > Add integration > **Somfy Bluetooth**. Add one entry per
motor with a name, Bluetooth address and PIN. Change them later with Reconfigure.

## Entities

Per motor:

| Platform | Entities |
|---|---|
| Button | Identify, Stop, Jog up, Jog down, Set upper limit, Set lower limit, Start range, Set direction clockwise / counter-clockwise, Interrogate Bluetooth |
| Number | Jog step (motor steps) |
| Sensor | Status, Last result, Last error, Last command ID, Selected source, Last contact age |
| Binary sensor | BLE reachable |

Plus one shared `select.somfy_ble_selected_blind`. Its options are the configured motors and
its `entities` attribute maps each function (`stop`, `jog_up`, …) to the selected motor's
entity ID, so dashboards and scripts can follow the selection without per-motor edits.

Commands connect and authenticate on demand. Stop jumps the queue. Idle sessions close after
60 seconds and the next command reconnects.

## Actions

- `somfy_ble.read_config` (`motor`): reads the motor's configuration file. Each setting
  comes back as its value plus whether it is writable, its range, unit, default and choices.
  Changes nothing on the motor.
- `somfy_ble.write_config` (`motor`, `key`, `value`): writes one speed or ramp setting
  (`NominalSpeed`, `TiltSpeed`, `StartUpRamp`, `StartDownRamp`, `StopUpRamp`,
  `StopDownRamp`, `DisabledSoftRamps`, `DisabledSoftStartRamp`, `DisabledSoftStopRamp`).
  It needs a `read_config` of the same motor in the last 15 minutes, must stay within the
  reported range, and counts as saved only when a re-read confirms it.

## Safety

- The integration only sends the commands listed above and has no firmware-update path.
- Configuration writes are limited to speed and ramp keys and checked against the range
  the motor reports.
- The card asks for confirmation before saving a limit, changing direction or starting
  range setup.

## Calibration card

[`www/somfy-blind-card.js`](www/somfy-blind-card.js) (`custom:somfy-blind-card`) puts the
whole flow in one card: blind picker, Identify, Stop, jog step and jog, limits, range setup,
direction, motor speed and soft start/stop, and a Bluetooth inspection report.

1. Copy it to `/config/www/somfy-blind-card.js`.
2. Settings > Dashboards > ⋮ > Resources, add `/local/somfy-blind-card.js` as a JavaScript
   module.
3. Add a card:

   ```yaml
   type: custom:somfy-blind-card
   ```

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
node --test www/somfy-blind-card.test.mjs
```

## Credits

The Bluetooth protocol this integration speaks was first documented by **Melair**
([mrmelair.com](https://mrmelair.com), [GitHub](https://github.com/melair)), who
reverse engineered Somfy's app to find the PIN authentication and motor commands:

- [“Hacking” the Somfy Sonesse2 Zigbee Motors](https://mrmelair.com/2024/07/10/hacking-the-somfy-sonesse2-zigbee-motors/)
  (July 2024)
- [More BLE for the Somfy Sonesse2 Zigbee Motors](https://mrmelair.com/2024/08/22/more-ble-for-the-somfy-sonesse2-zigbee-motors/)
  (August 2024)

None of this would exist without those write-ups.

## License

[MIT](LICENSE)

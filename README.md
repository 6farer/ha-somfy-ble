# Somfy Bluetooth for Home Assistant

<img src="custom_components/somfy_ble/brand/icon.png" alt="" width="96" align="right">

Set up Somfy Sonesse blinds from Home Assistant over Bluetooth: upper and lower limits,
direction, range setup, motor speed and soft start/stop.

## Why use this

Pairing Somfy's Zigbee motors straight to Home Assistant (Zigbee2MQTT or ZHA) gets you open,
close and position, but locks out Somfy's apps, which only reach motors through a TaHoma hub.
Zigbee doesn't expose the motor's setup, so nothing is left that can set limits or change speed.

I built this because my Zigbee2MQTT blinds' limits drifted over time and the TaHoma app couldn't
reach them. I also use it for per-room speeds: slow bedroom blinds so sunrise doesn't wake me,
fast office blinds for quick adjustments.

This integration does that setup locally, from Home Assistant:

- **No hub, no cloud, no Somfy account.** It talks to the motor directly over Bluetooth, through
  your adapter or an ESPHome proxy.
- **Fix limits and direction yourself**, with the blind in view and confirmation before anything
  is saved.
- **Tune speed and soft start/stop**, which Zigbee doesn't expose at all.
- **Every motor from one card**, with motors found automatically.

Keep using Zigbee for everyday control. This is the setup tool alongside it.

> Not affiliated with or endorsed by Somfy. The Bluetooth protocol is unofficial, built on
> [Melair's research](#credits). Writing limits or settings to a motor is at your own risk.

## Supported models

Bluetooth setup is only in Somfy's newer Zigbee motors. Older RTS-only motors have no
Bluetooth and won't work.

| Motor | Status |
|---|---|
| Sonesse 28 WF Li-Ion Zigbee (reports as "Sonesse2") | ✅ Tested |
| Sonesse 30 WireFree Zigbee | ❔ Untested; same generation, likely |
| Sonesse 40 AC Zigbee (wired) | ❔ Untested |
| Sonesse RTS / WireFree RTS | ❌ No Bluetooth |

Home Assistant only discovers motors that advertise both Somfy's Bluetooth company ID and a name
starting with `Sonesse`. If yours shows up under another name in
[Settings > Bluetooth](https://my.home-assistant.io/redirect/bluetooth/) > **Advertisement
monitor**, you can still add it by hand; please
[open an issue](https://github.com/6farer/ha-somfy-ble/issues) with the model and what
worked, either way, so this table can grow.

Tested with ESPHome Bluetooth proxies on ESP32-S3 and ESP32-C3.

## Quick start

### Before you start

- [ ] **A supported motor.** A Somfy Sonesse Zigbee motor (see [Supported models](#supported-models)).
  It doesn't need to be paired to Zigbee or anything else; Bluetooth works either way.
- [ ] **Each motor's PIN.** 6 characters, 0–9 and A–F. It's on the motor's label, next to
  its EUI and Zigbee install code, which is hidden once the blind is mounted, so ask your
  installer for the list or note it down before installing.
- [ ] **Bluetooth that reaches the blinds.** Your Home Assistant's built-in adapter if it's
  close, otherwise a $5 ESP32 [Bluetooth proxy](docs/bluetooth-proxy.md) within a room or two.
- [ ] **Home Assistant 2026.7 or newer**, with [HACS](https://hacs.xyz/docs/use/) for the
  easiest install ([manual install](#manual-install-without-hacs) works too).

### Steps

1. **Install.** Click the button, then **Download**, then restart Home Assistant.

   [![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=6farer&repository=ha-somfy-ble&category=integration)

   <sub>Without the button: HACS > ⋮ > Custom repositories, add
   `https://github.com/6farer/ha-somfy-ble` as an Integration, then download **Somfy
   Bluetooth**.</sub>

2. **Add your motors.** Home Assistant finds them on its own. Open
   [Settings > Devices & services](https://my.home-assistant.io/redirect/integrations/), and
   under **Discovered** click **Add** on each Somfy Bluetooth motor and type its PIN. Motors
   arrive named by the end of their Bluetooth address (e.g. `Somfy 3A:7F`); keep that name for
   now.

   Nothing discovered? See [Troubleshooting](#troubleshooting).

3. **Add the card.** Edit a dashboard, add a card, and pick **Somfy blind** (or paste
   `type: custom:somfy-blind-card`). It installs with the integration; there's no separate
   download.

4. **Name your blinds.** On the card, pick each motor and press **Identify**: that blind jogs
   briefly. Then rename it after the room with
   [Settings > Devices & services](https://my.home-assistant.io/redirect/integration/?domain=somfy_ble)
   > Somfy Bluetooth > the motor's ⋮ > **Reconfigure**, so `Somfy 3A:7F` becomes `Office Sheer`.
   A motor that jogs also proves its PIN and Bluetooth path work.

## What you get

Each motor becomes a device with:

| Platform | Entities |
|---|---|
| Button | Identify, Stop, Jog up, Jog down, Set upper limit, Set lower limit, Start range, Set direction clockwise / counter-clockwise, Interrogate Bluetooth |
| Number | Jog step (motor steps) |
| Sensor | Status, Last result, Last error, Last command ID, Selected source, Last contact age |
| Binary sensor | BLE reachable |

Plus:

- **The Somfy blind card** (`custom:somfy-blind-card`): blind picker, Identify, Stop, jog,
  limits, range setup, direction, and an Advanced tab for motor speed and soft start/stop.
  It asks before saving a limit, changing direction or starting range setup.
- **`select.somfy_ble_selected_blind`**: one shared picker. Its `entities` attribute maps each
  function (`stop`, `jog_up`, …) to the selected motor's entity, so scripts and dashboards
  follow the selection without per-motor edits.
- **Actions**:
  - `somfy_ble.read_config` (`motor`) reads the motor's settings: each value with whether it's
    writable, its range, unit, default and choices. Changes nothing.
  - `somfy_ble.write_config` (`motor`, `key`, `value`) writes one speed or ramp setting
    (`NominalSpeed`, `TiltSpeed`, `StartUpRamp`, `StartDownRamp`, `StopUpRamp`,
    `StopDownRamp`, `DisabledSoftRamps`, `DisabledSoftStartRamp`, `DisabledSoftStopRamp`). It
    needs a `read_config` of the same motor in the last 15 minutes, must stay within the
    reported range, and only counts as saved when a re-read confirms it.

Commands connect on demand. Stop jumps the queue. Idle connections close after 60 seconds.

## Troubleshooting

- **Motor not discovered.** Check
  [Settings > Bluetooth](https://my.home-assistant.io/redirect/bluetooth/) >
  **Advertisement monitor** for devices named `Sonesse`. If none show up, no adapter or proxy
  can hear the motor; see [Bluetooth proxy](docs/bluetooth-proxy.md). If a motor shows there but isn't
  discovered, add it by hand:
  [Add integration](https://my.home-assistant.io/redirect/config_flow_start/?domain=somfy_ble)
  > **Somfy Bluetooth**, and copy its address from the monitor.
- **`auth_failed`.** Check the PIN. It's 6 characters of hex (0–9, A–F), even when it's all
  digits.
- **Commands time out.** The motor is too far from any proxy. In the advertisement monitor,
  aim for better than about −85 dBm; move or add a proxy.
- **Low battery.** Motors stop answering well before they're empty. Charge and retry.

Change a motor's name, address or PIN later with **Reconfigure** on its entry.

## Safety

- Only the commands listed above are sent. There is no firmware-update path.
- Settings writes are limited to speed and ramp keys and checked against the range the motor
  reports.
- Each motor's address and PIN are stored in its config entry, like any integration's
  credentials (`/config/.storage/core.config_entries`, included in backups). Diagnostics
  downloads leave the PIN out. Change it with **Reconfigure**.

## Manual install (without HACS)

Copy `custom_components/somfy_ble/` from the
[latest release](https://github.com/6farer/ha-somfy-ble/releases/latest) to
`/config/custom_components/somfy_ble/` and restart Home Assistant. The card comes with it.

## Development

```sh
python3.14 -m venv .venv
.venv/bin/pip install -r requirements_test.txt
.venv/bin/python -m pytest
node --test tests/somfy-blind-card.test.mjs
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

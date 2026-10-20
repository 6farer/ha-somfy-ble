# Bluetooth proxy for Somfy Bluetooth

Home Assistant needs Bluetooth within a room or two of each blind. Motors heard at about
−90 dBm or weaker mostly time out. A built-in adapter works if it's close enough. Otherwise,
an ESP32 board ($5–10) running ESPHome as a Bluetooth proxy is the easy fix, and you can add
one per floor.

**Boards:** any ESP32, ESP32-S3 or ESP32-C3. Tested: ESP32-S3-DevKitC-1, ESP32-C3 Super Mini.
Not ESP8266 or ESP32-S2 (no Bluetooth).

**No-code install:**

1. Plug the board into a computer and open
   [esphome.io/projects](https://esphome.io/projects/) in Chrome or Edge.
2. Choose **Bluetooth Proxy** > **Connect**, pick the USB port, install, and enter your Wi-Fi.
3. Home Assistant shows it under
   [Settings > Devices & services](https://my.home-assistant.io/redirect/integrations/) >
   **Discovered**. Click **Add**.

**ESPHome YAML** (if you build your own firmware), the tested settings:

```yaml
esp32:
  board: esp32-s3-devkitc-1   # or esp32-c3-devkitm-1, esp32dev
  framework:
    type: esp-idf

esp32_ble_tracker:
  scan_parameters:
    active: true

bluetooth_proxy:
  active: true                # required: this integration connects to motors
  cache_services: true
  connection_slots: 3
```

Proxies must be **active** (allowed to connect), not passive. Home Assistant picks the
closest one per motor automatically.

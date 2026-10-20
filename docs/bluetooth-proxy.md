# Bluetooth proxy for Somfy Bluetooth

Home Assistant needs Bluetooth within a room or two of each blind, because motors heard at
about −90 dBm or weaker mostly time out. If your Home Assistant's built-in adapter is close
enough, you don't need anything else. Otherwise, an ESP32 board ($5–10) running ESPHome as a
Bluetooth proxy covers the gap, and you can add one per floor if the house is large.

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

The proxy has to be **active**, meaning it's allowed to open connections, because the
integration connects to each motor rather than only listening to it. With several proxies,
Home Assistant uses whichever one hears a motor best.

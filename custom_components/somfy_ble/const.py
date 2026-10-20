"""Constants for the Somfy Bluetooth Home Assistant integration."""

DOMAIN = "somfy_ble"

CONF_NAME = "name"
CONF_ADDRESS = "address"
CONF_PIN = "pin"

MAX_CONTACT_AGE_SECONDS = 7 * 24 * 60 * 60

# Keep each BLE phase bounded independently; these values are intentionally
# short enough for deterministic HA command ownership and tests.
BLE_DISCOVERY_TIMEOUT = 8.0
# Remote ESPHome proxy connections include allocation and GATT service setup;
# eight seconds is too short even with a strong, idle scanner path.
BLE_CONNECT_TIMEOUT = 20.0
BLE_AUTH_TIMEOUT = 5.0
BLE_AUTH_SETTLE_DELAY = 2.0
BLE_COMMAND_TIMEOUT = 3.0
BLE_DISCONNECT_TIMEOUT = 3.0
EXECUTION_CANCEL_TIMEOUT = 3.0

# Release an idle authenticated connection without locking out future commands.
BLE_SESSION_IDLE_TIMEOUT = 60.0

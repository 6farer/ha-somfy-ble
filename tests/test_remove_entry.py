"""Deleting a motor's entry lets Bluetooth discovery offer it again."""

from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.somfy_ble import async_remove_entry
from custom_components.somfy_ble.const import CONF_ADDRESS, DOMAIN


async def test_removing_an_entry_rediscovers_its_address(hass) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ADDRESS: "4c:c2:06:81:ab:c3"})
    with patch(
        "homeassistant.components.bluetooth.async_rediscover_address"
    ) as rediscover:
        await async_remove_entry(hass, entry)
    rediscover.assert_called_once_with(hass, "4C:C2:06:81:AB:C3")

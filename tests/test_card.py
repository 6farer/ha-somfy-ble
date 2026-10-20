"""The integration serves the blind card and loads it on every dashboard."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.somfy_ble import CARD_URL, _async_register_card


async def test_card_is_served_and_added_to_the_frontend(
    hass, enable_custom_integrations: None
) -> None:
    hass.config.components.add("frontend")
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    with patch("homeassistant.components.frontend.add_extra_js_url") as add_url:
        await _async_register_card(hass)

    (config,) = hass.http.async_register_static_paths.call_args.args[0]
    assert config.url_path == CARD_URL
    assert Path(config.path).is_file()
    add_url.assert_called_once()
    assert add_url.call_args.args[1].startswith(f"{CARD_URL}?v=")


async def test_card_is_skipped_without_the_frontend(hass) -> None:
    hass.http = MagicMock(async_register_static_paths=AsyncMock())
    await _async_register_card(hass)
    hass.http.async_register_static_paths.assert_not_called()

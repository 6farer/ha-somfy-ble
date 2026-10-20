import json
from pathlib import Path


def test_manifest_declares_home_assistant_bluetooth_dependency() -> None:
    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/somfy_ble/manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["dependencies"] == ["bluetooth"]
    assert manifest["requirements"] == []


def test_manifest_declares_bluetooth_discovery_matchers() -> None:
    manifest = json.loads(
        (
            Path(__file__).parents[1]
            / "custom_components/somfy_ble/manifest.json"
        ).read_text(encoding="utf-8")
    )

    assert manifest["version"] == "0.2.0"
    # One matcher, so a device needs both Somfy's company ID and a Sonesse name.
    assert manifest["bluetooth"] == [
        {"connectable": True, "local_name": "Sonesse*", "manufacturer_id": 1480}
    ]

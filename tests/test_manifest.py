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

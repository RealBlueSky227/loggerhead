from __future__ import annotations

import pytest

from loggerhead.config import EquipmentProfile, default_config, load_config, save_config, validate_config
from loggerhead.hardware import DiagnosticHalt, EquipmentDriver


def test_default_config_is_valid() -> None:
    validate_config(default_config())


def test_config_round_trips_enums(tmp_path) -> None:
    path = tmp_path / "loggerhead.json"
    save_config(path, default_config())
    loaded = load_config(path)
    assert loaded.equipment[0].driver == EquipmentDriver.MCP23017_RELAY


def test_rejects_unmapped_relay() -> None:
    config = default_config()
    config.equipment.append(EquipmentProfile("bad", "Bad Relay", EquipmentDriver.MCP23017_RELAY, "AC99"))
    with pytest.raises(DiagnosticHalt):
        validate_config(config)


def test_rejects_kasa_outlet_outside_hs300_range() -> None:
    config = default_config()
    config.equipment.append(
        EquipmentProfile("bad", "Bad Outlet", EquipmentDriver.KASA_HS300, "6", kasa_host="192.0.2.10")
    )
    with pytest.raises(DiagnosticHalt):
        validate_config(config)

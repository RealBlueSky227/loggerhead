from __future__ import annotations

import json
import types

import pytest

from loggerhead.hardware import DiagnosticHalt
from loggerhead.onewire import HelperOneWireOverlayClient, parse_dtoverlay_list


def test_parse_dtoverlay_list_extracts_w1_gpio_runtime_overlays() -> None:
    overlays = parse_dtoverlay_list(
        """
        0: w1-gpio  gpiopin=17
        1: w1-gpio  gpiopin=22
        2: i2c-gpio  bus=3
        """
    )

    assert [item.gpio for item in overlays if item.name == "w1-gpio"] == [17, 22]


def test_parse_dtoverlay_list_treats_w1_gpio_without_pin_as_bcm4() -> None:
    overlays = parse_dtoverlay_list("0: w1-gpio\n")

    assert overlays[0].gpio == 4


def test_helper_client_rejects_unmapped_gpio() -> None:
    client = HelperOneWireOverlayClient(subprocess_run=lambda *_args, **_kwargs: None)

    with pytest.raises(DiagnosticHalt):
        client.activate(99)


def test_helper_client_parses_json_result(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("loggerhead.onewire.os.geteuid", lambda: 1000, raising=False)

    def fake_run(command, **_kwargs):
        assert command[-2:] == ["activate", "17"]
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"ok": True, "action": "activate", "gpio": 17, "created": True, "owned": True}),
            stderr="",
        )

    client = HelperOneWireOverlayClient(helper_path="/usr/local/sbin/loggerhead-onewire-helper", sudo_command="sudo", subprocess_run=fake_run)

    result = client.activate(17)

    assert result.created is True
    assert result.owned is True

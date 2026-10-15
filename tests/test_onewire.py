from __future__ import annotations

import json
import threading
import types
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path

import pytest

from loggerhead.hardware import DiagnosticHalt
from loggerhead.onewire import HelperOneWireOverlayClient, parse_dtoverlay_list


def load_helper_module():
    path = Path(__file__).parents[1] / "scripts" / "loggerhead-onewire-helper"
    loader = SourceFileLoader("loggerhead_onewire_helper_test", str(path))
    spec = spec_from_loader(loader.name, loader)
    assert spec is not None
    module = module_from_spec(spec)
    loader.exec_module(module)
    return module


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
            stdout=json.dumps(
                {
                    "ok": True,
                    "action": "activate",
                    "gpio": 17,
                    "created": True,
                    "owned": True,
                    "bus_master": "w1_bus_master10",
                    "owned_bus_masters": {"17": "w1_bus_master10"},
                }
            ),
            stderr="",
        )

    client = HelperOneWireOverlayClient(
        helper_path="/usr/local/sbin/loggerhead-onewire-helper", sudo_command="sudo", subprocess_run=fake_run
    )

    result = client.activate(17)

    assert result.created is True
    assert result.owned is True
    assert result.bus_master == "w1_bus_master10"
    assert result.owned_bus_masters == {17: "w1_bus_master10"}


def test_helper_status_cache_coalesces_workers_and_invalidates(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("loggerhead.onewire.os.geteuid", lambda: 1000, raising=False)
    state = tmp_path / "owned.json"
    state.write_text("{}")
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    masters = tmp_path / "w1"
    masters.mkdir()
    calls = []
    fail = False

    def run(command, **_kwargs):
        calls.append(command[-1])
        if fail:
            raise TimeoutError("helper timeout")
        return types.SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "action": command[-1]}), stderr="")

    client = HelperOneWireOverlayClient(
        subprocess_run=run,
        state_path=state,
        overlays_path=overlays,
        masters_path=masters,
    )
    threads = [threading.Thread(target=client.status) for _ in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert len(calls) == 1
    state.write_text('{"changed":true}')
    client.status()
    assert len(calls) == 2
    (overlays / "0_w1-gpio").mkdir()
    for name, content in (("dtbo", "overlay"), ("path", ""), ("status", "applied")):
        (overlays / "0_w1-gpio" / name).write_text(content)
    client.status()
    assert len(calls) == 3
    (masters / "w1_bus_master10").mkdir()
    client.status()
    assert len(calls) == 4
    client.release(17)
    client.status()
    assert len(calls) == 6  # Mutations always bypass and invalidate the cache.
    client._status_expires = 0
    fail = True
    with pytest.raises(RuntimeError, match="timeout"):
        client.status()
    with pytest.raises(RuntimeError, match="timeout"):
        client.status()  # Never fall back to a stale successful helper response.


def test_helper_status_without_verifiable_fingerprint_is_never_cached(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("loggerhead.onewire.os.geteuid", lambda: 1000, raising=False)
    calls = []

    def run(*_args, **_kwargs):
        calls.append(True)
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    client = HelperOneWireOverlayClient(subprocess_run=run, state_path=tmp_path / "missing")
    client.status()
    client.status()
    assert len(calls) == 2


def test_helper_status_cache_spans_sensor_polls_but_rechecks_changed_evidence(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("loggerhead.onewire.os.geteuid", lambda: 1000, raising=False)
    clock = [100.0]
    monkeypatch.setattr("loggerhead.onewire.time.monotonic", lambda: clock[0])
    state = tmp_path / "owned.json"
    state.write_text("{}")
    overlays = tmp_path / "overlays"
    overlays.mkdir()
    masters = tmp_path / "w1"
    masters.mkdir()
    calls = []

    def run(*_args, **_kwargs):
        calls.append(clock[0])
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    client = HelperOneWireOverlayClient(subprocess_run=run, state_path=state,
                                        overlays_path=overlays, masters_path=masters)
    for now in (100, 110, 120):
        clock[0] = now
        client.status()
    assert calls == [100]
    state.write_text('{"owned_gpios":{}}')
    client.status()
    assert calls == [100, 120]
    clock[0] = 151
    client.status()
    assert calls == [100, 120, 151]


def test_helper_cache_handles_trixie_configfs_inode_churn_but_checks_contents(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("loggerhead.onewire.os.geteuid", lambda: 1000, raising=False)
    state = tmp_path / "owned.json"
    state.write_text("{}")
    overlays = tmp_path / "overlays"
    overlay = overlays / "0_w1-gpio"
    overlay.mkdir(parents=True)
    masters = tmp_path / "w1"
    masters.mkdir()
    calls = []

    def churn_attributes():
        for name, data in (("dtbo", b"gpio=17"), ("path", b"\n"), ("status", b"applied\n")):
            (overlay / name).unlink(missing_ok=True)
            (overlay / name).write_bytes(data)

    def run(*_args, **_kwargs):
        calls.append(True)
        churn_attributes()
        return types.SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

    churn_attributes()
    client = HelperOneWireOverlayClient(subprocess_run=run, state_path=state,
                                        overlays_path=overlays, masters_path=masters)
    client.status()
    churn_attributes()
    client.status()
    assert len(calls) == 1
    (overlay / "dtbo").write_bytes(b"gpio=22")
    client.status()
    assert len(calls) == 2
    (overlay / "status").unlink()
    client.status()
    client.status()
    assert len(calls) == 4  # Unreadable/incomplete evidence never enables caching.


def test_helper_activate_records_new_w1_bus_master(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper_module()
    helper.STATE_DIR = tmp_path
    helper.STATE_PATH = tmp_path / "owned.json"
    overlays = {"active": False}
    calls = {"masters": 0}

    def list_overlays():
        return [{"index": 0, "name": "w1-gpio", "params": {"gpiopin": "17"}}] if overlays["active"] else []

    def activate_overlay(_command, **_kwargs):
        overlays["active"] = True

    monkeypatch.setattr(helper, "list_overlays", list_overlays)
    monkeypatch.setattr(helper, "run", activate_overlay)

    def list_masters() -> set[str]:
        calls["masters"] += 1
        return set() if calls["masters"] == 1 else {"w1_bus_master10"}

    monkeypatch.setattr(helper, "list_w1_master_names", list_masters)

    result = helper.activate(17)
    status = helper.status()

    assert result["bus_master"] == "w1_bus_master10"
    assert status["owned_bus_masters"] == {"17": "w1_bus_master10"}


def test_helper_release_refuses_unowned_active_overlay(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper_module()
    helper.STATE_DIR = tmp_path
    helper.STATE_PATH = tmp_path / "owned.json"
    monkeypatch.setattr(helper, "list_overlays", lambda: [{"index": 0, "name": "w1-gpio", "params": {"gpiopin": "17"}}])

    with pytest.raises(helper.HelperError, match="not owned"):
        helper.release(17)


def test_helper_release_refuses_stale_overlay_identity(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper_module()
    helper.STATE_DIR = tmp_path
    helper.STATE_PATH = tmp_path / "owned.json"
    helper.save_state(
        {
            "owned_gpios": {
                "17": {
                    "activated_at": 1.0,
                    "overlay": {"index": 0, "name": "w1-gpio", "params": {"gpiopin": "17"}},
                }
            }
        }
    )
    removed: list[list[str]] = []
    monkeypatch.setattr(helper, "list_overlays", lambda: [{"index": 1, "name": "w1-gpio", "params": {"gpiopin": "17"}}])
    monkeypatch.setattr(helper, "run", lambda command, **_kwargs: removed.append(command))

    with pytest.raises(helper.HelperError, match="no longer matches"):
        helper.release(17)

    assert removed == []


def test_helper_release_reports_later_w1_overlays_and_safe_handoff(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = load_helper_module()
    helper.STATE_DIR = tmp_path
    helper.STATE_PATH = tmp_path / "owned.json"
    helper.save_state(
        {
            "owned_gpios": {
                "4": {
                    "activated_at": 1.0,
                    "overlay": {"index": 0, "name": "w1-gpio", "params": {"gpiopin": "4"}},
                    "bus_master": "w1_bus_master4",
                },
                "17": {
                    "activated_at": 2.0,
                    "overlay": {"index": 1, "name": "w1-gpio", "params": {"gpiopin": "17"}},
                    "bus_master": "w1_bus_master17",
                },
            }
        }
    )
    overlays = [
        {"index": 0, "name": "w1-gpio", "params": {"gpiopin": "4"}},
        {"index": 1, "name": "w1-gpio", "params": {"gpiopin": "17"}},
    ]
    removed: list[list[str]] = []
    monkeypatch.setattr(helper, "list_overlays", lambda: overlays)
    monkeypatch.setattr(helper, "run", lambda command, **_kwargs: removed.append(command))
    monkeypatch.setattr(helper, "set_gpio_input", lambda _gpio: True)

    result = helper.release(4)
    status = helper.status()

    assert result["affected_gpios"] == [17]
    assert result["safe_input"] is True
    assert status["owned_gpios"] == [17]
    assert status["owned_bus_masters"] == {}
    assert removed == [[helper.DTOVERLAY, "-r", "0"]]

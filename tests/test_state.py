from __future__ import annotations

import json
import os
import threading

import pytest

from loggerhead.state import EquipmentState, RuntimeState, StateStore


def test_state_store_concurrent_saves_keep_valid_json(tmp_path) -> None:
    store = StateStore(tmp_path / "state.json")
    state = RuntimeState()

    def writer(index: int) -> None:
        state.equipment[f"ac{index}"] = EquipmentState(f"ac{index}", bool(index % 2))
        store.save(state)

    threads = [threading.Thread(target=writer, args=(index,)) for index in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "equipment" in data
    assert not list(tmp_path.glob("state.json.*.tmp"))


def test_state_store_preserves_existing_file_when_replace_fails(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "state.json"
    store = StateStore(path)
    original = RuntimeState(equipment={"ac1": EquipmentState("ac1", True)})
    store.save(original)

    def fail_replace(_src, _dst) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save(RuntimeState(equipment={"ac2": EquipmentState("ac2", False)}))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert set(data["equipment"]) == {"ac1"}
    assert not list(tmp_path.glob("state.json.*.tmp"))


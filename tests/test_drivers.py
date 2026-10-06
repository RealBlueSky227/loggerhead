from __future__ import annotations

import pytest

from loggerhead.drivers import (
    TMC2209UART,
    HydrosTripleClassifier,
    KasaHS300Client,
    MCP23017RelayBoard,
    StepperPulseEngine,
)
from loggerhead.hardware import RELAYS, STEPPERS, LevelState


@pytest.mark.parametrize(
    ("period", "state"),
    [
        (1260, LevelState.HIGH),
        (2520, LevelState.NORMAL),
        (5040, LevelState.LOW),
        (25200, LevelState.DRY),
        (9000, LevelState.UNKNOWN),
    ],
)
def test_hydros_period_classification(period: int, state: LevelState) -> None:
    assert HydrosTripleClassifier.classify_period_us(period) == state


def test_hydros_debounce_requires_stable_samples() -> None:
    classifier = HydrosTripleClassifier(debounce_samples=2)
    assert classifier.observe_period_us(2520) == LevelState.UNKNOWN
    assert classifier.observe_period_us(2520) == LevelState.NORMAL


def test_hs300_xor_round_trip() -> None:
    payload = '{"system":{"get_sysinfo":{}}}'
    encrypted = KasaHS300Client.encrypt(payload)
    assert KasaHS300Client.decrypt(encrypted) == payload


def test_relay_polarity_handles_nc_and_normally_on() -> None:
    assert MCP23017RelayBoard.software_to_physical(True, normally_on=False, relay=RELAYS["AC1"]) is True
    assert MCP23017RelayBoard.software_to_physical(True, normally_on=False, relay=RELAYS["AC7"]) is False
    assert MCP23017RelayBoard.software_to_physical(True, normally_on=True, relay=RELAYS["AC1"]) is False


def test_tmc_crc_is_stable() -> None:
    assert TMC2209UART.crc8(bytes([0x05, 0xFF, 0x00, 0x90, 0, 0, 0, 0])) == TMC2209UART.crc8(
        bytes([0x05, 0xFF, 0x00, 0x90, 0, 0, 0, 0])
    )


def test_stepper_interlock_releases_after_move() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None

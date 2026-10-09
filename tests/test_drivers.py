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


class FakeMCPBus:
    def __init__(self, registers: dict[int, int] | None = None) -> None:
        self.registers = registers or {}
        self.writes: list[tuple[int, int, int]] = []

    def read_byte_data(self, address: int, register: int) -> int:
        return self.registers.get(register, 0)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        self.registers[register] = value
        self.writes.append((address, register, value))


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


def test_mcp23017_preloads_stepper_enables_high_before_outputs() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.simulation = False
    relay.bus = FakeMCPBus()
    relay._initialize_outputs_safely()
    assert relay.bus.writes[:4] == [
        (relay.address, relay.OLATA, 0xF0),
        (relay.address, relay.OLATB, 0x00),
        (relay.address, relay.IODIRA, 0x00),
        (relay.address, relay.IODIRB, 0x00),
    ]


def test_mcp23017_warm_restart_preserves_relays_while_disabling_steppers() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.simulation = False
    relay.bus = FakeMCPBus({relay.OLATA: 0x05, relay.OLATB: 0x03})
    relay._initialize_outputs_safely()
    assert relay.shadow_a == 0xF5
    assert relay.shadow_b == 0x03


def test_relay_write_preserves_disabled_stepper_bits() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_pin("GPA0", True)
    assert relay.shadow_a == 0xF1
    relay.set_pin("GPA0", False)
    assert relay.shadow_a == 0xF0


def test_stepper_enable_is_active_low_and_disables_others() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_pin("GPA0", True)
    relay.set_stepper_enabled(STEPPERS["dose2"], True)
    assert relay.shadow_a == 0xD1
    relay.set_stepper_enabled(STEPPERS["dose2"], False)
    assert relay.shadow_a == 0xF1


def test_tmc_crc_is_stable() -> None:
    assert TMC2209UART.crc8(bytes([0x05, 0xFF, 0x00, 0x90, 0, 0, 0, 0])) == TMC2209UART.crc8(
        bytes([0x05, 0xFF, 0x00, 0x90, 0, 0, 0, 0])
    )


def test_stepper_interlock_releases_after_move() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_stepper_enabled(STEPPERS["dose3"], True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    assert relay.shadow_a == 0xF0
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None
    assert relay.shadow_a == 0xF0

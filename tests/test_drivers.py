from __future__ import annotations

import sys
import types

import pytest

from loggerhead.drivers import (
    TMC2209UART,
    HardwareFault,
    HydrosTripleClassifier,
    KasaHS300Client,
    MCP23017RelayBoard,
    StepperPulseEngine,
)
from loggerhead.hardware import RELAYS, STEPPERS, DiagnosticHalt, LevelState


class FakeMCPBus:
    IODIRA = 0x00
    IODIRB = 0x01
    GPIOA = 0x12
    GPIOB = 0x13
    OLATA = 0x14
    OLATB = 0x15
    STEPPER_MASK = 0xF0

    def __init__(
        self,
        registers: dict[int, int] | None = None,
        *,
        fail_on_write: set[int] | None = None,
        fail_on_read: set[int] | None = None,
    ) -> None:
        self.registers = {
            self.IODIRA: 0xFF,
            self.IODIRB: 0xFF,
            self.OLATA: 0x00,
            self.OLATB: 0x00,
            **(registers or {}),
        }
        self.fail_on_write = fail_on_write or set()
        self.fail_on_read = fail_on_read or set()
        self.writes: list[tuple[int, int, int]] = []
        self.observable_a_history: list[int] = [self.observable_a()]

    def read_byte_data(self, address: int, register: int) -> int:
        if register in self.fail_on_read:
            raise OSError(f"read fault {register:#04x}")
        if register == self.GPIOA:
            return self.observable_a()
        if register == self.GPIOB:
            iodir = self.registers[self.IODIRB]
            return self.registers[self.OLATB] & ~iodir
        return self.registers.get(register, 0)

    def write_byte_data(self, address: int, register: int, value: int) -> None:
        if register in self.fail_on_write:
            raise OSError(f"write fault {register:#04x}")
        value &= 0xFF
        self.registers[register] = value
        self.writes.append((address, register, value))
        observed = self.observable_a()
        self.observable_a_history.append(observed)
        low = (~observed) & self.STEPPER_MASK
        assert low == 0 or low & (low - 1) == 0, f"multiple ENN lines LOW after write: {observed:#04x}"

    def observable_a(self) -> int:
        iodir = self.registers[self.IODIRA]
        olat = self.registers[self.OLATA]
        external_pullups = self.STEPPER_MASK
        return (olat & ~iodir) | (external_pullups & iodir)


class FakeSerial:
    def __init__(self, read_data: bytes = b"") -> None:
        self.writes: list[bytes] = []
        self.read_data = bytearray(read_data)

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, size: int) -> bytes:
        if not self.read_data:
            return b""
        chunk = bytes(self.read_data[:size])
        del self.read_data[:size]
        return chunk


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
    assert all(value & 0xF0 == 0xF0 for value in relay.bus.observable_a_history)


def test_mcp23017_warm_restart_preserves_relays_while_disabling_steppers() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.simulation = False
    relay.bus = FakeMCPBus({relay.IODIRA: 0x00, relay.IODIRB: 0x00, relay.OLATA: 0x05, relay.OLATB: 0x03})
    relay._initialize_outputs_safely()
    assert relay.shadow_a == 0xF5
    assert relay.shadow_b == 0x03
    assert relay.bus.observable_a() == 0xF5


def test_relay_write_preserves_disabled_stepper_bits() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_pin("GPA0", True)
    assert relay.shadow_a == 0xF1
    relay.set_pin("GPA0", False)
    assert relay.shadow_a == 0xF0


def test_raw_writes_to_stepper_enable_pins_are_rejected() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    with pytest.raises(DiagnosticHalt):
        relay.set_pin("GPA4", False)


def test_stepper_enable_is_active_low_and_disables_others() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_pin("GPA0", True)
    relay.set_stepper_enabled(STEPPERS["dose2"], True)
    assert relay.shadow_a == 0xD1
    relay.set_stepper_enabled(STEPPERS["dose2"], False)
    assert relay.shadow_a == 0xF1


def test_stepper_switching_is_break_before_make() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.simulation = False
    relay.bus = FakeMCPBus({relay.IODIRA: 0x00, relay.IODIRB: 0x00, relay.OLATA: 0xF0})
    relay.set_pin("GPA0", True)
    relay.set_stepper_enabled(STEPPERS["dose1"], True)
    relay.set_stepper_enabled(STEPPERS["dose2"], True)
    assert relay.shadow_a == 0xD1
    olata_writes = [value for _, register, value in relay.bus.writes if register == relay.OLATA]
    assert olata_writes[-4:] == [0xF1, 0xE1, 0xF1, 0xD1]
    assert all(value & 0xF0 in {0xF0, 0xE0, 0xD0} for value in relay.bus.observable_a_history)


def test_ac_relay_write_preserves_active_stepper_state() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_stepper_enabled(STEPPERS["dose3"], True)
    relay.set_pin("GPA0", True)
    assert relay.shadow_a == 0xB1
    relay.disable_all_steppers()
    assert relay.shadow_a == 0xF1


def test_mcp23017_real_init_raises_on_bus_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def bad_smbus(_bus_id: int) -> object:
        raise OSError("i2c offline")

    monkeypatch.setitem(sys.modules, "smbus2", types.SimpleNamespace(SMBus=bad_smbus))
    with pytest.raises(HardwareFault):
        MCP23017RelayBoard(simulation=False)


def test_mcp23017_write_fault_blocks_future_actuator_operations() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.simulation = False
    relay.bus = FakeMCPBus(fail_on_write={relay.OLATA})
    with pytest.raises(HardwareFault):
        relay.set_pin("GPA0", True)
    relay.bus.fail_on_write.clear()
    with pytest.raises(HardwareFault):
        relay.set_pin("GPB0", True)


def test_tmc_write_frame_matches_datasheet_without_master_byte() -> None:
    uart = TMC2209UART(simulation=True)
    uart.simulation = False
    uart.serial = FakeSerial()
    uart.write_register(0, uart.REG_IHOLD_IRUN, 0, verify=False)
    assert uart.serial.writes == [bytes.fromhex("0500900000000050")]


def test_tmc_read_request_and_response_crc_match_datasheet_shape() -> None:
    response = bytes.fromhex("05ff6f1234567879")
    uart = TMC2209UART(simulation=True)
    uart.simulation = False
    uart.serial = FakeSerial(read_data=bytes.fromhex("05036f69") + response)
    assert uart.read_register(3, uart.REG_DRV_STATUS) == 0x12345678
    assert uart.serial.writes == [bytes.fromhex("05036f69")]


def test_stepper_interlock_releases_after_move() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_stepper_enabled(STEPPERS["dose3"], True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    assert relay.shadow_a == 0xF0
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None
    assert relay.shadow_a == 0xF0


def test_stepper_move_fault_still_disables_and_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)

    def fail_pulse(*_args, **_kwargs) -> None:
        raise RuntimeError("pulse fault")

    monkeypatch.setattr(engine, "_pulse_windowed", fail_pulse)
    with pytest.raises(RuntimeError, match="pulse fault"):
        engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None
    assert relay.shadow_a == 0xF0
    monkeypatch.undo()
    engine.move(STEPPERS["dose2"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert relay.shadow_a == 0xF0


def test_stepper_disable_fault_faults_engine_and_releases_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    original = relay.set_stepper_enabled

    def fail_disable(assignment, enabled: bool) -> None:
        if not enabled:
            raise HardwareFault("disable failed")
        original(assignment, enabled)

    monkeypatch.setattr(relay, "set_stepper_enabled", fail_disable)
    with pytest.raises(HardwareFault, match="disable failed"):
        engine.move(STEPPERS["dose1"], steps=0, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None
    with pytest.raises(HardwareFault, match="faulted"):
        engine.move(STEPPERS["dose2"], steps=0, steps_per_second=1000, run_current_ma=500)


def test_stepper_shutdown_disables_all_and_blocks_future_moves() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    relay.set_stepper_enabled(STEPPERS["dose4"], True)
    engine.shutdown()
    assert relay.shadow_a == 0xF0
    with pytest.raises(HardwareFault, match="shut down"):
        engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)

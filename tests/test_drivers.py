from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path

import pytest

from loggerhead.drivers import (
    TMC2209UART,
    Buzzer,
    HardwareFault,
    HardwareUnavailable,
    HydrosPulseReader,
    HydrosTripleClassifier,
    KasaHS300Client,
    MCP23017RelayBoard,
    PigpioResourceCoordinator,
    StepperPulseEngine,
    TemperatureReader,
)
from loggerhead.hardware import RELAYS, STEPPERS, AlarmPriority, DiagnosticHalt, LevelState


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


class FakePigpioPulse:
    def __init__(self, gpio_on: int, gpio_off: int, delay: int) -> None:
        self.gpio_on = gpio_on
        self.gpio_off = gpio_off
        self.delay = delay


class FakeDiagnosticUART:
    REG_IHOLD_IRUN = TMC2209UART.REG_IHOLD_IRUN

    def __init__(self, diagnostics: list[dict[str, object]]) -> None:
        self.diagnostics_queue = list(diagnostics)
        self.current_writes: list[tuple[int, int, int]] = []
        self.configure_calls: list[tuple[int, int, int]] = []

    def set_current(self, node: int, run_current_ma: int, hold_current_ma: int) -> None:
        self.current_writes.append((node, run_current_ma, hold_current_ma))

    def configure_driver(self, node: int, *, microsteps: int, stallguard_threshold: int = 0) -> None:
        self.configure_calls.append((node, microsteps, stallguard_threshold))

    def diagnostics(self, _node: int) -> dict[str, object]:
        if self.diagnostics_queue:
            return dict(self.diagnostics_queue.pop(0))
        return tmc_diag()


class FakePigpioPi:
    connected = True

    def __init__(self) -> None:
        self.hardware_pwm_calls: list[tuple[int, int, int]] = []
        self.mode_calls: list[tuple[int, int]] = []
        self.write_calls: list[tuple[int, int]] = []
        self.wave_add_generic_calls: list[list[FakePigpioPulse]] = []
        self.wave_create_calls = 0
        self.wave_send_once_calls: list[int] = []
        self.wave_chain_calls: list[list[int]] = []
        self.wave_delete_calls: list[int] = []
        self.wave_tx_stop_calls = 0
        self.created_waves: dict[int, list[FakePigpioPulse]] = {}
        self.deleted_waves: set[int] = set()
        self.next_wave_id = 1
        self.wave_add_result = 0
        self.wave_create_result: int | None = None
        self.wave_send_result: int | None = None
        self.wave_chain_result: int | None = None
        self.busy_cycles = 0
        self.busy_checks = 0
        self.on_busy = None
        self.fail_write = False
        self.fail_wave_tx_stop = False
        self.fail_wave_tx_busy = False

    def hardware_PWM(self, bcm_pin: int, frequency_hz: int, duty: int) -> None:
        self.hardware_pwm_calls.append((bcm_pin, frequency_hz, duty))

    def set_mode(self, bcm_pin: int, mode: int) -> None:
        self.mode_calls.append((bcm_pin, mode))

    def write(self, bcm_pin: int, value: int) -> None:
        if self.fail_write:
            raise RuntimeError("gpio write failed")
        self.write_calls.append((bcm_pin, value))

    def wave_add_generic(self, pulses: list[FakePigpioPulse]) -> int:
        self.wave_add_generic_calls.append(list(pulses))
        return self.wave_add_result

    def wave_create(self) -> int:
        self.wave_create_calls += 1
        if self.wave_create_result is not None:
            return self.wave_create_result
        wave_id = self.next_wave_id
        self.next_wave_id += 1
        self.created_waves[wave_id] = self.wave_add_generic_calls[-1]
        return wave_id

    def wave_send_once(self, wave_id: int) -> int:
        self.wave_send_once_calls.append(wave_id)
        if self.wave_send_result is not None:
            return self.wave_send_result
        return wave_id

    def wave_chain(self, chain: list[int]) -> int:
        self.wave_chain_calls.append(list(chain))
        if self.wave_chain_result is not None:
            return self.wave_chain_result
        return 0

    def wave_tx_busy(self) -> bool:
        if self.fail_wave_tx_busy:
            raise RuntimeError("wave busy failed")
        self.busy_checks += 1
        if self.on_busy:
            self.on_busy(self)
        if self.busy_cycles > 0:
            self.busy_cycles -= 1
            return True
        return False

    def wave_tx_stop(self) -> None:
        if self.fail_wave_tx_stop:
            raise RuntimeError("wave stop failed")
        self.wave_tx_stop_calls += 1
        self.busy_cycles = 0

    def wave_delete(self, wave_id: int) -> None:
        self.wave_delete_calls.append(wave_id)
        self.deleted_waves.add(wave_id)


class FakePigpioCallback:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeHydrosPigpioPi:
    connected = True

    def __init__(self) -> None:
        self.callback_func = None
        self.callback_obj = FakePigpioCallback()
        self.stopped = False

    def set_mode(self, _bcm_pin: int, _mode: int) -> None:
        return

    def set_pull_up_down(self, _bcm_pin: int, _pull: int) -> None:
        return

    def callback(self, _bcm_pin: int, _edge: int, func) -> FakePigpioCallback:
        self.callback_func = func
        return self.callback_obj

    def stop(self) -> None:
        self.stopped = True


class FakeHydrosPigpioModule:
    INPUT = 0
    PUD_UP = 2
    RISING_EDGE = 1

    def __init__(self, pi: FakeHydrosPigpioPi) -> None:
        self._pi = pi

    def pi(self) -> FakeHydrosPigpioPi:
        return self._pi


def tmc_diag(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "drv_status": 0,
        "sg_result": 100,
        "standstill": False,
        "stealthchop": True,
        "cs_actual": 20,
        "overtemp_warning": False,
        "overtemp_shutdown": False,
        "short_to_ground_a": False,
        "short_to_ground_b": False,
        "short_to_supply_a": False,
        "short_to_supply_b": False,
        "open_load_a": False,
        "open_load_b": False,
    }
    data.update(overrides)
    return data


def make_real_stepper_engine(monkeypatch: pytest.MonkeyPatch, fake_pi: FakePigpioPi | None = None):
    fake_pi = fake_pi or FakePigpioPi()
    monkeypatch.setitem(
        sys.modules,
        "pigpio",
        types.SimpleNamespace(OUTPUT=1, pulse=FakePigpioPulse, pi=lambda: fake_pi),
    )
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=False)
    return engine, relay, uart, fake_pi


@pytest.fixture(autouse=True)
def reset_pigpio_resource_coordinator() -> None:
    PigpioResourceCoordinator.reset_for_tests()


@pytest.mark.parametrize(
    ("period", "state"),
    [
        (2520, LevelState.HIGH),
        (5040, LevelState.NORMAL),
        (10080, LevelState.LOW),
        (50394, LevelState.DRY),
        (18000, LevelState.UNKNOWN),
    ],
)
def test_hydros_period_classification(period: int, state: LevelState) -> None:
    assert HydrosTripleClassifier.classify_period_us(period) == state


def test_hydros_debounce_requires_stable_samples() -> None:
    classifier = HydrosTripleClassifier(debounce_samples=2)
    assert classifier.observe_period_us(5040) == LevelState.UNKNOWN
    assert classifier.observe_period_us(5040) == LevelState.NORMAL


@pytest.mark.parametrize(
    ("period", "state"),
    [
        (2520, LevelState.HIGH),
        (5040, LevelState.NORMAL),
        (10080, LevelState.LOW),
        (50394, LevelState.DRY),
    ],
)
def test_hydros_rising_edge_sequences_classify_all_states(period: int, state: LevelState) -> None:
    classifier = HydrosTripleClassifier(debounce_samples=3, activity_timeout=10.0)
    ticks = [1_000, 1_000 + period, 1_000 + period * 2, 1_000 + period * 3]

    observed = LevelState.UNKNOWN
    for previous, current in zip(ticks, ticks[1:], strict=False):
        observed = classifier.observe_period_us(HydrosPulseReader._tick_diff(previous, current))

    assert observed == state


def test_hydros_invalid_frequency_debounces_to_unknown() -> None:
    classifier = HydrosTripleClassifier(debounce_samples=2)
    classifier.observe_period_us(5040)
    assert classifier.observe_period_us(5040) == LevelState.NORMAL
    assert classifier.observe_period_us(18000) == LevelState.NORMAL
    assert classifier.observe_period_us(18000) == LevelState.UNKNOWN


def test_hydros_pulse_reader_uses_rising_edge_periods() -> None:
    fake_pi = FakeHydrosPigpioPi()
    classifier = HydrosTripleClassifier(debounce_samples=2, activity_timeout=10.0)
    reader = HydrosPulseReader(17, classifier, pigpio_module=FakeHydrosPigpioModule(fake_pi))

    assert fake_pi.callback_func is not None
    fake_pi.callback_func(17, 1, 1_000)
    fake_pi.callback_func(17, 1, 6_040)
    assert reader.read_state() == LevelState.UNKNOWN
    fake_pi.callback_func(17, 1, 11_080)
    assert reader.read_state() == LevelState.NORMAL
    diagnostics = reader.diagnostics()
    assert diagnostics["period_us"] == 5040.0
    assert diagnostics["measurement_method"] == "rising_to_rising_period_us"
    reader.close()
    assert fake_pi.callback_obj.cancelled is True
    assert fake_pi.stopped is True


def test_hydros_missing_pulses_becomes_inactive() -> None:
    classifier = HydrosTripleClassifier(debounce_samples=1, activity_timeout=0.01)
    classifier.observe_period_us(5040)
    time.sleep(0.02)

    assert classifier.activity_state() == LevelState.INACTIVE


def write_w1_sensor(root: Path, sensor_id: str, temp_milli_c: int = 25_000) -> None:
    path = root / sensor_id
    path.mkdir(parents=True)
    path.joinpath("w1_slave").write_text(f"aa YES\nbb t={temp_milli_c}\n", encoding="utf-8")


def test_kernel_one_wire_existing_sensor_skips_privileged_setup(tmp_path) -> None:
    root = tmp_path / "w1"
    root.mkdir()
    (root / "w1_bus_master1").mkdir()
    write_w1_sensor(root, "28-000000000001")

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("dtoverlay should not be called when the configured sensor is already visible")

    reader = TemperatureReader(simulation=False, one_wire_root=root, subprocess_run=fail_if_called)
    reader.configure_kernel_one_wire(4, "28-000000000001")

    assert reader.read_one_wire_bus("28-000000000001", bcm_pin=4) == 77.0


def test_kernel_one_wire_uses_narrow_privileged_overlay_setup(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w1"
    root.mkdir()
    calls: list[list[str]] = []
    monkeypatch.setattr("loggerhead.drivers.os.geteuid", lambda: 1000, raising=False)

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        if command == ["dtoverlay", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout="No overlays loaded\n", stderr="")
        if command == ["sudo", "-n", "dtoverlay", "w1-gpio", "gpiopin=4"]:
            (root / "w1_bus_master1").mkdir()
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected command {command}")

    reader = TemperatureReader(
        simulation=False,
        one_wire_root=root,
        dtoverlay_command="dtoverlay",
        sudo_command="sudo",
        subprocess_run=fake_run,
    )
    reader.configure_kernel_one_wire(4)

    assert ["sudo", "-n", "dtoverlay", "w1-gpio", "gpiopin=4"] in calls


def test_kernel_one_wire_refuses_runtime_setup_when_process_is_root(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "w1"
    root.mkdir()
    monkeypatch.setattr("loggerhead.drivers.os.geteuid", lambda: 0, raising=False)

    def fake_run(command, **_kwargs):
        if command == ["dtoverlay", "-l"]:
            return types.SimpleNamespace(returncode=0, stdout="No overlays loaded\n", stderr="")
        raise AssertionError("privileged setup should not be attempted from a root process")

    reader = TemperatureReader(
        simulation=False,
        one_wire_root=root,
        dtoverlay_command="dtoverlay",
        sudo_command="sudo",
        subprocess_run=fake_run,
    )

    with pytest.raises(HardwareUnavailable, match="root Loggerhead process"):
        reader.configure_kernel_one_wire(4)


def test_kernel_one_wire_does_not_duplicate_loaded_overlay(tmp_path) -> None:
    root = tmp_path / "w1"
    root.mkdir()
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(list(command))
        return types.SimpleNamespace(returncode=0, stdout="0: w1-gpio  gpiopin=4\n", stderr="")

    reader = TemperatureReader(
        simulation=False,
        one_wire_root=root,
        dtoverlay_command="dtoverlay",
        sudo_command="sudo",
        subprocess_run=fake_run,
    )
    reader.configure_kernel_one_wire(4)

    assert calls == [["dtoverlay", "-l"]]


def test_kernel_one_wire_requires_unambiguous_sensor_id(tmp_path) -> None:
    root = tmp_path / "w1"
    root.mkdir()
    write_w1_sensor(root, "28-000000000001")
    write_w1_sensor(root, "28-000000000002")
    reader = TemperatureReader(simulation=False, one_wire_root=root)

    with pytest.raises(HardwareUnavailable, match="Multiple DS18B20 sensors"):
        reader.read_one_wire_bus()

    assert reader.read_one_wire_bus("28-000000000002") == 77.0


def test_kernel_one_wire_missing_sensor_is_not_simulated_on_real_hardware() -> None:
    reader = TemperatureReader(simulation=False)
    with pytest.raises(HardwareUnavailable, match="not found|not present|No DS18B20"):
        reader.read_one_wire_bus("28-000000000000")


def test_bit_banged_ds18b20_decodes_scratchpad_with_mocked_gpio(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = TemperatureReader(simulation=False)
    raw = int(25 * 16)
    scratchpad = bytearray([raw & 0xFF, (raw >> 8) & 0xFF, 0, 0, 0, 0, 0, 0, 0])
    scratchpad[8] = reader._crc8_maxim(bytes(scratchpad[:8]))

    class FakeOneWirePi:
        connected = True

        def set_pull_up_down(self, _pin: int, _pull: int) -> None:
            return

    monkeypatch.setitem(
        sys.modules,
        "pigpio",
        types.SimpleNamespace(PUD_UP=2, pi=lambda: FakeOneWirePi()),
    )
    monkeypatch.setattr(reader, "_read_ds18b20_scratchpad_bit_banged", lambda _pi, _pin: bytes(scratchpad))

    assert reader.read_bit_banged(4) == 77.0


def test_hs300_xor_round_trip() -> None:
    payload = '{"system":{"get_sysinfo":{}}}'
    encrypted = KasaHS300Client.encrypt(payload)
    assert KasaHS300Client.decrypt(encrypted) == payload


def test_buzzer_init_flushes_stale_hardware_pwm(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    monkeypatch.setitem(sys.modules, "pigpio", types.SimpleNamespace(pi=lambda: fake_pi))

    Buzzer(simulation=False)

    assert fake_pi.hardware_pwm_calls == [(12, 0, 0)]


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


def test_tmc_configure_driver_sets_uart_microsteps_and_stallguard() -> None:
    uart = TMC2209UART(simulation=True)
    uart.configure_driver(2, microsteps=16, stallguard_threshold=23)

    assert uart.read_register(2, uart.REG_GCONF) & (
        uart.GCONF_PDN_DISABLE | uart.GCONF_MSTEP_REG_SELECT
    ) == (uart.GCONF_PDN_DISABLE | uart.GCONF_MSTEP_REG_SELECT)
    assert uart.read_register(2, uart.REG_CHOPCONF) & uart.CHOPCONF_MRES_MASK == (
        uart.MICROSTEP_TO_MRES[16] << uart.CHOPCONF_MRES_SHIFT
    )
    assert uart.read_register(2, uart.REG_TPOWERDOWN) == 20
    assert uart.read_register(2, uart.REG_SGTHRS) == 23


def test_tmc_configure_driver_rejects_startup_charge_pump_fault() -> None:
    uart = TMC2209UART(simulation=True)
    uart._sim_registers[(0, uart.REG_GSTAT)] = uart.GSTAT_UV_CP

    with pytest.raises(HardwareFault, match="charge-pump undervoltage"):
        uart.configure_driver(0, microsteps=16)


def test_tmc_configure_driver_rejects_disabled_chopper() -> None:
    uart = TMC2209UART(simulation=True)
    uart._sim_registers[(0, uart.REG_CHOPCONF)] = 0

    with pytest.raises(HardwareFault, match="TOFF"):
        uart.configure_driver(0, microsteps=16)


def test_stepper_interlock_releases_after_move() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    relay.set_stepper_enabled(STEPPERS["dose3"], True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    assert relay.shadow_a == 0xF0
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert engine.active_stepper is None
    assert relay.shadow_a == 0xF0


def test_stepper_real_gpio_path_initializes_step_and_direction(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, uart, fake_pi = make_real_stepper_engine(monkeypatch)

    engine.move(
        STEPPERS["dose1"],
        steps=2,
        steps_per_second=5000,
        run_current_ma=500,
        microsteps=8,
        stallguard_threshold=17,
        direction_high=False,
    )

    assert (STEPPERS["dose1"].step_bcm, 1) in fake_pi.mode_calls
    assert (STEPPERS["dose1"].direction_bcm, 1) in fake_pi.mode_calls
    assert fake_pi.write_calls[:2] == [
        (STEPPERS["dose1"].step_bcm, 0),
        (STEPPERS["dose1"].direction_bcm, 0),
    ]
    assert fake_pi.write_calls[-1] == (STEPPERS["dose1"].step_bcm, 0)
    assert fake_pi.wave_chain_calls == [[1]]
    assert fake_pi.wave_delete_calls == [1]
    assert uart.read_register(0, uart.REG_SGTHRS) == 17
    assert uart.read_register(0, uart.REG_CHOPCONF) & uart.CHOPCONF_MRES_MASK == (
        uart.MICROSTEP_TO_MRES[8] << uart.CHOPCONF_MRES_SHIFT
    )
    assert relay.shadow_a == 0xF0


@pytest.mark.parametrize("steps_per_second", [100, 400, 1000, 5000])
def test_stepper_waveform_uses_dma_timed_edges(monkeypatch: pytest.MonkeyPatch, steps_per_second: int) -> None:
    engine, _relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch)

    engine.move(STEPPERS["dose1"], steps=10, steps_per_second=steps_per_second, run_current_ma=500)

    pulses = fake_pi.wave_add_generic_calls[0]
    step_mask = 1 << STEPPERS["dose1"].step_bcm
    assert len(pulses) == 20
    for high, low in zip(pulses[::2], pulses[1::2], strict=True):
        assert (high.gpio_on, high.gpio_off, high.delay) == (step_mask, 0, engine.STEP_HIGH_US)
        assert low.gpio_on == 0
        assert low.gpio_off == step_mask
        assert high.delay + low.delay == 1_000_000 // steps_per_second


def test_stepper_waveform_preserves_exact_requested_pulse_count(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch)

    engine.move(STEPPERS["dose1"], steps=257, steps_per_second=1000, run_current_ma=500)

    assert len(fake_pi.wave_add_generic_calls) == 1
    assert len(fake_pi.wave_add_generic_calls[0]) == 514


def test_stepper_continuous_real_wave_batches_are_accounted(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch)

    result = engine.run_continuous(
        STEPPERS["dose1"],
        stop_event=threading.Event(),
        steps_per_second=1000,
        run_current_ma=500,
        max_seconds=5.0,
        max_steps=1200,
    )

    assert result.steps_sent == 1200
    assert result.possible_steps == 1200
    assert result.completed_batches == 3
    assert result.interrupted_batch is False
    assert result.reason == "max_steps"
    assert [len(pulses) // 2 for pulses in fake_pi.wave_add_generic_calls] == [500, 200]
    assert fake_pi.wave_chain_calls == [[255, 0, 1, 255, 1, 2, 0, 2]]
    assert fake_pi.wave_delete_calls == [1, 2]
    assert relay.shadow_a == 0xF0


def test_stepper_stop_cancels_active_waveform(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.busy_cycles = 5
    engine, relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)
    stopped = False

    def stop_during_busy(_pi: FakePigpioPi) -> None:
        nonlocal stopped
        if not stopped:
            stopped = True
            engine._stop_event.set()

    fake_pi.on_busy = stop_during_busy

    engine.move(STEPPERS["dose1"], steps=10, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_tx_stop_calls >= 1
    assert fake_pi.wave_delete_calls == [1]
    assert fake_pi.write_calls[-1] == (STEPPERS["dose1"].step_bcm, 0)
    assert relay.shadow_a == 0xF0


def test_stepper_external_waveform_cancel_is_not_counted_successful(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.busy_cycles = 1
    engine, relay, _uart, _fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)

    with pytest.raises(HardwareFault, match="ended early"):
        engine.move(STEPPERS["dose1"], steps=500, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_tx_stop_calls == 0
    assert fake_pi.wave_delete_calls == [1]
    assert relay.shadow_a == 0xF0


def test_stepper_stop_before_send_prevents_waveform_transmission(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch)
    original_begin = engine._begin_wave_operation

    def stop_before_wave(owner: str) -> None:
        original_begin(owner)
        engine.request_stop()

    monkeypatch.setattr(engine, "_begin_wave_operation", stop_before_wave)

    with pytest.raises(HardwareFault, match="stopped before waveform"):
        engine.move(STEPPERS["dose1"], steps=20, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_chain_calls == []
    assert relay.shadow_a == 0xF0


def test_stepper_wave_create_failure_disables_motor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.wave_create_result = -1
    engine, relay, _uart, _fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)

    with pytest.raises(HardwareFault, match="wave_create"):
        engine.move(STEPPERS["dose1"], steps=10, steps_per_second=1000, run_current_ma=500)

    assert relay.shadow_a == 0xF0


def test_stepper_wave_add_failure_disables_motor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.wave_add_result = -1
    engine, relay, _uart, _fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)

    with pytest.raises(HardwareFault, match="wave_add_generic"):
        engine.move(STEPPERS["dose1"], steps=10, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_chain_calls == []
    assert relay.shadow_a == 0xF0


def test_stepper_wave_chain_failure_deletes_wave_and_disables_motor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.wave_chain_result = -1
    engine, relay, _uart, _fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)

    with pytest.raises(HardwareFault, match="wave_chain"):
        engine.move(STEPPERS["dose1"], steps=10, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_delete_calls == [1]
    assert relay.shadow_a == 0xF0


def test_stepper_uart_error_during_wave_stops_wave_and_disables_motor(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.busy_cycles = 2
    engine, relay, uart, _fake_pi = make_real_stepper_engine(monkeypatch, fake_pi)
    engine.WAVE_DIAGNOSTIC_SECONDS = 0.0
    diagnostics_calls = 0

    def diagnostics(_node: int) -> dict[str, object]:
        nonlocal diagnostics_calls
        diagnostics_calls += 1
        if diagnostics_calls >= 2:
            raise HardwareFault("uart read failed")
        return tmc_diag()

    monkeypatch.setattr(uart, "diagnostics", diagnostics)

    with pytest.raises(HardwareFault, match="uart read failed"):
        engine.move(STEPPERS["dose1"], steps=500, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_tx_stop_calls >= 1
    assert fake_pi.wave_delete_calls == [1]
    assert relay.shadow_a == 0xF0


def test_stepper_cleanup_attempts_disable_and_current_after_step_low_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, uart, fake_pi = make_real_stepper_engine(monkeypatch)
    writes_before_cleanup = 0

    def fail_step_low(_assignment) -> None:
        raise RuntimeError("step low failed")

    def start_failing_after_motion(_chain: list[int]) -> int:
        nonlocal writes_before_cleanup
        writes_before_cleanup = len(fake_pi.write_calls)
        monkeypatch.setattr(engine, "_set_step_low", fail_step_low)
        return 0

    monkeypatch.setattr(fake_pi, "wave_chain", start_failing_after_motion)

    with pytest.raises(RuntimeError, match="step low failed"):
        engine.move(STEPPERS["dose1"], steps=5, steps_per_second=1000, run_current_ma=500)

    assert relay.shadow_a == 0xF0
    assert uart.read_register(0, uart.REG_IHOLD_IRUN) == 5 << 16
    assert len(fake_pi.write_calls) == writes_before_cleanup
    with pytest.raises(HardwareFault, match="faulted"):
        engine.move(STEPPERS["dose2"], steps=1, steps_per_second=1000, run_current_ma=500)


def test_stepper_cleanup_attempts_disable_after_current_shutdown_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, uart, fake_pi = make_real_stepper_engine(monkeypatch)
    original_set_current = uart.set_current

    def fail_zero_current(node: int, run_current_ma: int, hold_current_ma: int) -> None:
        if run_current_ma == 0 and hold_current_ma == 0:
            raise HardwareFault("current shutdown failed")
        original_set_current(node, run_current_ma, hold_current_ma)

    monkeypatch.setattr(uart, "set_current", fail_zero_current)

    with pytest.raises(HardwareFault, match="current shutdown"):
        engine.move(STEPPERS["dose1"], steps=5, steps_per_second=1000, run_current_ma=500)

    assert relay.shadow_a == 0xF0
    assert fake_pi.write_calls[-1] == (STEPPERS["dose1"].step_bcm, 0)
    with pytest.raises(HardwareFault, match="faulted"):
        engine.move(STEPPERS["dose2"], steps=1, steps_per_second=1000, run_current_ma=500)


def test_stepper_real_mode_rejects_disconnected_pigpiod(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.connected = False
    monkeypatch.setitem(
        sys.modules,
        "pigpio",
        types.SimpleNamespace(OUTPUT=1, pulse=FakePigpioPulse, pi=lambda: fake_pi),
    )

    with pytest.raises(HardwareFault, match="pigpiod is not connected"):
        StepperPulseEngine(TMC2209UART(simulation=True), MCP23017RelayBoard(simulation=True), simulation=False)


def test_buzzer_defers_hardware_pwm_while_stepper_wave_active(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    monkeypatch.setitem(sys.modules, "pigpio", types.SimpleNamespace(pi=lambda: fake_pi))
    buzzer = Buzzer(simulation=False)
    fake_pi.hardware_pwm_calls.clear()

    PigpioResourceCoordinator.begin_stepper_wave("stepper:test")
    try:
        buzzer.sound(AlarmPriority.HIGH)
    finally:
        PigpioResourceCoordinator.end_stepper_wave("stepper:test")

    assert fake_pi.hardware_pwm_calls == []


def test_buzzer_defers_during_full_stepper_operation(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_pi = FakePigpioPi()
    fake_pi.busy_cycles = 2
    monkeypatch.setitem(
        sys.modules,
        "pigpio",
        types.SimpleNamespace(OUTPUT=1, pulse=FakePigpioPulse, pi=lambda: fake_pi),
    )
    buzzer = Buzzer(simulation=False)
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=False)
    fake_pi.hardware_pwm_calls.clear()

    def sound_alarm_while_wave_active(_pi: FakePigpioPi) -> None:
        buzzer.sound(AlarmPriority.HIGH)

    fake_pi.on_busy = sound_alarm_while_wave_active
    engine.move(STEPPERS["dose1"], steps=10, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.hardware_pwm_calls == []
    assert relay.shadow_a == 0xF0


def test_stepper_repeated_wave_cycles_do_not_leak_wave_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, relay, _uart, fake_pi = make_real_stepper_engine(monkeypatch)

    for _ in range(3):
        engine.move(STEPPERS["dose1"], steps=25, steps_per_second=1000, run_current_ma=500)

    assert fake_pi.wave_chain_calls == [[1], [2], [3]]
    assert fake_pi.wave_delete_calls == [1, 2, 3]
    assert engine._wave_ids == set()
    assert relay.shadow_a == 0xF0


def test_stepper_interlock_rejects_second_motor_while_one_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    stop = threading.Event()
    started = threading.Event()

    def run_first() -> None:
        started.set()
        engine.run_continuous(
            STEPPERS["dose1"],
            stop_event=stop,
            steps_per_second=100,
            run_current_ma=500,
            max_seconds=5.0,
            max_steps=1000,
        )

    thread = threading.Thread(target=run_first)
    thread.start()
    assert started.wait(1.0)
    while engine.active_stepper != "dose1":
        time.sleep(0.001)

    with pytest.raises(RuntimeError, match="already active"):
        engine.move(STEPPERS["dose2"], steps=1, steps_per_second=1000, run_current_ma=500)

    stop.set()
    engine.request_stop()
    thread.join(timeout=1.0)
    assert not thread.is_alive()


def test_stepper_continuous_run_enables_once_and_honors_step_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = TMC2209UART(simulation=True)
    engine = StepperPulseEngine(uart, relay, simulation=True)
    enable_calls: list[bool] = []
    original_set_stepper_enabled = relay.set_stepper_enabled

    def track_enable(assignment, enabled: bool) -> None:
        enable_calls.append(enabled)
        original_set_stepper_enabled(assignment, enabled)

    monkeypatch.setattr(relay, "set_stepper_enabled", track_enable)
    result = engine.run_continuous(
        STEPPERS["dose1"],
        stop_event=threading.Event(),
        steps_per_second=5000,
        run_current_ma=500,
        max_seconds=5.0,
        max_steps=3,
    )

    assert result.steps_sent == 3
    assert result.reason == "max_steps"
    assert result.possible_steps == 3
    assert result.completed_batches == 1
    assert enable_calls == [True, False]
    assert relay.shadow_a == 0xF0


def test_stepper_low_sg_result_at_standstill_is_not_a_stall_fault() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = FakeDiagnosticUART(
        [
            tmc_diag(sg_result=0, standstill=True),
            tmc_diag(sg_result=0, standstill=True),
        ]
    )
    engine = StepperPulseEngine(uart, relay, simulation=True)
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500, stallguard_threshold=50)
    assert relay.shadow_a == 0xF0


def test_stepper_low_sg_result_at_low_speed_is_logged_not_faulted() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = FakeDiagnosticUART(
        [
            tmc_diag(sg_result=0, standstill=False, stealthchop=True),
            tmc_diag(sg_result=0, standstill=False, stealthchop=True),
        ]
    )
    engine = StepperPulseEngine(uart, relay, simulation=True)
    engine.move(
        STEPPERS["dose1"],
        steps=8,
        steps_per_second=8,
        run_current_ma=500,
        microsteps=16,
        stallguard_threshold=50,
    )
    assert relay.shadow_a == 0xF0


def test_stepper_open_load_indicator_does_not_stop_at_startup() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = FakeDiagnosticUART(
        [
            tmc_diag(open_load_a=True, open_load_b=True, standstill=True, sg_result=0),
            tmc_diag(open_load_a=True, open_load_b=True, standstill=True, sg_result=0),
        ]
    )
    engine = StepperPulseEngine(uart, relay, simulation=True)
    engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert relay.shadow_a == 0xF0


def test_stepper_short_to_ground_is_genuine_fault() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = FakeDiagnosticUART([tmc_diag(short_to_ground_a=True, drv_status=1 << 2)])
    engine = StepperPulseEngine(uart, relay, simulation=True)
    with pytest.raises(HardwareUnavailable, match="electrical fault"):
        engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
    assert relay.shadow_a == 0xF0


def test_stepper_overtemp_is_genuine_fault() -> None:
    relay = MCP23017RelayBoard(simulation=True)
    uart = FakeDiagnosticUART([tmc_diag(overtemp_shutdown=True, drv_status=0x02)])
    engine = StepperPulseEngine(uart, relay, simulation=True)
    with pytest.raises(HardwareUnavailable, match="thermal warning"):
        engine.move(STEPPERS["dose1"], steps=1, steps_per_second=1000, run_current_ma=500)
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

"""Tests for the GridBOSS smart port function switches (register 229)."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.eg4_web_monitor.const import (
    MIDBOX_REG_SMART_PORT_FUNCTIONS,
    decode_midbox_smart_port_functions,
    midbox_smart_port_function_bit,
)
from custom_components.eg4_web_monitor.coordinator import EG4DataUpdateCoordinator
from custom_components.eg4_web_monitor.coordinator_http import HTTPUpdateMixin
from custom_components.eg4_web_monitor.coordinator_local import LocalTransportMixin
from custom_components.eg4_web_monitor.smart_port import (
    EG4SmartPortFunctionSwitch,
    create_smart_port_switches,
)

GRIDBOSS_SERIAL = "9876543210"


# ── Register 229 decode (pin evidence in const/modbus.py) ────────────


def test_decode_live_third_system_raw_117():
    """Raw 117: SL enables ports 1/3, grid-on ports 1-3 (live dongle pin)."""
    decoded = decode_midbox_smart_port_functions(117)
    assert decoded == {
        "FUNC_SMART_LOAD_EN_1": True,
        "FUNC_SMART_LOAD_EN_2": False,
        "FUNC_SMART_LOAD_EN_3": True,
        "FUNC_SMART_LOAD_EN_4": False,
        "FUNC_SMART_LOAD_GRID_ON_1": True,
        "FUNC_SMART_LOAD_GRID_ON_2": True,
        "FUNC_SMART_LOAD_GRID_ON_3": True,
        "FUNC_SMART_LOAD_GRID_ON_4": False,
        "FUNC_AC_COUPLE_EN_1": False,
        "FUNC_AC_COUPLE_EN_2": False,
        "FUNC_AC_COUPLE_EN_3": False,
        "FUNC_AC_COUPLE_EN_4": False,
    }


def test_decode_dump_raw_152():
    """Raw 152 = SL_EN_4 + GRID_ON_1 + GRID_ON_4 (GridBoss_43XXXXXX85 dump)."""
    decoded = decode_midbox_smart_port_functions(152)
    true_keys = {key for key, value in decoded.items() if value}
    assert true_keys == {
        "FUNC_SMART_LOAD_EN_4",
        "FUNC_SMART_LOAD_GRID_ON_1",
        "FUNC_SMART_LOAD_GRID_ON_4",
    }


def test_decode_dump_raw_256():
    """Raw 256 = FUNC_AC_COUPLE_EN_1 alone (GridBoss_example dump)."""
    decoded = decode_midbox_smart_port_functions(256)
    true_keys = {key for key, value in decoded.items() if value}
    assert true_keys == {"FUNC_AC_COUPLE_EN_1"}


def test_decode_zero_is_all_false():
    """Raw 0 decodes to twelve False values."""
    decoded = decode_midbox_smart_port_functions(0)
    assert len(decoded) == 12
    assert not any(decoded.values())


def test_function_bit_positions():
    """Bit = group base + port - 1 for every exposed group."""
    assert midbox_smart_port_function_bit("FUNC_SMART_LOAD_EN", 1) == 0
    assert midbox_smart_port_function_bit("FUNC_SMART_LOAD_EN", 4) == 3
    assert midbox_smart_port_function_bit("FUNC_SMART_LOAD_GRID_ON", 1) == 4
    assert midbox_smart_port_function_bit("FUNC_AC_COUPLE_EN", 4) == 11


# ── Fixtures ─────────────────────────────────────────────────────────


def _mock_coordinator(
    *,
    statuses: dict[int, str] | None = None,
    parameters: dict | None = None,
    has_local: bool = True,
    link_down: bool = False,
    has_http: bool = False,
) -> MagicMock:
    """Build a mock coordinator with one GridBOSS device."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.has_configured_local_transport = MagicMock(return_value=has_local)
    coordinator.has_local_transport = MagicMock(return_value=has_local)
    coordinator.is_transport_link_down = MagicMock(return_value=link_down)
    coordinator.has_http_api = MagicMock(return_value=has_http)
    coordinator.write_midbox_smart_port_function = AsyncMock()
    coordinator.note_parameters_written = MagicMock()

    sensors: dict = {}
    for port, status in (statuses or {}).items():
        sensors[f"smart_port{port}_status"] = status
    coordinator.data = {
        "devices": {
            GRIDBOSS_SERIAL: {
                "type": "gridboss",
                "model": "GridBOSS",
                "sensors": sensors,
            },
        },
        "parameters": {GRIDBOSS_SERIAL: parameters or {}},
    }
    return coordinator


def _switch(
    coordinator: MagicMock, port: int, spec_index: int
) -> EG4SmartPortFunctionSwitch:
    """Build one switch (spec order: smart load, grid always on, AC couple)."""
    switches = create_smart_port_switches(coordinator, GRIDBOSS_SERIAL)
    switch = switches[(port - 1) * 3 + spec_index]
    switch.async_write_ha_state = MagicMock()  # type: ignore[method-assign]
    return switch


# ── Entity creation ──────────────────────────────────────────────────


def test_factory_creates_twelve_switches():
    """Four ports x three functions, unique ids keyed by port and function."""
    switches = create_smart_port_switches(_mock_coordinator(), GRIDBOSS_SERIAL)
    assert len(switches) == 12
    unique_ids = {switch.unique_id for switch in switches}
    assert f"{GRIDBOSS_SERIAL}_smart_port1_smart_load_enabled" in unique_ids
    assert f"{GRIDBOSS_SERIAL}_smart_port3_grid_always_on" in unique_ids
    assert f"{GRIDBOSS_SERIAL}_smart_port4_ac_couple_enabled" in unique_ids


def test_factory_requires_local_transport():
    """No configured local transport -> no state source -> no switches."""
    coordinator = _mock_coordinator(has_local=False)
    coordinator.has_configured_local_transport = MagicMock(return_value=False)
    assert create_smart_port_switches(coordinator, GRIDBOSS_SERIAL) == []


# ── Availability and state ───────────────────────────────────────────


def test_availability_follows_port_mode():
    """Each function is available only in its port mode."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load", 2: "ac_couple", 3: "unused"},
        parameters=decode_midbox_smart_port_functions(117),
    )
    smart_load, grid_on, ac_couple = (_switch(coordinator, 1, i) for i in range(3))
    assert smart_load.available is True
    assert grid_on.available is True
    assert ac_couple.available is False

    smart_load, grid_on, ac_couple = (_switch(coordinator, 2, i) for i in range(3))
    assert smart_load.available is False
    assert grid_on.available is False
    assert ac_couple.available is True

    # Unused port and a port with no status this cycle: all unavailable.
    for port in (3, 4):
        for index in range(3):
            assert _switch(coordinator, port, index).available is False


def test_unavailable_until_first_parameter_read():
    """Right mode but never-read enable state must not show a fake OFF."""
    coordinator = _mock_coordinator(statuses={1: "smart_load"}, parameters={})
    switch = _switch(coordinator, 1, 0)
    assert switch.available is False
    assert switch.is_on is None


def test_unavailable_on_failed_update_or_wrong_device_type():
    """Coordinator failure or a non-GridBOSS device row disables the switch."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load"},
        parameters=decode_midbox_smart_port_functions(117),
    )
    switch = _switch(coordinator, 1, 0)
    coordinator.last_update_success = False
    assert switch.available is False
    coordinator.last_update_success = True
    coordinator.data["devices"][GRIDBOSS_SERIAL]["type"] = "inverter"
    assert switch.available is False


def test_is_on_reflects_parameter_store():
    """States decode from the raw-117 live fixture."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load", 2: "smart_load", 3: "smart_load"},
        parameters=decode_midbox_smart_port_functions(117),
    )
    assert _switch(coordinator, 1, 0).is_on is True  # SL_EN_1
    assert _switch(coordinator, 2, 0).is_on is False  # SL_EN_2
    assert _switch(coordinator, 2, 1).is_on is True  # GRID_ON_2


# ── Write routing ────────────────────────────────────────────────────


async def test_turn_on_routes_to_local_rmw():
    """The local path calls the coordinator's locked read-modify-write."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load"},
        parameters=decode_midbox_smart_port_functions(117),
    )
    switch = _switch(coordinator, 1, 0)
    await switch.async_turn_on()
    coordinator.write_midbox_smart_port_function.assert_awaited_once_with(
        GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 1, True
    )


async def test_local_failure_without_cloud_propagates():
    """LOCAL-only: a failed local write raises and clears optimistic state."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load"},
        parameters=decode_midbox_smart_port_functions(117),
    )
    coordinator.write_midbox_smart_port_function.side_effect = HomeAssistantError(
        "boom"
    )
    switch = _switch(coordinator, 1, 0)
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_off()
    assert switch._optimistic_state is None


@pytest.mark.parametrize(
    ("spec_index", "enabled", "method", "args"),
    [
        (0, True, "enable_smart_load", (GRIDBOSS_SERIAL, 2)),
        (0, False, "disable_smart_load", (GRIDBOSS_SERIAL, 2)),
        (2, True, "enable_ac_couple", (GRIDBOSS_SERIAL, 2)),
        (2, False, "disable_ac_couple", (GRIDBOSS_SERIAL, 2)),
        (1, True, "set_smart_load_grid_on", (GRIDBOSS_SERIAL, 2, True)),
        (1, False, "set_smart_load_grid_on", (GRIDBOSS_SERIAL, 2, False)),
    ],
)
async def test_cloud_fallback_routes_to_functioncontrol(
    spec_index, enabled, method, args
):
    """A down local link routes each function to its pylxpweb cloud wrapper."""
    coordinator = _mock_coordinator(
        statuses={2: "smart_load", 3: "ac_couple"},
        parameters=decode_midbox_smart_port_functions(0),
        link_down=True,
        has_http=True,
    )
    control = coordinator.require_client.return_value.api.control
    result = MagicMock()
    result.success = True
    for name in (
        "enable_smart_load",
        "disable_smart_load",
        "enable_ac_couple",
        "disable_ac_couple",
        "set_smart_load_grid_on",
    ):
        setattr(control, name, AsyncMock(return_value=result))

    switch = _switch(coordinator, 2, spec_index)
    if enabled:
        await switch.async_turn_on()
    else:
        await switch.async_turn_off()

    getattr(control, method).assert_awaited_once_with(*args)
    coordinator.write_midbox_smart_port_function.assert_not_awaited()
    # The write envelope seeds the parameter cache with the acknowledged
    # cloud write while a local transport is attached.
    coordinator.note_parameters_written.assert_called_once()


async def test_cloud_result_failure_raises():
    """An unsuccessful cloud response raises and clears optimistic state."""
    coordinator = _mock_coordinator(
        statuses={1: "smart_load"},
        parameters=decode_midbox_smart_port_functions(0),
        link_down=True,
        has_http=True,
    )
    control = coordinator.require_client.return_value.api.control
    result = MagicMock()
    result.success = False
    control.enable_smart_load = AsyncMock(return_value=result)
    switch = _switch(coordinator, 1, 0)
    with pytest.raises(HomeAssistantError):
        await switch.async_turn_on()
    assert switch._optimistic_state is None


# ── Coordinator read-modify-write (register 229) ─────────────────────


def _rmw_self(read_values: list[int | None]) -> MagicMock:
    """Mock coordinator self for write_midbox_smart_port_function."""
    mock_self = MagicMock()
    mock_self._midbox_function_locks = {}
    transport = MagicMock()
    transport.is_connected = True
    transport.read_parameters = AsyncMock(
        side_effect=[
            {MIDBOX_REG_SMART_PORT_FUNCTIONS: value} if value is not None else {}
            for value in read_values
        ]
    )
    transport.write_parameters = AsyncMock()
    mock_self.get_local_transport = MagicMock(return_value=transport)
    mock_self.note_parameters_written = MagicMock()
    mock_self._transport = transport
    return mock_self


async def test_rmw_sets_bit_and_seeds_cache():
    """Enabling SL_EN_2 on raw 117 writes 119 and seeds the verify decode."""
    mock_self = _rmw_self([117, 119])
    await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
        mock_self, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 2, True
    )
    mock_self._transport.write_parameters.assert_awaited_once_with(
        {MIDBOX_REG_SMART_PORT_FUNCTIONS: 119}
    )
    mock_self.note_parameters_written.assert_called_once_with(
        GRIDBOSS_SERIAL, decode_midbox_smart_port_functions(119)
    )


async def test_rmw_clears_bit():
    """Disabling GRID_ON_2 on raw 117 clears bit 5 -> 85."""
    mock_self = _rmw_self([117, 85])
    await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
        mock_self, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_GRID_ON", 2, False
    )
    mock_self._transport.write_parameters.assert_awaited_once_with(
        {MIDBOX_REG_SMART_PORT_FUNCTIONS: 85}
    )


async def test_rmw_skips_noop_write():
    """Writing an already-set bit skips the register write, still verifies."""
    mock_self = _rmw_self([117, 117])
    await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
        mock_self, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 1, True
    )
    mock_self._transport.write_parameters.assert_not_awaited()
    mock_self.note_parameters_written.assert_called_once()


async def test_rmw_raises_on_firmware_nak_but_seeds_truth():
    """A verify read still showing the old value raises after seeding truth."""
    mock_self = _rmw_self([117, 117])
    with pytest.raises(HomeAssistantError):
        await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
            mock_self, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 2, True
        )
    mock_self.note_parameters_written.assert_called_once_with(
        GRIDBOSS_SERIAL, decode_midbox_smart_port_functions(117)
    )


async def test_rmw_requires_transport_and_value():
    """No transport, or a read with no register value, raises."""
    no_transport = MagicMock()
    no_transport._midbox_function_locks = {}
    no_transport.get_local_transport = MagicMock(return_value=None)
    with pytest.raises(HomeAssistantError):
        await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
            no_transport, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 1, True
        )

    mock_self = _rmw_self([None])
    with pytest.raises(HomeAssistantError):
        await EG4DataUpdateCoordinator.write_midbox_smart_port_function(
            mock_self, GRIDBOSS_SERIAL, "FUNC_SMART_LOAD_EN", 1, True
        )


# ── Coordinator refresh reads ────────────────────────────────────────


async def test_local_read_decodes_register():
    """A good register read returns the decoded parameter dict."""
    mock_self = MagicMock()
    mock_self.data = {}
    transport = MagicMock()
    transport.is_connected = True
    transport.read_parameters = AsyncMock(
        return_value={MIDBOX_REG_SMART_PORT_FUNCTIONS: 117}
    )
    params = await LocalTransportMixin._read_midbox_smart_port_functions(
        mock_self, transport, GRIDBOSS_SERIAL
    )
    assert params == decode_midbox_smart_port_functions(117)


async def test_local_read_failure_carries_forward():
    """A failed read keeps the previous cycle's values (#282 semantics)."""
    previous = decode_midbox_smart_port_functions(117)
    mock_self = MagicMock()
    mock_self.data = {"parameters": {GRIDBOSS_SERIAL: previous}}
    transport = MagicMock()
    transport.is_connected = True
    transport.read_parameters = AsyncMock(side_effect=TimeoutError("dead socket"))
    params = await LocalTransportMixin._read_midbox_smart_port_functions(
        mock_self, transport, GRIDBOSS_SERIAL
    )
    assert params == previous
    assert params is not previous  # a copy, not the shared dict

    # No transport and no previous cycle: empty store, never an exception.
    mock_self.data = {}
    params = await LocalTransportMixin._read_midbox_smart_port_functions(
        mock_self, None, GRIDBOSS_SERIAL
    )
    assert params == {}


async def test_http_pass_reads_only_live_transport_mids():
    """The HYBRID pass reads attached-and-up MIDs, skips the rest."""
    live_mid = MagicMock()
    live_mid.serial_number = GRIDBOSS_SERIAL
    live_mid.transport = MagicMock()
    live_mid.transport_link_down = False

    cloud_mid = MagicMock()
    cloud_mid.serial_number = "1111111111"
    cloud_mid.transport = None

    down_mid = MagicMock()
    down_mid.serial_number = "2222222222"
    down_mid.transport = MagicMock()
    down_mid.transport_link_down = True

    mock_self = MagicMock()
    mock_self.station.all_mid_devices = [live_mid, cloud_mid, down_mid]
    decoded = decode_midbox_smart_port_functions(117)
    mock_self._read_midbox_smart_port_functions = AsyncMock(return_value=decoded)

    processed: dict = {
        "devices": {
            GRIDBOSS_SERIAL: {"type": "gridboss"},
            "1111111111": {"type": "gridboss"},
            "2222222222": {"type": "gridboss"},
        },
        "parameters": {},
    }
    await HTTPUpdateMixin._update_midbox_smart_port_parameters(mock_self, processed)
    mock_self._read_midbox_smart_port_functions.assert_awaited_once_with(
        live_mid.transport, GRIDBOSS_SERIAL
    )
    assert processed["parameters"] == {GRIDBOSS_SERIAL: decoded}

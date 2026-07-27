"""Tests for the "AC Charge Based On" select (reg 120) and its mode gating.

Covers the four legs of the feature:
- the EG4_HYBRID-gated select entity (creation, decode, both write routes);
- the coordinator's verified local RMW write (write_ac_charge_type);
- the LOCAL raw read + the HYBRID/CLOUD refresh overlay that both bypass
  pylxpweb's mis-modeled reg-120 named decode (single bit 3);
- the mode-based availability gating of the AC Charge schedule times, the
  AC Charge SOC Limit, and the AC Charge Start/End Voltage pair
  (ac_charge_type_allows).

Value space everywhere is the CLOUD space pinned live on the FlexBOSS21
(see the AC_CHARGE_TYPE evidence block in const/modbus.py): Time=0,
Volt=2, Time+SOC/Volt=4, equal to raw & 0x0E.
"""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.eg4_web_monitor.const import (
    AC_CHARGE_TYPE_THRESHOLD_MODES,
    AC_CHARGE_TYPE_TIME_MODES,
    INVERTER_FAMILY_EG4_HYBRID,
    PARAM_BIT_AC_CHARGE_TYPE,
    REG_AC_CHARGE_TYPE,
    SCHEDULE_TIME_TYPES,
)
from custom_components.eg4_web_monitor.coordinator import EG4DataUpdateCoordinator
from custom_components.eg4_web_monitor.number import (
    VOLTAGE_NUMBER_SPECS,
    ACChargeSOCLimitNumber,
    EG4VoltageNumber,
)
from custom_components.eg4_web_monitor.select import (
    AC_CHARGE_TYPE_OPTIONS,
    EG4ACChargeTypeSelect,
    async_setup_entry,
)
from custom_components.eg4_web_monitor.time import EG4ScheduleTimeEntity
from custom_components.eg4_web_monitor.utils import ac_charge_type_allows
from tests.conftest import wire_coordinator_write_helpers

SERIAL = "1234567890"

_AC_CHARGE_SPEC = next(s for s in SCHEDULE_TIME_TYPES if s.key == "ac_charge")
_FORCED_CHARGE_SPEC = next(s for s in SCHEDULE_TIME_TYPES if s.key == "forced_charge")


# ── Helpers ──────────────────────────────────────────────────────────


def _mock_coordinator(
    *,
    serial: str = SERIAL,
    model: str = "FlexBOSS21",
    family: str | None = INVERTER_FAMILY_EG4_HYBRID,
    parameters: dict | None = None,
) -> MagicMock:
    """Build a mock coordinator with a (by default) EG4_HYBRID inverter."""
    coordinator = MagicMock()
    coordinator.last_update_success = True
    coordinator.has_http_api = MagicMock(return_value=True)
    coordinator.is_transport_link_down = MagicMock(return_value=False)
    coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    coordinator.async_refresh_device_parameters = AsyncMock()
    coordinator.write_ac_charge_type = AsyncMock(return_value=True)
    coordinator.get_device_info = MagicMock(return_value=None)
    # Cloud-shaped parameter decode for the schedule time entities.
    coordinator.is_local_only = MagicMock(return_value=False)
    coordinator.has_configured_local_transport = MagicMock(return_value=False)
    coordinator.get_inverter_object = MagicMock(return_value=None)
    coordinator.get_configured_control_modes = MagicMock(return_value=("soc", "soc"))

    device: dict = {"type": "inverter", "model": model}
    if family is not None:
        device["features"] = {"inverter_family": family}
    coordinator.data = {
        "devices": {serial: device},
        "parameters": {serial: parameters or {}},
    }

    wire_coordinator_write_helpers(coordinator)
    return coordinator


def _select(coordinator: MagicMock) -> EG4ACChargeTypeSelect:
    device_data = coordinator.data["devices"][SERIAL]
    return EG4ACChargeTypeSelect(coordinator, SERIAL, device_data)


# ── Platform setup ───────────────────────────────────────────────────


class TestSetupGating:
    """The select is created only for positively-identified EG4_HYBRID."""

    @pytest.mark.asyncio
    async def test_hybrid_family_creates_select(self, hass):
        coordinator = _mock_coordinator()
        entry = MagicMock()
        entry.runtime_data = coordinator

        entities = []
        await async_setup_entry(hass, entry, lambda e, **kw: entities.extend(e))

        type_names = [type(e).__name__ for e in entities]
        assert type_names.count("EG4ACChargeTypeSelect") == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("family", [None, "EG4_OFFGRID", "LXP", "bogus"])
    async def test_non_hybrid_family_fails_closed(self, hass, family):
        """No positive EG4_HYBRID identification — no select (#488 convention)."""
        coordinator = _mock_coordinator(family=family)
        entry = MagicMock()
        entry.runtime_data = coordinator

        entities = []
        await async_setup_entry(hass, entry, lambda e, **kw: entities.extend(e))

        assert "EG4ACChargeTypeSelect" not in [type(e).__name__ for e in entities]


# ── current_option / available ───────────────────────────────────────


class TestCurrentOption:
    """Cloud-space values decode to the app's three labels; junk to None."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "Time"),
            (2, "SOC/Volt"),
            (4, "Time+SOC/Volt"),
            ("4", "Time+SOC/Volt"),  # cloud occasionally serves strings
            (1, None),  # the bare protocol-Time field this repo once wrote
            (6, None),  # unverified SOC-mode values render honestly unknown
            ("junk", None),
            (None, None),
            # Bools are pylxpweb's mis-decode shape for this key; False
            # must NOT render as "Time" (int(False) == 0).
            (False, None),
            (True, None),
        ],
    )
    def test_decode(self, value, expected):
        params = {} if value is None else {PARAM_BIT_AC_CHARGE_TYPE: value}
        entity = _select(_mock_coordinator(parameters=params))
        assert entity.current_option == expected

    def test_optimistic_state_wins(self):
        entity = _select(_mock_coordinator(parameters={PARAM_BIT_AC_CHARGE_TYPE: 2}))
        entity._optimistic_state = "Time+SOC/Volt"
        assert entity.current_option == "Time+SOC/Volt"

    def test_options_are_the_app_trio(self):
        entity = _select(_mock_coordinator())
        assert (
            entity.options
            == AC_CHARGE_TYPE_OPTIONS
            == ["Time", "SOC/Volt", "Time+SOC/Volt"]
        )

    def test_unavailable_without_update_success(self):
        coordinator = _mock_coordinator()
        coordinator.last_update_success = False
        assert _select(coordinator).available is False

    def test_available_for_inverter(self):
        assert _select(_mock_coordinator()).available is True


# ── async_select_option write routing ────────────────────────────────


class TestSelectOption:
    """Local writes go through the verified RMW; cloud through the bit param."""

    @pytest.mark.asyncio
    async def test_local_write_routes_to_coordinator_rmw(self):
        coordinator = _mock_coordinator()
        coordinator.has_local_transport = MagicMock(return_value=True)
        entity = _select(coordinator)
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("Time+SOC/Volt")

        coordinator.write_ac_charge_type.assert_awaited_once_with(SERIAL, 4)

    @pytest.mark.asyncio
    async def test_cloud_write_uses_raw_bit_param_not_pylxpweb_helper(self):
        """The cloud path must send the cloud-space value via control_bit_param.

        pylxpweb's set_ac_charge_type() would shift the field down and write
        the wrong value for every non-Time option — pin that it is never
        called.
        """
        coordinator = _mock_coordinator()
        coordinator.has_local_transport = MagicMock(return_value=False)
        control = coordinator.client.api.control
        control.control_bit_param = AsyncMock(return_value=MagicMock(success=True))
        control.set_ac_charge_type = AsyncMock()
        entity = _select(coordinator)
        entity.async_write_ha_state = MagicMock()

        await entity.async_select_option("SOC/Volt")

        control.control_bit_param.assert_awaited_once_with(
            SERIAL, PARAM_BIT_AC_CHARGE_TYPE, 2
        )
        control.set_ac_charge_type.assert_not_called()
        coordinator.refresh_inverter_params_if_linked.assert_awaited_once_with(SERIAL)

    @pytest.mark.asyncio
    async def test_cloud_failure_raises_and_reverts_optimistic(self):
        coordinator = _mock_coordinator(parameters={PARAM_BIT_AC_CHARGE_TYPE: 2})
        coordinator.has_local_transport = MagicMock(return_value=False)
        coordinator.client.api.control.control_bit_param = AsyncMock(
            return_value=MagicMock(success=False)
        )
        entity = _select(coordinator)
        entity.async_write_ha_state = MagicMock()

        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("Time")

        assert entity.current_option == "SOC/Volt"

    @pytest.mark.asyncio
    async def test_invalid_option_rejected(self):
        entity = _select(_mock_coordinator())
        with pytest.raises(HomeAssistantError):
            await entity.async_select_option("SOC")


# ── coordinator.write_ac_charge_type (local RMW) ─────────────────────


def _rmw_harness(transport: MagicMock) -> MagicMock:
    """Mock coordinator self wired to the REAL RMW + write-shell methods."""
    mock_self = MagicMock()
    mock_self.get_local_transport = MagicMock(return_value=transport)
    mock_self.note_parameters_written = MagicMock()
    mock_self._write_with_local_transport = types.MethodType(
        EG4DataUpdateCoordinator._write_with_local_transport, mock_self
    )
    return mock_self


class TestWriteACChargeType:
    """The RMW preserves foreign bits and refuses unverified writes."""

    @pytest.mark.asyncio
    async def test_rmw_preserves_other_bits_and_verifies(self):
        transport = MagicMock()
        transport.is_connected = True
        # Live pre-fix raw: 0x0052 (field 1 + discharge-control/EOD bits).
        transport.read_parameters = AsyncMock(
            side_effect=[{REG_AC_CHARGE_TYPE: 0x0052}, {REG_AC_CHARGE_TYPE: 0x0054}]
        )
        transport.write_parameters = AsyncMock()
        mock_self = _rmw_harness(transport)

        result = await EG4DataUpdateCoordinator.write_ac_charge_type(
            mock_self, SERIAL, 4
        )

        assert result is True
        transport.write_parameters.assert_awaited_once_with(
            {REG_AC_CHARGE_TYPE: 0x0054}
        )
        mock_self.note_parameters_written.assert_called_once_with(
            SERIAL, {PARAM_BIT_AC_CHARGE_TYPE: 4}
        )

    @pytest.mark.asyncio
    async def test_readback_mismatch_raises_without_cache_seed(self):
        """A silently-reverted write (the reg-229 failure mode) must raise."""
        transport = MagicMock()
        transport.is_connected = True
        transport.read_parameters = AsyncMock(
            side_effect=[{REG_AC_CHARGE_TYPE: 0x0052}, {REG_AC_CHARGE_TYPE: 0x0052}]
        )
        transport.write_parameters = AsyncMock()
        mock_self = _rmw_harness(transport)

        with pytest.raises(HomeAssistantError):
            await EG4DataUpdateCoordinator.write_ac_charge_type(mock_self, SERIAL, 4)

        mock_self.note_parameters_written.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_register_refuses_blind_write(self):
        """No pre-read value — the RMW must not guess and clobber bits."""
        transport = MagicMock()
        transport.is_connected = True
        transport.read_parameters = AsyncMock(return_value={})
        transport.write_parameters = AsyncMock()
        mock_self = _rmw_harness(transport)

        with pytest.raises(HomeAssistantError):
            await EG4DataUpdateCoordinator.write_ac_charge_type(mock_self, SERIAL, 4)

        transport.write_parameters.assert_not_called()


# ── LOCAL raw read (coordinator_local) ───────────────────────────────


def _local_read_harness() -> MagicMock:
    mock_self = MagicMock()
    mock_self.data = {"parameters": {}}
    mock_self._read_modbus_parameters = types.MethodType(
        EG4DataUpdateCoordinator._read_modbus_parameters, mock_self
    )
    return mock_self


def _hybrid_device_data() -> dict:
    return {"features": {"inverter_family": INVERTER_FAMILY_EG4_HYBRID}}


class TestLocalRawRead:
    """LOCAL mode reads reg 120 raw and stores the cloud-space field."""

    @pytest.mark.asyncio
    async def test_hybrid_read_decodes_cloud_space(self):
        transport = MagicMock()
        transport.read_named_parameters = AsyncMock(return_value={})
        transport.read_parameters = AsyncMock(return_value={REG_AC_CHARGE_TYPE: 0x0054})
        mock_self = _local_read_harness()

        params, complete = await mock_self._read_modbus_parameters(
            transport, _hybrid_device_data()
        )

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 4
        assert complete is True
        transport.read_parameters.assert_awaited_once_with(REG_AC_CHARGE_TYPE, 1)

    @pytest.mark.asyncio
    async def test_non_hybrid_family_skips_the_read(self):
        transport = MagicMock()
        transport.read_named_parameters = AsyncMock(return_value={})
        transport.read_parameters = AsyncMock()
        mock_self = _local_read_harness()

        params, _ = await mock_self._read_modbus_parameters(
            transport, {"features": {"inverter_family": "EG4_OFFGRID"}}
        )

        assert PARAM_BIT_AC_CHARGE_TYPE not in params
        transport.read_parameters.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_read_carries_forward_without_incomplete(self):
        """A reg-120 failure keeps the last value for THIS key only and must
        not push the whole cycle into the #282 incomplete/early-retry loop."""
        transport = MagicMock()
        transport.read_named_parameters = AsyncMock(return_value={"PARAM": 1})
        transport.read_parameters = AsyncMock(side_effect=TimeoutError("nak"))
        device = types.SimpleNamespace(
            serial_number=SERIAL, transport=transport, transport_link_down=False
        )
        mock_self = _local_read_harness()
        mock_self.data = {"parameters": {SERIAL: {PARAM_BIT_AC_CHARGE_TYPE: 2}}}

        params, complete = await mock_self._read_modbus_parameters(
            transport, _hybrid_device_data(), device
        )

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 2
        assert complete is True

    @pytest.mark.asyncio
    async def test_failed_read_never_carries_a_bool_forward(self):
        """A mis-decode-shaped (bool) cache value is not perpetuated."""
        transport = MagicMock()
        transport.read_named_parameters = AsyncMock(return_value={})
        transport.read_parameters = AsyncMock(side_effect=TimeoutError("nak"))
        device = types.SimpleNamespace(
            serial_number=SERIAL, transport=transport, transport_link_down=False
        )
        mock_self = _local_read_harness()
        mock_self.data = {"parameters": {SERIAL: {PARAM_BIT_AC_CHARGE_TYPE: False}}}

        params, _ = await mock_self._read_modbus_parameters(
            transport, _hybrid_device_data(), device
        )

        assert PARAM_BIT_AC_CHARGE_TYPE not in params

    @pytest.mark.asyncio
    async def test_failed_read_with_no_history_leaves_key_absent(self):
        transport = MagicMock()
        transport.read_named_parameters = AsyncMock(return_value={})
        transport.read_parameters = AsyncMock(side_effect=TimeoutError("nak"))
        mock_self = _local_read_harness()

        params, complete = await mock_self._read_modbus_parameters(
            transport, _hybrid_device_data()
        )

        assert PARAM_BIT_AC_CHARGE_TYPE not in params
        assert complete is True


# ── HYBRID/CLOUD refresh overlay ─────────────────────────────────────


def _overlay_harness() -> MagicMock:
    mock_self = MagicMock()
    mock_self._overlay_ac_charge_type = types.MethodType(
        EG4DataUpdateCoordinator._overlay_ac_charge_type, mock_self
    )
    return mock_self


def _overlay_transport(raw: int | None = 0x0054) -> MagicMock:
    """A transport whose raw reg-120 read returns ``raw`` (None = failure)."""
    transport = MagicMock()
    if raw is None:
        transport.read_parameters = AsyncMock(side_effect=TimeoutError("dead"))
    else:
        transport.read_parameters = AsyncMock(return_value={REG_AC_CHARGE_TYPE: raw})
    return transport


def _overlay_inverter(
    *, params: dict, transport: object | None, link_down: bool = False
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        parameters=params,
        transport=transport,
        transport_link_down=link_down,
    )


class TestRefreshOverlay:
    """Transport-served refreshes get the mis-decoded key corrected."""

    @pytest.mark.asyncio
    async def test_local_served_value_is_replaced(self):
        """pylxpweb's bit-3 decode (here a garbage 0 — colliding with the
        real "Time") is replaced by the raw register's mask nibble. The raw
        read goes through the TRANSPORT: the integration's device objects
        are GenericInverter, which has no get_ac_charge_type (live-verified
        AttributeError on first deployment)."""
        params = {PARAM_BIT_AC_CHARGE_TYPE: 0}
        transport = _overlay_transport(0x0054)
        inverter = _overlay_inverter(params=params, transport=transport)
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 4
        transport.read_parameters.assert_awaited_once_with(REG_AC_CHARGE_TYPE, 1)

    @pytest.mark.asyncio
    async def test_cloud_served_value_is_trusted(self):
        """No transport attached — the server decoded the field correctly."""
        params = {PARAM_BIT_AC_CHARGE_TYPE: 4}
        inverter = _overlay_inverter(params=params, transport=None)
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 4

    @pytest.mark.asyncio
    async def test_link_down_fallback_value_is_trusted(self):
        """Attached-but-dead link means the fetch fell back to HTTP — the
        cloud value is correct and a local re-read would hang."""
        params = {PARAM_BIT_AC_CHARGE_TYPE: 2}
        transport = _overlay_transport(0x0054)
        inverter = _overlay_inverter(params=params, transport=transport, link_down=True)
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 2
        transport.read_parameters.assert_not_called()

    @pytest.mark.asyncio
    async def test_failed_overlay_read_drops_only_the_misdecode_shape(self):
        """Unknown beats garbage: a failed re-read removes a BOOL value —
        pylxpweb's mis-decode shape — since garbage is all a bool can be."""
        params = {PARAM_BIT_AC_CHARGE_TYPE: False}
        inverter = _overlay_inverter(params=params, transport=_overlay_transport(None))
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert PARAM_BIT_AC_CHARGE_TYPE not in params

    @pytest.mark.asyncio
    async def test_failed_overlay_read_keeps_a_carried_good_value(self):
        """Correlated failures: when the fetch itself failed, pylxpweb kept
        its previous parameters dict — the cached int is the carried-forward
        CORRECTED value, and the failed re-read must not discard it."""
        params = {PARAM_BIT_AC_CHARGE_TYPE: 4}
        inverter = _overlay_inverter(params=params, transport=_overlay_transport(None))
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert params[PARAM_BIT_AC_CHARGE_TYPE] == 4

    @pytest.mark.asyncio
    async def test_absent_key_is_untouched(self):
        params: dict = {}
        transport = _overlay_transport(0x0054)
        inverter = _overlay_inverter(params=params, transport=transport)
        mock_self = _overlay_harness()

        await mock_self._overlay_ac_charge_type(SERIAL, inverter)

        assert params == {}
        transport.read_parameters.assert_not_called()

    @pytest.mark.asyncio
    async def test_refresh_publishes_the_corrected_value_end_to_end(self):
        """The overlay is actually WIRED into _refresh_device_parameters.

        Binds the real refresh method (the test_button_entities harness
        pattern) with a transport-attached inverter whose fetch produced
        the bool mis-decode, and asserts the published cache holds the
        corrected cloud-space int — deleting the overlay call from
        _refresh_device_parameters fails this test (it was the one
        unpinned link in the corrective chain, adversarial-review F5).
        """
        from custom_components.eg4_web_monitor.coordinator_mixins import (
            ParameterManagementMixin,
        )

        coordinator = MagicMock()
        coordinator.data = {
            "devices": {SERIAL: {"type": "inverter"}},
            "parameters": {},
        }
        coordinator._refresh_device_parameters = types.MethodType(
            ParameterManagementMixin._refresh_device_parameters, coordinator
        )
        coordinator._overlay_ac_charge_type = types.MethodType(
            ParameterManagementMixin._overlay_ac_charge_type, coordinator
        )

        inverter = MagicMock()
        inverter.transport = _overlay_transport(0x0054)
        inverter.transport_link_down = False
        inverter.refresh = AsyncMock()
        # The default refresh path (include_runtime_data=False) awaits the
        # narrow _fetch_parameters(); the full-refresh path awaits refresh().
        inverter._fetch_parameters = AsyncMock()
        inverter.parameters = {PARAM_BIT_AC_CHARGE_TYPE: False, "OTHER": 1}
        inverter.parameters_complete = True
        coordinator.get_inverter_object = MagicMock(return_value=inverter)

        result = await coordinator._refresh_device_parameters(SERIAL)

        assert result is True
        published = coordinator.data["parameters"][SERIAL]
        assert published[PARAM_BIT_AC_CHARGE_TYPE] == 4
        assert published["OTHER"] == 1


# ── Mode-based availability gating ───────────────────────────────────


class TestACChargeTypeAllows:
    """The shared gate: strict only on known EG4_HYBRID modes, else open."""

    @pytest.mark.parametrize(
        ("value", "time_ok", "threshold_ok"),
        [
            (0, True, False),  # Time
            (2, False, True),  # Volt
            (4, True, True),  # Time+SOC/Volt
            (1, True, True),  # unknown values fail open
            (6, True, True),
            ("junk", True, True),
            (None, True, True),
            # Bools are pylxpweb's mis-decode shape; False must NOT gate
            # as the legitimate "Time" (int(False) == 0) — fail open.
            (False, True, True),
            (True, True, True),
        ],
    )
    def test_hybrid_modes(self, value, time_ok, threshold_ok):
        params = {} if value is None else {PARAM_BIT_AC_CHARGE_TYPE: value}
        coordinator = _mock_coordinator(parameters=params)
        assert (
            ac_charge_type_allows(coordinator, SERIAL, AC_CHARGE_TYPE_TIME_MODES)
            is time_ok
        )
        assert (
            ac_charge_type_allows(coordinator, SERIAL, AC_CHARGE_TYPE_THRESHOLD_MODES)
            is threshold_ok
        )

    def test_non_hybrid_family_always_open(self):
        coordinator = _mock_coordinator(
            family=None, parameters={PARAM_BIT_AC_CHARGE_TYPE: 2}
        )
        assert (
            ac_charge_type_allows(coordinator, SERIAL, AC_CHARGE_TYPE_TIME_MODES)
            is True
        )


class TestEntityGating:
    """Schedule times and the SOC limit follow the selection."""

    def _time_entity(self, coordinator, spec=_AC_CHARGE_SPEC) -> EG4ScheduleTimeEntity:
        return EG4ScheduleTimeEntity(coordinator, SERIAL, spec, 1, is_end=False)

    def _params(self, mode_value) -> dict:
        # Cloud-shaped window-1 start (01:30) plus the mode under test.
        params = {"HOLD_AC_CHARGE_START_HOUR": 1, "HOLD_AC_CHARGE_START_MINUTE": 30}
        if mode_value is not None:
            params[PARAM_BIT_AC_CHARGE_TYPE] = mode_value
        return params

    def test_ac_charge_window_unavailable_in_volt_mode(self):
        coordinator = _mock_coordinator(parameters=self._params(2))
        assert self._time_entity(coordinator).available is False

    @pytest.mark.parametrize("mode_value", [0, 4, None, 6])
    def test_ac_charge_window_available_when_time_relevant_or_unknown(self, mode_value):
        coordinator = _mock_coordinator(parameters=self._params(mode_value))
        assert self._time_entity(coordinator).available is True

    def test_other_schedule_families_ignore_the_mode(self):
        """SOC/Volt mode gates ONLY the AC Charge windows, not e.g. Forced Charge."""
        params = {
            "HOLD_FORCED_CHARGE_START_HOUR": 1,
            "HOLD_FORCED_CHARGE_START_MINUTE": 30,
            PARAM_BIT_AC_CHARGE_TYPE: 2,
        }
        coordinator = _mock_coordinator(parameters=params)
        entity = self._time_entity(coordinator, spec=_FORCED_CHARGE_SPEC)
        assert entity.available is True

    def test_soc_limit_unavailable_in_time_mode(self):
        coordinator = _mock_coordinator(parameters={PARAM_BIT_AC_CHARGE_TYPE: 0})
        entity = ACChargeSOCLimitNumber(coordinator, SERIAL)
        assert entity.available is False

    @pytest.mark.parametrize("mode_value", [2, 4, None])
    def test_soc_limit_available_when_threshold_relevant_or_unknown(self, mode_value):
        params = {} if mode_value is None else {PARAM_BIT_AC_CHARGE_TYPE: mode_value}
        coordinator = _mock_coordinator(parameters=params)
        entity = ACChargeSOCLimitNumber(coordinator, SERIAL)
        assert entity.available is True

    def test_soc_limit_ungated_off_family(self):
        """Grid-tied non-hybrid keeps the entity available in any mode."""
        coordinator = _mock_coordinator(
            family=None, parameters={PARAM_BIT_AC_CHARGE_TYPE: 0}
        )
        entity = ACChargeSOCLimitNumber(coordinator, SERIAL)
        assert entity.available is True

    def _voltage_entity(self, coordinator, key: str) -> EG4VoltageNumber:
        spec = next(s for s in VOLTAGE_NUMBER_SPECS if s.key == key)
        return EG4VoltageNumber(coordinator, SERIAL, spec)

    @pytest.mark.parametrize(
        "key", ["ac_charge_start_voltage", "ac_charge_end_voltage"]
    )
    def test_ac_charge_voltage_pair_unavailable_in_time_mode(self, key):
        """The voltage thresholds gate with their sibling SOC limit — the
        whole 158-161 threshold side is ignored in Time mode."""
        coordinator = _mock_coordinator(parameters={PARAM_BIT_AC_CHARGE_TYPE: 0})
        assert self._voltage_entity(coordinator, key).available is False

    @pytest.mark.parametrize("mode_value", [2, 4, None])
    def test_ac_charge_voltage_available_when_threshold_relevant_or_unknown(
        self, mode_value
    ):
        params = {} if mode_value is None else {PARAM_BIT_AC_CHARGE_TYPE: mode_value}
        coordinator = _mock_coordinator(parameters=params)
        entity = self._voltage_entity(coordinator, "ac_charge_start_voltage")
        assert entity.available is True

    def test_other_voltage_specs_ignore_the_mode(self):
        """Time mode gates ONLY the AC charge pair, not e.g. the cutoffs."""
        coordinator = _mock_coordinator(parameters={PARAM_BIT_AC_CHARGE_TYPE: 0})
        entity = self._voltage_entity(coordinator, "on_grid_cutoff_voltage")
        assert entity.available is True

"""GridBOSS smart port function switches (per-port enable controls).

The GridBOSS portal settings page carries three per-port toggles next to the
Smart Port mode selector the integration already exposes (select.py):
*Smart Load Enable* and *Grid Always On* for ports in Smart Load mode, and
*AC Couple Enable* for ports in AC Couple mode. All twelve flags live in
holding register 229 — layout and the three-system raw↔named pin evidence in
const/modbus.py.

Requires a configured local transport (LOCAL, or HYBRID with an attached
dongle): the enable states are read from register 229 on the MID refresh
cycle into the parameter store, and local writes are a locked
read-modify-write of the same register
(:meth:`EG4DataUpdateCoordinator.write_midbox_smart_port_function`). In
HYBRID mode a down local link falls back to pylxpweb's cloud
functionControl wrappers (enable_smart_load / enable_ac_couple /
set_smart_load_grid_on) through the shared write envelope. Pure-cloud
entries get no switches yet: the cloud can only read these params through a
multi-register range read the integration does not perform — a documented
follow-up once pylxpweb grows a midbox settings getter.

Availability follows the port's runtime mode (the same
``smart_port{n}_status`` source the mode select uses): a switch whose
function does not apply to the current mode — or whose port status is
unknown this cycle (#217 placeholder semantics) — shows unavailable, and an
enable state that has never been read shows unavailable rather than a fake
OFF (the #471 AC Couple switch doctrine). The mode gating also mirrors the
firmware: live write tests (const/modbus.py) show a mode-consistent enable
bit persists while a flag for a mode the port is not in is silently
reverted, so the switches only ever issue writes the device accepts.
"""

import logging
from typing import Any

from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .base_entity import EG4BaseSwitch
from .const import DEVICE_TYPE_GRIDBOSS
from .coordinator import EG4DataUpdateCoordinator
from .utils import async_write_with_cloud_fallback

_LOGGER = logging.getLogger(__name__)

# Port status labels (sensor values) each function applies to.
_STATUS_SMART_LOAD = "smart_load"
_STATUS_AC_COUPLE = "ac_couple"

# (entity_key suffix, name suffix, cloud param prefix, required status, icon)
_SMART_PORT_FUNCTION_SPECS: tuple[tuple[str, str, str, str, str], ...] = (
    (
        "smart_load_enabled",
        "Smart Load",
        "FUNC_SMART_LOAD_EN",
        _STATUS_SMART_LOAD,
        "mdi:power-plug-outline",
    ),
    (
        "grid_always_on",
        "Grid Always On",
        "FUNC_SMART_LOAD_GRID_ON",
        _STATUS_SMART_LOAD,
        "mdi:transmission-tower",
    ),
    (
        "ac_couple_enabled",
        "AC Couple",
        "FUNC_AC_COUPLE_EN",
        _STATUS_AC_COUPLE,
        "mdi:solar-power-variant",
    ),
)


def create_smart_port_switches(
    coordinator: EG4DataUpdateCoordinator,
    serial: str,
) -> list["EG4SmartPortFunctionSwitch"]:
    """Build the per-port function switches for one GridBOSS device.

    All four ports get all three switches — a port's mode can change at any
    time through the mode select, and availability gating (not entity
    existence) tracks the current mode. Skipped entirely without a
    configured local transport (no state source — module docstring).
    """
    if not coordinator.has_configured_local_transport(serial):
        _LOGGER.debug(
            "Skipping smart port switches for GridBOSS %s: no local "
            "transport configured (cloud-only state read is a follow-up)",
            serial,
        )
        return []
    return [
        EG4SmartPortFunctionSwitch(coordinator, serial, port, *spec)
        for port in range(1, 5)
        for spec in _SMART_PORT_FUNCTION_SPECS
    ]


class EG4SmartPortFunctionSwitch(EG4BaseSwitch):
    """One per-port GridBOSS smart port function enable switch."""

    def __init__(
        self,
        coordinator: EG4DataUpdateCoordinator,
        serial: str,
        port: int,
        entity_suffix: str,
        name_suffix: str,
        param_prefix: str,
        required_status: str,
        icon: str,
    ) -> None:
        """Initialize the switch for one (port, function) pair."""
        super().__init__(
            coordinator=coordinator,
            serial=serial,
            entity_key=f"smart_port{port}_{entity_suffix}",
            name=f"Smart Port {port} {name_suffix}",
            icon=icon,
            entity_category=EntityCategory.CONFIG,
        )
        self._port = port
        self._param_prefix = param_prefix
        self._param_name = f"{param_prefix}_{port}"
        self._required_status = required_status

    @property
    def _port_status(self) -> str | None:
        """The port's runtime mode label, or None when unknown this cycle."""
        sensors = self._device_data.get("sensors") or {}
        status = sensors.get(f"smart_port{self._port}_status")
        return status if isinstance(status, str) else None

    @property
    def _stored_enabled(self) -> bool | None:
        """The function state from the parameter store, None if never read."""
        value = self._parameter_data.get(self._param_name)
        return value if isinstance(value, bool) else None

    @property
    def available(self) -> bool:
        """Available while the port is in this function's mode with known state."""
        if not self.coordinator.last_update_success:
            return False
        if self._device_data.get("type") != DEVICE_TYPE_GRIDBOSS:
            return False
        if self._port_status != self._required_status:
            return False
        if self._optimistic_state is not None:
            return True
        return self._stored_enabled is not None

    @property
    def is_on(self) -> bool | None:
        """Return the function's enable state."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self._stored_enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the port function."""
        await self._async_set_function(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable the port function."""
        await self._async_set_function(False)

    async def _async_set_function(self, enabled: bool) -> None:
        """Write the function bit locally, falling back to the cloud API.

        The local path's post-write verify read seeds the parameter store
        with device truth, so no full refresh is scheduled (the #471 AC
        Couple switch precedent); the cloud fallback path seeds the store
        through the write envelope's ``local_values``.
        """
        action = f"smart port {self._port} {self._param_name} to {enabled}"
        _LOGGER.info("Setting %s for %s", action, self._serial)
        self._optimistic_state = enabled
        self.async_write_ha_state()

        async def _local_write() -> None:
            await self.coordinator.write_midbox_smart_port_function(
                self._serial, self._param_prefix, self._port, enabled
            )

        async def _cloud_write() -> None:
            control = self.coordinator.require_client().api.control
            if self._param_prefix == "FUNC_SMART_LOAD_EN":
                result = await (
                    control.enable_smart_load(self._serial, self._port)
                    if enabled
                    else control.disable_smart_load(self._serial, self._port)
                )
            elif self._param_prefix == "FUNC_AC_COUPLE_EN":
                result = await (
                    control.enable_ac_couple(self._serial, self._port)
                    if enabled
                    else control.disable_ac_couple(self._serial, self._port)
                )
            else:
                result = await control.set_smart_load_grid_on(
                    self._serial, self._port, enabled
                )
            if not result.success:
                raise HomeAssistantError(f"Failed to set {action} for {self._serial}")

        try:
            await async_write_with_cloud_fallback(
                self.coordinator,
                self._serial,
                action,
                local_write=_local_write,
                cloud_write=_cloud_write,
                local_values={self._param_name: enabled},
            )
        except Exception:
            self._optimistic_state = None
            self.async_write_ha_state()
            raise

        self._optimistic_state = None
        self.async_write_ha_state()

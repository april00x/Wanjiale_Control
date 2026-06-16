"""万家乐开关平台。"""
from __future__ import annotations

from typing import Any, List

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._entity import WanjialeEntity
from .api import (
    WanjialeApi,
    WanjialeDevice,
    WanjialeDisinfect,
    WanjialeStove,
    WanjialeWaterHeater,
)
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    devices: List[Any] = [
        WanjialeSwitchEntity(dev, coordinator)
        for dev in api.devices
        if isinstance(dev, (WanjialeStove, WanjialeDisinfect))
    ]
    # 热水器待机开关
    for dev in api.devices:
        if isinstance(dev, WanjialeWaterHeater):
            devices.append(WanjialePowerSwitch(dev, coordinator))
    async_add_entities(devices, True)


class WanjialeSwitchEntity(WanjialeEntity, SwitchEntity):
    """通用开关实体（灶具/消毒柜）。"""

    def __init__(self, device: WanjialeDevice, coordinator) -> None:
        super().__init__(device, coordinator)
        self._attr_name = f"{device.name} 开关"

    @property
    def unique_id(self) -> str:
        return f"{self._device.unique_id()}-switch"

    @property
    def is_on(self) -> bool:
        return bool(getattr(self._device, "is_power_on", False))

    def turn_on(self, **kwargs: Any) -> None:
        self._device.turn_on()

    def turn_off(self, **kwargs: Any) -> None:
        self._device.turn_off()


class WanjialePowerSwitch(WanjialeEntity, SwitchEntity):
    """热水器待机开关。

    对应 Java PostMessage dvid="4" + opt 消息。
    dwtype=2 开关型：0=关机, 1=开机。
    """

    _wh: WanjialeWaterHeater
    _attr_icon = "mdi:power"

    def __init__(self, device: WanjialeWaterHeater, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._attr_name = f"{device.name} 待机"

    @property
    def unique_id(self) -> str:
        return f"{self._device.unique_id()}-power"

    @property
    def is_on(self) -> bool:
        return bool(self._wh.is_power_on)

    def turn_on(self, **kwargs: Any) -> None:
        self._wh.turn_on()
        self.schedule_update_ha_state()

    def turn_off(self, **kwargs: Any) -> None:
        self._wh.turn_off()
        self.schedule_update_ha_state()

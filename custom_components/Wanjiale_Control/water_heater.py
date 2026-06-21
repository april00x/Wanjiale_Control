"""万家乐热水器实体平台。"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._entity import WanjialeEntity
from .api import WanjialeApi, WanjialeWaterHeater
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

MIN_TEMP = 30.0
MAX_TEMP = 60.0

_MODE_MAP = {
    WanjialeWaterHeater.MODE_COMFORT: "舒适浴",
    WanjialeWaterHeater.MODE_SMART: "随温感",
    WanjialeWaterHeater.MODE_KITCHEN: "厨房洗",
    WanjialeWaterHeater.MODE_ECO: "ECO",
    WanjialeWaterHeater.MODE_SUR: "SUR",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    devices: List[Any] = [
        WanjialeWaterHeaterEntity(dev, coordinator)
        for dev in api.devices
        if isinstance(dev, WanjialeWaterHeater)
    ]
    _LOGGER.info("创建 %d 个热水器实体: %s", len(devices), [d.name for d in devices])
    async_add_entities(devices, True)


class WanjialeWaterHeaterEntity(WanjialeEntity, WaterHeaterEntity):
    """热水器实体。

    控制方法使用同步签名，HA 会自动包装到 executor 线程，
    这样底层的同步 socket 不会阻塞事件循环。
    """

    _attr_supported_features = (
        WaterHeaterEntityFeature.TARGET_TEMPERATURE
        | WaterHeaterEntityFeature.OPERATION_MODE
        | WaterHeaterEntityFeature.ON_OFF
    )
    _attr_min_temp = MIN_TEMP
    _attr_max_temp = MAX_TEMP
    _attr_target_temperature_step = 1
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_operation_list = list(_MODE_MAP.values())
    _attr_icon = "mdi:water-boiler"

    def __init__(self, device, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh: WanjialeWaterHeater = device

    @property
    def name(self) -> str:
        return "调温"

    # --------------------------------------------------------------
    # 状态
    # --------------------------------------------------------------
    @property
    def is_on(self) -> Optional[bool]:
        return self._wh.is_power_on

    @property
    def current_temperature(self) -> Optional[float]:
        return self._wh.current_temperature

    @property
    def target_temperature(self) -> Optional[float]:
        return self._wh.target_temperature

    @property
    def current_operation(self) -> Optional[str]:
        if not self._wh.current_mode:
            return None
        return _MODE_MAP.get(self._wh.current_mode)

    # --------------------------------------------------------------
    # 控制（同步方法 -> HA 自动包装到 executor）
    # --------------------------------------------------------------
    def turn_on(self, **kwargs: Any) -> None:
        self._wh.turn_on()
        self.schedule_update_ha_state()
        self._request_refresh_soon()

    def turn_off(self, **kwargs: Any) -> None:
        self._wh.turn_off()
        self.schedule_update_ha_state()
        self._request_refresh_soon()

    def set_temperature(self, **kwargs: Any) -> None:
        temp = kwargs.get("temperature")
        if temp is None:
            return
        self._wh.set_temperature(int(float(temp)))
        self.schedule_update_ha_state()
        self._request_refresh_soon()

    def set_operation_mode(self, operation_mode: str) -> None:
        internal = None
        for k, v in _MODE_MAP.items():
            if v == operation_mode:
                internal = k
                break
        if internal is None:
            return
        self._wh.set_mode(internal)
        self.schedule_update_ha_state()
        self._request_refresh_soon()

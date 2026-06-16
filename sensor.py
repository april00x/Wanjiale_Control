"""万家乐通用传感器平台。

- 对 WanjialeDevice（基类，未分派到具体子类的设备）暴露为一个
  "在线状态" + "原始属性" 的 sensor；
- 对其他未在 HA 中找到合适平台的子类（如油烟机预留）也可以在此
  以额外传感器形式暴露业务属性。
"""
from __future__ import annotations

from typing import Any, List

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from ._entity import WanjialeEntity
from .api import WanjialeApi, WanjialeDevice, WanjialeWaterHeater
from .const import DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[Any] = []
    for dev in api.devices:
        # 非水热/已有专门平台的设备，给一个在线状态 sensor
        if isinstance(dev, WanjialeWaterHeater):
            # 热水器单独添加当前温度 sensor
            entities.append(WanjialeTemperatureSensor(dev, coordinator))
            continue
        entities.append(WanjialeOnlineSensor(dev, coordinator))

    async_add_entities(entities, True)


class WanjialeOnlineSensor(WanjialeEntity, SensorEntity):
    """通用设备的在线状态传感器。"""

    _attr_icon = "mdi:lan-connect"

    def __init__(self, device, coordinator) -> None:
        super().__init__(device, coordinator)
        self._attr_name = f"{device.name} 状态"

    @property
    def unique_id(self) -> str:
        return f"{self._device.unique_id()}-status"

    @property
    def native_value(self):
        return self._device.online


class WanjialeTemperatureSensor(WanjialeEntity, SensorEntity):
    """热水器当前温度 sensor。"""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = "°C"
    _attr_suggested_display_precision = 1
    _attr_icon = "mdi:thermometer"

    def __init__(self, device: WanjialeDevice, coordinator) -> None:
        super().__init__(device, coordinator)
        self._attr_name = f"{device.name} 当前温度"

    @property
    def unique_id(self) -> str:
        return f"{self._device.unique_id()}-current_temp"

    @property
    def native_value(self):
        # 优先使用设备类自身解析的 current_temperature
        if isinstance(self._device, WanjialeWaterHeater) and self._device.current_temperature is not None:
            return self._device.current_temperature
        # 回退到 attributes 中的原始值
        return self._device.attributes.get("28")

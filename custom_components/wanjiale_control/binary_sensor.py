"""万家乐二元状态实体（binary_sensor 平台）。

spec 表由品类目录提供（`registry.platform_specs`），本模块只负责把 spec 渲染成
HA 实体，与具体品类无关。

只读平台是设备级的：所有角色共用同一份上报状态 `parsed`，按角色复制只会得到重复实体。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import BinarySensorSpec

_LOGGER = logging.getLogger(__name__)


def _device_class(name: Optional[str]) -> Optional[BinarySensorDeviceClass]:
    """spec 里的字符串转为 HA 枚举（HA 的枚举是 str 子类，转换无损）。"""
    if not name:
        return None
    try:
        return BinarySensorDeviceClass(name)
    except ValueError:
        _LOGGER.warning("未知的 binary_sensor 设备类：%s", name)
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeBinarySensor] = []
    for dev in api.devices:
        for spec in registry.platform_specs(dev, "binary_sensor"):
            if dev.has_point(spec.dvid):
                entities.append(WanjialeBinarySensor(dev, coordinator, spec))
    _LOGGER.debug("创建 %d 个二元状态实体", len(entities))
    async_add_entities(entities, True)


class WanjialeBinarySensor(WanjialeEntity, BinarySensorEntity):
    """由 `BinarySensorSpec` 驱动的二元状态实体。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        spec: BinarySensorSpec,
    ) -> None:
        super().__init__(device, coordinator)
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_device_class = _device_class(spec.device_class)

    @property
    def is_on(self) -> Optional[bool]:
        value = self._device.parsed.get(self._spec.value_key)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self):
        """输出原始点位值，便于核对判据是否正确（报警类点位语义未获官方明示）。"""
        if not self._spec.raw_key:
            return None
        raw = self._device.parsed.get(self._spec.raw_key)
        if raw is None:
            return None
        return {f"原始值(dvid{self._spec.dvid})": raw}

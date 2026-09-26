"""万家乐数值型功能实体（number 平台）。

spec 表由品类目录提供（`registry.platform_specs`），本模块只负责把 spec 渲染成
HA 实体，与具体品类无关。离散档位类功能放在 select 平台，避免下发非法值。

控制类平台是角色级的：spec 的 `method` 在角色上解析。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import NumberSpec

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeFeatureNumber] = []
    for dev in api.devices:
        for spec in registry.platform_specs(dev, "number"):
            if not dev.has_feature(spec.feature):
                continue
            for role in dev.role_views():
                if not hasattr(role, spec.method):
                    continue
                entities.append(WanjialeFeatureNumber(dev, coordinator, role, spec))
    _LOGGER.debug("创建 %d 个数值实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureNumber(WanjialeEntity, NumberEntity):
    """由 `NumberSpec` 驱动的通用数值实体。"""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        role: Any,
        spec: NumberSpec,
    ) -> None:
        super().__init__(device, coordinator, role)
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_native_min_value = spec.min_value
        self._attr_native_max_value = spec.max_value
        self._attr_native_step = spec.step
        self._attr_native_unit_of_measurement = spec.unit

    @property
    def native_value(self) -> Optional[float]:
        raw = self._device.parsed.get(self._spec.state_key)
        if raw is None:
            return None
        return float(raw) * self._spec.scale

    def set_native_value(self, value: float) -> None:
        device_value = int(round(value / self._spec.scale))
        self._control(getattr(self.role, self._spec.method), device_value)

    @property
    def extra_state_attributes(self):
        # 设备侧原始值，便于排障
        return {f"设备原始值({self._spec.state_key})": self._device.parsed.get(self._spec.state_key)}

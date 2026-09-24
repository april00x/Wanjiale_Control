"""万家乐动作型功能实体（button 平台）。

这些功能在协议层是「一次性动作」，没有可读的开关状态，用 button 承载最贴合语义：
  点动循环（dvid1=17，单次触发）
  累计用水量清零 / 累计用气量清零（dvid1=5，两种取值区分水/气）
  清除预约冲突（dvid245=0）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN
from .entity import WanjialeEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ButtonSpec:
    key: str
    name: str
    icon: str
    method: str
    feature: Optional[str] = None       # 依赖的能力名；None 表示按点位判断
    require_point: Optional[str] = None  # 需要设备上报过该点位才创建


_SPECS: tuple[ButtonSpec, ...] = (
    ButtonSpec("motion", "点动循环", "mdi:gesture-tap", "set_motion", feature="点动循环"),
    ButtonSpec("reset_water", "累计用水量清零", "mdi:water-off-outline", "reset_water_total", feature="水量/气量清零"),
    ButtonSpec("reset_gas", "累计用气量清零", "mdi:fire-off", "reset_gas_total", feature="水量/气量清零"),
    ButtonSpec("clear_conflict", "清除预约冲突", "mdi:calendar-remove-outline", "clear_reserve_conflict", require_point="245"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeFeatureButton] = []
    for dev in api.devices:
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if spec.feature and not dev.has_feature(spec.feature):
                continue
            if spec.require_point and not dev.has_point(spec.require_point):
                continue
            entities.append(WanjialeFeatureButton(dev, coordinator, spec))
    _LOGGER.debug("创建 %d 个动作实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureButton(WanjialeEntity, ButtonEntity):
    """由 ButtonSpec 驱动的通用动作实体。"""

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: ButtonSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon

    def press(self) -> None:
        self._control(getattr(self._wh, self._spec.method))

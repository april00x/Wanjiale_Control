"""万家乐开关型功能实体（switch 平台）。

以下功能都是「按一下开/关」，全部是 dvid1 + dvid2 复合命令：
  零冷水/即热(dvid1=14)、增压(7)、全天循环(27)、UV杀菌(6)、巡航杀菌(dvid251)、
  预约(15)、冷气泡水(29)。
每个功能是否创建，由 capabilities 解析出的设备功能集合决定。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN
from .entity import WanjialeEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SwitchSpec:
    """一个开关功能到设备能力的映射。"""

    feature: str            # 能力名（与 features_001.FEATURES 对应）
    key: str                # unique_id 短键
    name: str               # 实体名
    icon: str
    method: str             # WanjialeGasWaterHeater 上的控制方法名
    state_key: Optional[str] = None   # parsed 中的状态键；None 表示状态不可靠
    assumed: bool = False   # 状态位未经验证时置 True，UI 显示为可切换按钮


# 状态位定义见 features_001 的位域部分；冷气泡水状态位不可靠，不做状态判断
_SPECS: tuple[SwitchSpec, ...] = (
    SwitchSpec("零冷水/即热", "instant_heat", "零冷水/即热", "mdi:water-sync", "set_instant_heat", "instant_heat_on"),
    SwitchSpec("增压", "boost", "增压", "mdi:water-pump", "set_boost", "boost_on"),
    SwitchSpec("全天循环", "all_day", "全天循环", "mdi:calendar-clock", "set_all_day", "all_day"),
    SwitchSpec("UV杀菌", "uv", "UV杀菌", "mdi:lightbulb-on-outline", "set_uv", "uv_on"),
    SwitchSpec("巡航杀菌", "sterilize", "巡航杀菌", "mdi:shield-sun-outline", "set_sterilize", "sterilizing"),
    SwitchSpec("预约", "reserve", "预约", "mdi:calendar-check-outline", "set_reserve", "reserve_on"),
    SwitchSpec("冷气泡水", "sparkling", "冷气泡水", "mdi:soda-water", "set_sparkling", None, True),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeFeatureSwitch] = []
    for dev in api.devices:
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if dev.has_feature(spec.feature):
                entities.append(WanjialeFeatureSwitch(dev, coordinator, spec))
    _LOGGER.debug("创建 %d 个功能开关实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureSwitch(WanjialeEntity, SwitchEntity):
    """由 SwitchSpec 驱动的通用功能开关。"""

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: SwitchSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_assumed_state = spec.assumed
        # 设备未上报该状态位时的兜底值（由最近一次控制结果决定）
        self._local_state: Optional[bool] = None

    @property
    def is_on(self) -> Optional[bool]:
        if self._spec.state_key:
            value = self._wh.parsed.get(self._spec.state_key)
            if value is not None:
                return bool(value)
        return self._local_state

    def turn_on(self, **kwargs: Any) -> None:
        self._control(self._apply, True)

    def turn_off(self, **kwargs: Any) -> None:
        self._control(self._apply, False)

    def _apply(self, on: bool) -> None:
        getattr(self._wh, self._spec.method)(on)
        if on:
            self._local_state = True
        else:
            self._local_state = False

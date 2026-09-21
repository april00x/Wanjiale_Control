"""万家乐枚举型功能实体（select 平台）。

这三个功能在设备侧都是「离散档位」，用 select 承载可从根本上避免非法值：
  保温时长（dvid1=10，档位 → ×10 分钟）
  循环时长（dvid1=26，0=全循环 / 1=自学习 / 2~15 分钟）
  厨房定时（dvid1=29，30/60/120/180 分钟）
档位取值表定义在 features_001.py，本模块只做 UI 名称映射。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import features_001 as f001
from ._entity import WanjialeEntity
from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def _circulation_options() -> Dict[int, str]:
    """循环时长档位 → 文案。"""
    options = dict(f001.CIRCULATION_SPECIAL)
    for level in range(2, 16):
        options[level] = f"{level}分钟"
    return {k: options[k] for k in sorted(options)}


@dataclass(frozen=True)
class SelectSpec:
    feature: str
    key: str
    name: str
    icon: str
    method: str
    state_key: str
    options: Dict[int, str]   # 档位 → 文案


_SPECS: tuple[SelectSpec, ...] = (
    SelectSpec(
        "保温时长", "keep_warm", "保温时长", "mdi:thermometer-chevron-up", "set_keep_warm",
        "keep_warm", dict(f001.KEEP_WARM_OPTIONS),
    ),
    SelectSpec(
        "循环时长", "circulation", "循环时长", "mdi:timer-sync-outline", "set_circulation",
        "circulation", _circulation_options(),
    ),
    SelectSpec(
        "厨房洗/定时", "kitchen_timer", "厨房定时", "mdi:stove", "set_kitchen_timer",
        "kitchen_timer", {k: f"{v} 分钟" for k, v in sorted(f001.KITCHEN_TIMER_OPTIONS.items())},
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeFeatureSelect] = []
    for dev in api.devices:
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if dev.has_feature(spec.feature):
                entities.append(WanjialeFeatureSelect(dev, coordinator, spec))
    _LOGGER.info("创建 %d 个枚举实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureSelect(WanjialeEntity, SelectEntity):
    """由 SelectSpec 驱动的通用枚举实体。"""

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: SelectSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_options = list(spec.options.values())

    @property
    def current_option(self) -> str | None:
        raw = self._wh.parsed.get(self._spec.state_key)
        if raw is None:
            return None
        return self._spec.options.get(int(raw))

    def select_option(self, option: str) -> None:
        for level, name in self._spec.options.items():
            if name == option:
                self._control(getattr(self._wh, self._spec.method), level)
                return
        raise HomeAssistantError(f"未知档位：{option}")

    @property
    def extra_state_attributes(self):
        return {f"设备原始档位({self._spec.state_key})": self._wh.parsed.get(self._spec.state_key)}

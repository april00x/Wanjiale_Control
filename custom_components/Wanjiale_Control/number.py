"""万家乐数值型功能实体（number 平台）。

回差温度（dvid1=11）、浴缸注水量（dvid1=4 的 byte0，设备单位为 10L，对外按 L 呈现）。
离散档位的功能（保温时长/循环时长/厨房定时）见 select.py，避免出现非法档位。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import features_001 as f001
from ._entity import WanjialeEntity
from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class NumberSpec:
    feature: str            # 能力名
    key: str
    name: str
    icon: str
    method: str             # 设备控制方法名
    state_key: str          # parsed 中的状态键
    min_value: float
    max_value: float
    step: float
    unit: Optional[str] = None
    scale: int = 1          # 设备值 × scale = 对外值（浴缸注水量为 10L 单位）


_SPECS: tuple[NumberSpec, ...] = (
    NumberSpec(
        "回差温度", "diff_temp", "回差温度", "mdi:thermometer-lines", "set_diff_temp",
        "diff_temp", f001.DIFF_TEMP_MIN, f001.DIFF_TEMP_MAX, 1, "°C",
    ),
    NumberSpec(
        "浴缸注水量", "bath_volume", "浴缸注水量", "mdi:bathtub-outline", "set_bath_volume",
        "target_volume",
        f001.BATH_VOLUME_MIN * 10, f001.BATH_VOLUME_MAX * 10, f001.BATH_VOLUME_STEP * 10, "L",
        scale=10,
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

    entities: List[WanjialeFeatureNumber] = []
    for dev in api.devices:
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if dev.has_feature(spec.feature):
                entities.append(WanjialeFeatureNumber(dev, coordinator, spec))
    _LOGGER.info("创建 %d 个数值实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureNumber(WanjialeEntity, NumberEntity):
    """由 NumberSpec 驱动的通用数值实体。"""

    _attr_mode = NumberMode.BOX

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: NumberSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
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
        raw = self._wh.parsed.get(self._spec.state_key)
        if raw is None:
            return None
        return float(raw) * self._spec.scale

    def set_native_value(self, value: float) -> None:
        device_value = int(round(value / self._spec.scale))
        self._control(getattr(self._wh, self._spec.method), device_value)

    @property
    def extra_state_attributes(self):
        # 设备侧原始值，便于排障
        return {f"设备原始值({self._spec.state_key})": self._wh.parsed.get(self._spec.state_key)}

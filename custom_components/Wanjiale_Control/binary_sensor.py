"""万家乐二元状态实体（binary_sensor 平台）。

覆盖设备运行态与报警位：
  加热中 / 有水流（dvid10 位域）、水泵运行（dvid59 bit1）、
  放水报警(241) / CO 报警(242) / 即热报警(243) / 预约加热中(250)。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import features_001 as f001
from ._entity import WanjialeEntity
from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class BinarySensorSpec:
    key: str
    name: str
    dvid: str
    value_key: str
    icon: str
    device_class: Optional[BinarySensorDeviceClass] = None
    raw_key: Optional[str] = None   # 对应 parsed 中的原始值键，作为属性输出便于核对


_SPECS: tuple[BinarySensorSpec, ...] = (
    BinarySensorSpec("heating", "加热中", f001.DVID_STATUS, "heating",
                     "mdi:fire", BinarySensorDeviceClass.RUNNING),
    BinarySensorSpec("water_flowing", "有水流", f001.DVID_STATUS, "water_flowing",
                     "mdi:water-pump", BinarySensorDeviceClass.RUNNING),
    BinarySensorSpec("pump_running", "水泵运行", f001.DVID_PUMP, "pump_running",
                     "mdi:pump", BinarySensorDeviceClass.RUNNING),
    BinarySensorSpec("motion_running", "水控运行中", f001.DVID_MOTION, "motion_running",
                     "mdi:autorenew", BinarySensorDeviceClass.RUNNING),
    BinarySensorSpec("bath_fill_done", "浴缸注水完成", f001.DVID_DRAIN, "bath_fill_done",
                     "mdi:bathtub-outline", BinarySensorDeviceClass.RUNNING),
    # 241 在字典里同时有「放水报警」与「放水完成」两种叫法，后者是正常事件，
    # 因此不给 PROBLEM 设备类，避免把正常事件显示成"问题/异常"。
    BinarySensorSpec("drain_alarm", "放水状态", f001.DVID_DRAIN_ALARM, "drain_alarm",
                     "mdi:water-check-outline", None, "drain_alarm_raw"),
    BinarySensorSpec("instant_heat_alarm", "即热报警", f001.DVID_HEAT_ALARM, "instant_heat_alarm",
                     "mdi:alert-outline", BinarySensorDeviceClass.PROBLEM, "instant_heat_alarm_raw"),
    BinarySensorSpec("reserve_heating", "预约加热中", f001.DVID_RESERVE_HEATING, "reserve_heating",
                     "mdi:calendar-clock", BinarySensorDeviceClass.RUNNING),
    BinarySensorSpec("reserve_conflict", "预约时段冲突", f001.DVID_RESERVE_CONFLICT, "reserve_conflict",
                     "mdi:calendar-alert", BinarySensorDeviceClass.PROBLEM),
)


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
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if dev.has_point(spec.dvid):
                entities.append(WanjialeBinarySensor(dev, coordinator, spec))
    _LOGGER.info("创建 %d 个二元状态实体", len(entities))
    async_add_entities(entities, True)


class WanjialeBinarySensor(WanjialeEntity, BinarySensorEntity):
    """由 BinarySensorSpec 驱动的二元状态实体。"""

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: BinarySensorSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_device_class = spec.device_class

    @property
    def is_on(self) -> Optional[bool]:
        value = self._wh.parsed.get(self._spec.value_key)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self):
        """输出原始点位值，便于核对判据是否正确（报警类点位语义未获官方明示）。"""
        if not self._spec.raw_key:
            return None
        raw = self._wh.parsed.get(self._spec.raw_key)
        if raw is None:
            return None
        return {f"原始值(dvid{self._spec.dvid})": raw}

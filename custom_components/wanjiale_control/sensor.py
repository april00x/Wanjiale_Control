"""万家乐只读状态实体（sensor 平台）。

覆盖状态显示与用量统计两块能力。
每个传感器只在设备确实上报过对应点位（或命中机型基线点位）时才创建，
避免给不支持的机型堆一批永远 unknown 的实体。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import features_001 as f001
from .api import WanjialeApi, WanjialeGasWaterHeater
from .capabilities import fault_info
from .const import DOMAIN
from .entity import WanjialeEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class SensorSpec:
    key: str
    name: str
    dvid: str                      # 触发创建的点位
    value_key: str                 # parsed 中的取值键
    icon: str
    unit: Optional[str] = None
    device_class: Optional[SensorDeviceClass] = None
    precision: Optional[int] = None
    diagnostic: bool = False


_SPECS: tuple[SensorSpec, ...] = (
    SensorSpec("current_temp", "当前水温", f001.DVID_CURRENT_TEMP, "current_temp",
               "mdi:thermometer-water", UnitOfTemperature.CELSIUS,
               SensorDeviceClass.TEMPERATURE, 1),
    SensorSpec("target_temp_raw", "设定温度(原始)", f001.DVID_TARGET_TEMP, "target_temp_raw",
               "mdi:thermometer-chevron-up", UnitOfTemperature.CELSIUS,
               SensorDeviceClass.TEMPERATURE, diagnostic=True),
    SensorSpec("water_flow", "当前水流量", f001.DVID_WATER_FLOW, "water_flow",
               "mdi:water-outline", None, None, diagnostic=True),
    SensorSpec("inlet_temp", "进水温度", f001.DVID_INLET_TEMP, "inlet_temp",
               "mdi:thermometer-low", UnitOfTemperature.CELSIUS,
               SensorDeviceClass.TEMPERATURE, 1),
    SensorSpec("env_offset", "环境修正值", f001.DVID_WATER_PRESSURE, "env_offset",
               "mdi:tune-vertical", None, None, diagnostic=True),
    SensorSpec("priority", "优先权", f001.DVID_PRIORITY, "priority",
               "mdi:account-key-outline", None, None, diagnostic=True),
    SensorSpec("fa_type", "FA变升类型", f001.DVID_FA_TYPE, "fa_type",
               "mdi:swap-vertical-bold", None, None, diagnostic=True),
    SensorSpec("water_level", "水量档位", f001.DVID_MOTION, "water_level_percent",
               "mdi:chart-arc", "%", None, diagnostic=True),
    SensorSpec("continuous_water_time", "连续用水时间", f001.DVID_CONTINUOUS_TIME,
               "continuous_water_time", "mdi:timer-outline",
               UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    SensorSpec("instant_heat_countdown", "即热剩余时间", f001.DVID_HEAT_TIME_L,
               "instant_heat_countdown", "mdi:timer-sand",
               UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    SensorSpec("sterilize_countdown", "杀菌剩余时间", f001.DVID_STERILIZE,
               "sterilize_countdown", "mdi:timer-sand-complete",
               UnitOfTime.MINUTES, SensorDeviceClass.DURATION),
    SensorSpec("water_usage_session", "本次用水量", f001.DVID_USE_WATER_H,
               "water_usage_session", "mdi:water", UnitOfVolume.LITERS,
               SensorDeviceClass.WATER),
    SensorSpec("gas_usage_session", "本次用气量", f001.DVID_USE_GAS_H,
               "gas_usage_session", "mdi:fire", UnitOfVolume.CUBIC_METERS,
               SensorDeviceClass.GAS),
    SensorSpec("water_usage_total", "累计用水量", f001.DVID_TOTAL_WATER_3,
               "water_usage_total", "mdi:water-plus", UnitOfVolume.CUBIC_METERS,
               SensorDeviceClass.WATER),
    SensorSpec("gas_usage_total", "累计用气量", f001.DVID_TOTAL_GAS_3,
               "gas_usage_total", "mdi:fire-circle", UnitOfVolume.CUBIC_METERS,
               SensorDeviceClass.GAS),
    # 以下两点的取值语义未解码（实测常态 242=7、249=1），只暴露原始值，不做布尔解读，
    # 也就不会出现"恒为异常"的误报。见 features_001.ALARM_NORMAL_VALUES 注释。
    SensorSpec("co_status", "CO状态(原始值)", f001.DVID_CO_ALARM, "co_status_raw",
               "mdi:molecule-co", None, None, diagnostic=True),
    SensorSpec("blockage_raw", "堵塞条件(原始值)", f001.DVID_BLOCKAGE, "blockage_raw",
               "mdi:pipe-valve", None, None, diagnostic=True),
)


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[SensorEntity] = []
    for dev in api.devices:
        if not isinstance(dev, WanjialeGasWaterHeater):
            continue
        for spec in _SPECS:
            if dev.has_point(spec.dvid):
                entities.append(WanjialeSensor(dev, coordinator, spec))
        if dev.has_point(f001.DVID_FAULT):
            entities.append(WanjialeFaultSensor(dev, coordinator))
        if dev.has_point(f001.DVID_RESERVE_LIST):
            entities.append(WanjialeReserveScheduleSensor(dev, coordinator))
        if dev.has_point(f001.DVID_NEXT_RESERVE):
            entities.append(WanjialeNextReserveSensor(dev, coordinator))
    _LOGGER.debug("创建 %d 个状态传感器", len(entities))
    async_add_entities(entities, True)


class WanjialeSensor(WanjialeEntity, SensorEntity):
    """由 SensorSpec 驱动的只读传感器。"""

    def __init__(self, device: WanjialeGasWaterHeater, coordinator, spec: SensorSpec) -> None:
        super().__init__(device, coordinator)
        self._wh = device
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = spec.device_class
        if spec.precision is not None:
            self._attr_suggested_display_precision = spec.precision
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return self._wh.parsed.get(self._spec.value_key)


class WanjialeFaultSensor(WanjialeEntity, SensorEntity):
    """故障代码（dvid17）。正常时显示「正常」，异常时显示 E0/E1/En/F3 等文案。

    故障码背后的「标题 + 官方排查指引」是机型专属的，从 faults_001.json 按机型读取；
    未收录的机型只显示故障码。
    """

    _entity_key = "fault"
    _attr_name = "故障代码"
    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, device: WanjialeGasWaterHeater, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh = device

    @property
    def native_value(self):
        raw = self._wh.parsed.get("fault")
        if raw is None:
            return None
        if raw == f001.FAULT_NONE:
            return "正常"
        return self._wh.parsed.get("fault_text") or f"E{raw}"

    @property
    def extra_state_attributes(self):
        raw = self._wh.parsed.get("fault")
        info = fault_info(self._wh.caps.key, raw)
        attrs: Dict[str, Any] = {
            "故障值": raw,
            "机型": self._wh.caps.key or "未收录（无故障文案）",
        }
        if raw in (None, f001.FAULT_NONE):
            return attrs
        if info:
            attrs["故障标题"] = info.get("title")
            attrs["官方排查指引"] = info.get("guide")
        else:
            attrs["故障标题"] = self._wh.parsed.get("fault_text")
            attrs["官方排查指引"] = "该机型未收录故障文案，请查阅随箱说明书或官方应用"
        return attrs


class WanjialeNextReserveSensor(WanjialeEntity, SensorEntity):
    """下次预约时间（dvid9，单位分钟）。

    HA 没有「时刻」类型的传感器设备类，这里状态直接给 HH:MM 文本，属性保留原始分钟，
    便于仪表盘直接展示、自动化按属性取值。
    """

    _entity_key = "next_reserve"
    _attr_name = "下次预约时间"
    _attr_icon = "mdi:clock-outline"

    def __init__(self, device: WanjialeGasWaterHeater, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh = device

    @property
    def native_value(self):
        minutes = self._wh.parsed.get("next_reserve_time")
        if minutes is None:
            return None
        return _format_minutes(int(minutes))

    @property
    def extra_state_attributes(self):
        return {"原始分钟数": self._wh.parsed.get("next_reserve_time")}


class WanjialeReserveScheduleSensor(WanjialeEntity, SensorEntity):
    """预约时段（dvid6）。

    HA 没有原生的「时间窗计划」实体，这里只做只读展示：
    状态为预约时段数量，属性里列出每段起止时间（HH:MM）。
    增删改请在官方应用中完成，避免多端写入冲突。
    """

    _entity_key = "reserve_schedule"
    _attr_name = "预约时段"
    _attr_icon = "mdi:calendar-multiple"

    def __init__(self, device: WanjialeGasWaterHeater, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh = device

    @property
    def native_value(self):
        windows = self._wh.parsed.get("reserve_list")
        return None if windows is None else len(windows)

    @property
    def extra_state_attributes(self):
        windows = self._wh.parsed.get("reserve_list") or []
        return {
            "预约时段": [
                f"{_format_minutes(int(w['s']))}-{_format_minutes(int(w['e']))}" for w in windows
            ]
        }

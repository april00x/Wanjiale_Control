"""万家乐只读状态实体（sensor 平台）。

实体有三种来源，均与具体品类无关：spec 驱动（`registry.platform_specs`），仅在设备
确实上报过对应点位或命中机型基线点位时才创建，避免给不支持的机型堆一批永远 unknown
的实体；跨品类通用的故障代码，是否创建由插件声明的 `fault_points` 决定，「正常值」
用 `plugin.fault_none` 判定，文案由 `capabilities.fault_info()` 按机型查表；未收录
品类的原始状态，只读且便于用户报障时直接提供诊断信息。

只读平台是设备级的：所有角色共用同一份上报状态 `parsed`。
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .capabilities import fault_info
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import SensorSpec

_LOGGER = logging.getLogger(__name__)


def _format_minutes(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _fmt_hhmm(raw: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """分钟数格式化为 HH:MM 文本；原始分钟数保留在属性里，供自动化取值。"""
    if raw is None:
        return None, None
    return _format_minutes(int(raw)), {"原始分钟数": raw}


def _fmt_reserve_windows(raw: Any) -> Tuple[Any, Optional[Dict[str, Any]]]:
    """预约时段列表只做只读展示：状态值为段数，每段起止时间列在属性里。

    HA 没有原生的时间窗计划实体，增删改请在官方应用中完成。
    """
    windows = raw or []
    value = None if raw is None else len(windows)
    return value, {
        "预约时段": [
            f"{_format_minutes(int(w['s']))}-{_format_minutes(int(w['e']))}" for w in windows
        ]
    }


# spec.format 到格式化函数的映射（返回「状态值, 附加属性」）
_FORMATTERS: Dict[str, Callable[[Any], Tuple[Any, Optional[Dict[str, Any]]]]] = {
    "hhmm": _fmt_hhmm,
    "reserve_windows": _fmt_reserve_windows,
}


def _device_class(name: Optional[str]) -> Optional[SensorDeviceClass]:
    """spec 里的字符串转为 HA 枚举（HA 的枚举是 str 子类，转换无损）。"""
    if not name:
        return None
    try:
        return SensorDeviceClass(name)
    except ValueError:
        _LOGGER.warning("未知的 sensor 设备类：%s", name)
        return None


def _has_fault_point(dev: WanjialeDevice, plugin) -> bool:
    """该设备是否上报了插件声明的故障点位。"""
    declared = (plugin.fault_points or {}).get("device") or ()
    return any(dev.has_point(str(point)) for point in declared)


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
        plugin = dev.plugin
        if plugin is None:
            # 未收录品类：只给一个只读诊断实体，可见但不可控
            entities.append(WanjialeRawStateSensor(dev, coordinator))
            continue
        for spec in registry.platform_specs(dev, "sensor"):
            if dev.has_point(spec.dvid):
                entities.append(WanjialeSensor(dev, coordinator, spec))
        if _has_fault_point(dev, plugin):
            entities.append(WanjialeFaultSensor(dev, coordinator))
    _LOGGER.debug("创建 %d 个状态传感器", len(entities))
    async_add_entities(entities, True)


class WanjialeSensor(WanjialeEntity, SensorEntity):
    """由 `SensorSpec` 驱动的只读传感器。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        spec: SensorSpec,
    ) -> None:
        super().__init__(device, coordinator)
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_device_class = _device_class(spec.device_class)
        if spec.precision is not None:
            self._attr_suggested_display_precision = spec.precision
        if spec.diagnostic:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    def _formatted(self) -> Tuple[Any, Optional[Dict[str, Any]]]:
        raw = self._device.parsed.get(self._spec.value_key)
        formatter = _FORMATTERS.get(self._spec.format) if self._spec.format else None
        if formatter is None:
            return raw, None
        return formatter(raw)

    @property
    def native_value(self):
        return self._formatted()[0]

    @property
    def extra_state_attributes(self):
        return self._formatted()[1]


class WanjialeFaultSensor(WanjialeEntity, SensorEntity):
    """故障代码（跨品类通用）。正常时显示「正常」，异常时显示故障码文案。

    故障码背后的「标题 + 官方排查指引」是机型专属的，按机型读取；未收录的机型
    只显示故障码。正常值取自 `plugin.fault_none`。
    """

    _entity_key = "fault"
    _attr_name = "故障代码"
    _attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self):
        raw = self._device.parsed.get("fault")
        if raw is None:
            return None
        plugin = self._device.plugin
        if plugin is not None and raw in plugin.fault_none:
            return "正常"
        return self._device.parsed.get("fault_text") or f"E{raw}"

    @property
    def extra_state_attributes(self):
        raw = self._device.parsed.get("fault")
        caps = self._device.caps
        # 按认领品类查表，避免跨品类误查
        info = fault_info(caps.key, raw, caps.product)
        attrs: Dict[str, Any] = {
            "故障值": raw,
            "机型": caps.key or "未收录（无故障文案）",
        }
        plugin = self._device.plugin
        if raw is None or (plugin is not None and raw in plugin.fault_none):
            return attrs
        if info:
            attrs["故障标题"] = info.get("title")
            attrs["官方排查指引"] = info.get("guide")
        else:
            attrs["故障标题"] = self._device.parsed.get("fault_text")
            attrs["官方排查指引"] = "该机型未收录故障文案，请查阅随箱说明书或官方应用"
        return attrs


class WanjialeRawStateSensor(WanjialeEntity, SensorEntity):
    """未收录品类的原始状态只读实体：可见但不可控。

    状态值为已上报点数，属性给出原始点位，便于用户报障时提供诊断信息；
    该品类不接受任何控制。
    """

    _entity_key = "raw_state"
    _attr_name = "原始状态(未收录品类)"
    _attr_icon = "mdi:help-circle-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return len(self._device.state)

    @property
    def extra_state_attributes(self):
        return {
            "设备标识": self._device.did,
            "云平台产品码": self._device.product,
            "机型": self._device.model,
            "说明": "该机型未收录，当前为只读；报障时请提供本实体属性",
            "原始点位": dict(self._device.state),
        }

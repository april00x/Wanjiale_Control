"""万家乐燃气热水器主实体（water_heater 平台）。

承载最核心的三件事：开关机、设定温度、模式切换。
- 温度：对外一律暴露显示温度；随温感模式下内部按 +3-环境修正 反算原始值下发。
- 模式：模式列表来自「机型模板 ∪ 实机上报」，不写死。
- 其余零冷水 / 增压 / 保温等能力拆到 switch / number / select / button 平台。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.water_heater import (
    WaterHeaterEntity,
    WaterHeaterEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WanjialeApi, WanjialeGasWaterHeater
from .const import DOMAIN
from .entity import WanjialeDiagnosticEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities = [
        WanjialeWaterHeaterEntity(dev, coordinator)
        for dev in api.devices
        if isinstance(dev, WanjialeGasWaterHeater) and dev.has_feature("开关机")
    ]
    _LOGGER.debug("创建 %d 个热水器主实体", len(entities))
    async_add_entities(entities, True)


class WanjialeWaterHeaterEntity(WanjialeDiagnosticEntity, WaterHeaterEntity):
    """热水器主实体。"""

    _entity_key = "water_heater"
    # 主实体直接使用设备名（has_entity_name 组合后即设备名本身）
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_icon = "mdi:water-boiler"

    def __init__(self, device: WanjialeGasWaterHeater, coordinator) -> None:
        super().__init__(device, coordinator)
        self._wh: WanjialeGasWaterHeater = device

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        features = (
            WaterHeaterEntityFeature.ON_OFF
            | WaterHeaterEntityFeature.TARGET_TEMPERATURE
        )
        if self._wh.has_feature("模式切换"):
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        return features

    @property
    def operation_list(self) -> List[str]:
        modes = self._wh.mode_names()
        # 按模式号排序，并去重（同名的模式只保留一个）
        names: List[str] = []
        for mode_id in sorted(modes):
            name = modes[mode_id]
            if name not in names:
                names.append(name)
        return names

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def is_on(self) -> Optional[bool]:
        return self._wh.is_power_on

    @property
    def current_temperature(self) -> Optional[float]:
        """当前水温（dvid18）。"""
        return self._wh.current_temperature

    @property
    def target_temperature(self) -> Optional[float]:
        """设定温度（对外显示值）。"""
        return self._wh.target_temperature

    @property
    def current_operation(self) -> Optional[str]:
        mode = self._wh.current_mode
        if mode is None:
            return None
        return self._wh.mode_names().get(mode)

    @property
    def min_temp(self) -> float:
        return float(self._wh.min_temp())

    @property
    def max_temp(self) -> float:
        # 随模式变化：随温感 = 环境修正+45 对应显示值，ECO = 48℃，其余 60℃
        return float(self._wh.max_temp())

    # ------------------------------------------------------------------
    # 控制（同步方法，HA 自动放到 executor 线程执行）
    # ------------------------------------------------------------------
    def turn_on(self, **kwargs: Any) -> None:
        self._control(self._wh.turn_on)

    def turn_off(self, **kwargs: Any) -> None:
        self._control(self._wh.turn_off)

    def set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        self._control(self._wh.set_temperature, int(round(float(temperature))))

    def set_operation_mode(self, operation_mode: str) -> None:
        for mode_id, name in self._wh.mode_names().items():
            if name == operation_mode:
                self._control(self._wh.set_mode, mode_id)
                return
        raise HomeAssistantError(f"未知模式：{operation_mode}")

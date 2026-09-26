"""万家乐热水器主实体（water_heater 平台）。

承载开关机、设定温度、模式切换三件事，全部由实现 `WaterHeaterProto` 的角色提供。
判定按角色接口而非设备类型，因此与具体品类无关，新增品类无需改本文件。
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

from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeDiagnosticEntity
from .roles import WaterHeaterProto

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeWaterHeaterEntity] = []
    for dev in api.devices:
        if not dev.has_feature("开关机"):
            continue
        # 角色级平台：一台设备可能有多个实现该接口的角色
        for role in dev.role_views():
            if isinstance(role, WaterHeaterProto):
                entities.append(WanjialeWaterHeaterEntity(dev, coordinator, role))
    _LOGGER.debug("创建 %d 个热水器主实体", len(entities))
    async_add_entities(entities, True)


class WanjialeWaterHeaterEntity(WanjialeDiagnosticEntity, WaterHeaterEntity):
    """热水器主实体（由实现 `WaterHeaterProto` 的角色驱动）。"""

    _entity_key = "water_heater"
    # 主实体直接使用设备名（has_entity_name 组合后即设备名本身）
    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_icon = "mdi:water-boiler"

    def __init__(self, device: WanjialeDevice, coordinator, role: Any) -> None:
        super().__init__(device, coordinator, role)

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    @property
    def supported_features(self) -> WaterHeaterEntityFeature:
        features = (
            WaterHeaterEntityFeature.ON_OFF
            | WaterHeaterEntityFeature.TARGET_TEMPERATURE
        )
        # 「模式切换」这一 flag 同时表示温度可设与模式可切，故再加一层角色判定：
        # 机型模板没有模式表时不暴露模式选择，避免给出空的假下拉框。
        # 未实现该成员的角色按 True 处理。
        if self._device.has_feature("模式切换") and getattr(
            self.role, "supports_mode_selection", True
        ):
            features |= WaterHeaterEntityFeature.OPERATION_MODE
        return features

    @property
    def operation_list(self) -> List[str]:
        modes = self.role.mode_names()
        # 按模式号排序，并去重（同名的模式只保留一个）
        names: List[str] = []
        for mode_id in sorted(modes):
            name = modes[mode_id]
            if name not in names:
                names.append(name)
        return names

    # ------------------------------------------------------------------
    # 状态（角色暴露，均为属性）
    # ------------------------------------------------------------------
    @property
    def is_on(self) -> Optional[bool]:
        return self.role.is_power_on

    @property
    def current_temperature(self) -> Optional[float]:
        """当前水温。"""
        return self.role.current_temperature

    @property
    def target_temperature(self) -> Optional[float]:
        """设定温度（对外显示值）。"""
        return self.role.target_temperature

    @property
    def current_operation(self) -> Optional[str]:
        mode = self.role.current_mode
        if mode is None:
            return None
        return self.role.mode_names().get(mode)

    @property
    def min_temp(self) -> float:
        return float(self.role.min_temp())

    @property
    def max_temp(self) -> float:
        # 上限随模式变化，具体值由角色按模式给出
        return float(self.role.max_temp())

    # ------------------------------------------------------------------
    # 控制（同步方法，HA 自动放到 executor 线程执行）
    # ------------------------------------------------------------------
    def turn_on(self, **kwargs: Any) -> None:
        self._control(self.role.turn_on)

    def turn_off(self, **kwargs: Any) -> None:
        self._control(self.role.turn_off)

    def set_temperature(self, **kwargs: Any) -> None:
        temperature = kwargs.get("temperature")
        if temperature is None:
            return
        self._control(self.role.set_temperature, int(round(float(temperature))))

    def set_operation_mode(self, operation_mode: str) -> None:
        for mode_id, name in self.role.mode_names().items():
            if name == operation_mode:
                self._control(self.role.set_mode, mode_id)
                return
        raise HomeAssistantError(f"未知模式：{operation_mode}")

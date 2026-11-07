"""万家乐壁挂炉供暖实体（climate 平台）。

判定按角色接口（`ClimateProto`）而非品类，与其它平台一致；一台壁挂炉同时有
卫浴与供暖两个角色，本平台只消费供暖角色，避免两个设定温度互相覆盖。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.climate import ClimateEntity, ClimateEntityFeature, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .api import WanjialeApi
from .const import DOMAIN
from .entity import WanjialeDiagnosticEntity
from .roles import ClimateProto

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeClimateEntity] = []
    for dev in api.devices:
        if not dev.controls_enabled:
            continue                      # 控制被禁用的机型：不出任何控制实体
        if not (dev.has_feature("供暖温度") or dev.has_feature("供暖开关")):
            continue
        for role in dev.role_views():
            if isinstance(role, ClimateProto):
                entities.append(WanjialeClimateEntity(dev, coordinator, role))
    _LOGGER.debug("创建 %d 个供暖实体", len(entities))
    async_add_entities(entities, True)


class WanjialeClimateEntity(WanjialeDiagnosticEntity, ClimateEntity):
    """供暖主实体（供暖开关 / 设定温度 / 供暖模式）。"""

    _entity_key = "climate"
    _attr_name = "供暖"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_icon = "mdi:radiator"

    def __init__(self, device, coordinator, role) -> None:
        super().__init__(device, coordinator, role)

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    @property
    def supported_features(self) -> ClimateEntityFeature:
        features = ClimateEntityFeature.TURN_ON | ClimateEntityFeature.TURN_OFF
        if getattr(self.role, "supports_target_temperature", True):
            features |= ClimateEntityFeature.TARGET_TEMPERATURE
        return features

    @property
    def hvac_modes(self) -> List[HVACMode]:
        return [HVACMode(value) for value in self.role.hvac_modes().values()]

    @property
    def hvac_mode(self) -> Optional[HVACMode]:
        mode = self.role.current_hvac_mode
        return None if mode is None else HVACMode(self.role.hvac_modes()[mode])

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    @property
    def current_temperature(self) -> Optional[float]:
        return self.role.current_temperature

    @property
    def target_temperature(self) -> Optional[float]:
        return self.role.target_temperature

    @property
    def min_temp(self) -> float:
        return float(self.role.min_temp())

    @property
    def max_temp(self) -> float:
        return float(self.role.max_temp())

    # ------------------------------------------------------------------
    # 控制（同步方法，HA 自动放到 executor 线程执行）
    # ------------------------------------------------------------------
    def turn_on(self) -> None:
        self._control(self.role.turn_on)

    def turn_off(self) -> None:
        self._control(self.role.turn_off)

    def set_temperature(self, **kwargs: Any) -> None:
        value = kwargs.get("temperature")
        if value is not None:
            self._control(self.role.set_temperature, int(round(float(value))))

    def set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        for code, value in self.role.hvac_modes().items():
            if value == hvac_mode:
                self._control(self.role.set_hvac_mode, code)
                return
        raise HomeAssistantError(f"未知供暖模式：{hvac_mode}")

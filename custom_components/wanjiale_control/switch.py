"""万家乐开关型功能实体（switch 平台）。

spec 表由品类目录提供（`registry.platform_specs`），本模块只负责把 spec 渲染成
HA 实体，与具体品类无关。

控制类平台是角色级的：spec 的 `method` 在角色上解析，所以遍历 `dev.role_views()`，
且只在角色确实实现了该方法时才建实体。
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import SwitchSpec, require_points_satisfied

_LOGGER = logging.getLogger(__name__)


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
        if not dev.controls_enabled:
            continue                      # 控制被禁用的机型：不出控制实体
        points = getattr(dev.caps, "model_points", None) or {}
        for spec in registry.platform_specs(dev, "switch"):
            if spec.feature and not dev.has_feature(spec.feature):
                continue
            if not require_points_satisfied(spec, points):
                continue
            # device_level=True 的组实体直接对设备渲染（unique_id 不带角色后缀）
            roles = [dev] if spec.device_level else dev.role_views()
            for role in roles:
                if not hasattr(role, spec.method):
                    continue
                entities.append(WanjialeFeatureSwitch(dev, coordinator, role, spec))
    _LOGGER.debug("创建 %d 个功能开关实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureSwitch(WanjialeEntity, SwitchEntity):
    """由 `SwitchSpec` 驱动的通用功能开关。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        role: Any,
        spec: SwitchSpec,
    ) -> None:
        super().__init__(device, coordinator, role)
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
            value = self._device.parsed.get(self._spec.state_key)
            if value is not None:
                return bool(value)
        return self._local_state

    def turn_on(self, **kwargs: Any) -> None:
        self._control(self._apply, True)

    def turn_off(self, **kwargs: Any) -> None:
        self._control(self._apply, False)

    def _apply(self, on: bool) -> None:
        getattr(self.role, self._spec.method)(*self._spec.method_args, on)
        self._local_state = bool(on)

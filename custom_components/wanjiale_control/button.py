"""万家乐动作型功能实体（button 平台）。

spec 表由品类目录提供（`registry.platform_specs`），本模块只负责把 spec 渲染成
HA 实体，与具体品类无关。一次性动作没有可读的开关状态，用 button 承载最贴合语义。

控制类平台是角色级的：spec 的 `method` 在角色上解析。
"""
from __future__ import annotations

import logging
from typing import Any, List

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import ButtonSpec

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeFeatureButton] = []
    for dev in api.devices:
        for spec in registry.platform_specs(dev, "button"):
            if spec.feature and not dev.has_feature(spec.feature):
                continue
            if spec.require_point and not dev.has_point(spec.require_point):
                continue
            for role in dev.role_views():
                if not hasattr(role, spec.method):
                    continue
                entities.append(WanjialeFeatureButton(dev, coordinator, role, spec))
    _LOGGER.debug("创建 %d 个动作实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureButton(WanjialeEntity, ButtonEntity):
    """由 `ButtonSpec` 驱动的通用动作实体。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        role: Any,
        spec: ButtonSpec,
    ) -> None:
        super().__init__(device, coordinator, role)
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon

    def press(self) -> None:
        self._control(getattr(self.role, self._spec.method))

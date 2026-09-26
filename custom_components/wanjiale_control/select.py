"""万家乐枚举型功能实体（select 平台）。

spec 表由品类目录提供（`registry.platform_specs`），本模块只负责把 spec 渲染成
HA 实体，与具体品类无关。设备侧的离散档位用 select 承载可避免下发非法值。

档位表有两种来源（`SelectSpec`）：`options` 为静态表；`options_from` 是角色上的
方法名，在 `__init__` 解析一次，机型级档位表只能运行时取。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN
from .entity import WanjialeEntity
from .specs import SelectSpec

_LOGGER = logging.getLogger(__name__)


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
        for spec in registry.platform_specs(dev, "select"):
            if not dev.has_feature(spec.feature):
                continue
            for role in dev.role_views():
                if not hasattr(role, spec.method):
                    continue
                entities.append(WanjialeFeatureSelect(dev, coordinator, role, spec))
    _LOGGER.debug("创建 %d 个枚举实体", len(entities))
    async_add_entities(entities, True)


class WanjialeFeatureSelect(WanjialeEntity, SelectEntity):
    """由 `SelectSpec` 驱动的通用枚举实体。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator,
        role: Any,
        spec: SelectSpec,
    ) -> None:
        super().__init__(device, coordinator, role)
        self._spec = spec
        self._entity_key = spec.key
        self._attr_name = spec.name
        self._attr_icon = spec.icon
        self._options: Dict[int, str] = self._resolve_options(role, spec)
        self._attr_options = list(self._options.values())

    @staticmethod
    def _resolve_options(role: Any, spec: SelectSpec) -> Dict[int, str]:
        """解析档位表：静态表或角色提供的机型级表（只解析一次）。"""
        if spec.options_from:
            provider = getattr(role, spec.options_from, None)
            if callable(provider):
                return {int(k): str(v) for k, v in (provider() or {}).items()}
            _LOGGER.warning("角色缺少 options_from 方法 %s，档位表为空", spec.options_from)
            return {}
        return {int(k): str(v) for k, v in (spec.options or {}).items()}

    @property
    def current_option(self) -> str | None:
        raw = self._device.parsed.get(self._spec.state_key)
        if raw is None:
            return None
        return self._options.get(int(raw))

    def select_option(self, option: str) -> None:
        for level, name in self._options.items():
            if name == option:
                self._control(getattr(self.role, self._spec.method), level)
                return
        raise HomeAssistantError(f"未知档位：{option}")

    @property
    def extra_state_attributes(self):
        return {
            f"设备原始档位({self._spec.state_key})": self._device.parsed.get(self._spec.state_key)
        }

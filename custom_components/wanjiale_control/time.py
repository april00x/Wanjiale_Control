"""万家乐壁挂炉预约组时间实体（time 平台）。

设备级平台：预约组（供暖/热水/洗浴）的起止时间不挂在角色上，
`unique_id` 保持 `wanjiale-<did>-schedule_<kind>_<n>-<field>`，不带角色后缀。
spec 由品类目录按设备生成（`registry.platform_specs(dev, "time")`）。
"""
from __future__ import annotations

import logging
from datetime import time
from typing import List

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import registry
from .api import WanjialeApi
from .const import DOMAIN
from .entity import WanjialeDiagnosticEntity

_LOGGER = logging.getLogger(__name__)

_KIND_LABELS = {"heat": "供暖预约", "dhw": "热水预热", "wash": "洗浴预约"}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    api: WanjialeApi = entry_data["api"]
    coordinator = entry_data["coordinator"]

    entities: List[WanjialeScheduleTimeEntity] = []
    for dev in api.devices:
        if not dev.controls_enabled:
            continue                      # 控制被禁用的机型：不出时间实体
        for spec in registry.platform_specs(dev, "time"):
            # time spec 允许无功能位（B3 洗浴预约 / B4 温控器派生量）
            if spec.feature and not dev.has_feature(spec.feature):
                continue
            entities.append(WanjialeScheduleTimeEntity(dev, coordinator, spec))
    _LOGGER.debug("创建 %d 个预约时间实体", len(entities))
    async_add_entities(entities, True)


class WanjialeScheduleTimeEntity(WanjialeDiagnosticEntity, TimeEntity):
    """预约组起/止时间（分钟数 ↔ `datetime.time`）。"""

    def __init__(self, device, coordinator, spec) -> None:
        super().__init__(device, coordinator, device)
        self._spec = spec
        # 基类 unique_id property 拼的是 "_entity_key + role_suffix"，
        # 直接写 _attr_unique_id 不生效（会被基类 property 覆盖），必须设 _entity_key。
        self._entity_key = f"schedule_{spec.kind}_{spec.group}-{spec.field}"
        default_label = _KIND_LABELS.get(spec.kind, "预约")
        self._attr_name = (
            (spec.label or default_label)
            + f"{spec.group}"
            + ("开始" if spec.field == "start" else "结束")
        )

    @property
    def native_value(self):
        """HA TimeEntity 的取值契约是 `native_value`，用 `value` 会永远 unknown。"""
        minutes = self.device.parsed.get(self._spec.state_key)
        if minutes is None:
            return None
        minutes = int(minutes)
        # 协议允许 0~1440，但 HA 的 datetime.time 不支持 24:00
        if not 0 <= minutes < 1440:
            return None
        return time(hour=minutes // 60, minute=minutes % 60)

    async def async_set_value(self, value) -> None:
        # TimeEntity 是 async 平台，_send 是同步阻塞 IO，必须放 executor；
        # 直接在事件循环里调 _control 会卡住 HA（最坏等局域网/云端超时）。
        await self.hass.async_add_executor_job(
            self._set_value, value.hour * 60 + value.minute
        )

    def _set_value(self, minutes: int) -> None:
        self._control(
            self.device.set_schedule_time,
            self._spec.kind, self._spec.group, self._spec.field, minutes,
        )

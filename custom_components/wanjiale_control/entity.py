"""万家乐实体的公共基类。

统一约定：
  - has_entity_name = True：实体名由「设备名 + 实体名」自动组合，实体 ID 干净；
  - unique_id = 设备 unique_id + 实体短键，跨重启稳定；
  - _control()：执行设备控制，把 WanjialeControlError 转成 HA 的 HomeAssistantError，
    并统一做「乐观刷新 + 延迟拉取真实状态」。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable, Dict, Optional, cast

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .api import WanjialeApi, WanjialeControlError, WanjialeDevice
from .const import DOMAIN

# 控制后延迟多久拉取一次真实状态（早于定时轮询，让下发结果尽快回显）
REFRESH_DELAY = 2.0


def device_info(dev: WanjialeDevice) -> DeviceInfo:
    """构造 Home Assistant 的 DeviceInfo。"""
    return DeviceInfo(
        identifiers={(DOMAIN, dev.did)},
        name=dev.name or dev.did,
        manufacturer="万家乐 (Wanjiale)",
        model=dev.model or dev.category_cn,
        sw_version=dev.ver or None,
        serial_number=dev.did or None,
    )


class WanjialeEntity(CoordinatorEntity):
    """所有万家乐实体的基类。"""

    _attr_has_entity_name = True

    # 子类必须覆盖：实体短键（用于拼 unique_id，需全局唯一）
    _entity_key: str = ""

    def __init__(self, device: WanjialeDevice, coordinator: DataUpdateCoordinator) -> None:
        super().__init__(coordinator)
        self._device = device

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(self._device)

    @property
    def unique_id(self) -> str:
        return f"{self._device.unique_id()}-{self._entity_key}"

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def api(self) -> WanjialeApi:
        # coordinator.data 即 WanjialeApi 实例
        return cast(WanjialeApi, self.coordinator.data)

    @property
    def device(self) -> WanjialeDevice:
        return self._device

    # ------------------------------------------------------------------
    # 控制辅助
    # ------------------------------------------------------------------
    def _control(self, action: Callable[..., Any], *args: Any) -> None:
        """执行控制动作；失败原因以 HomeAssistantError 抛给用户。"""
        try:
            action(*args)
        except WanjialeControlError as err:
            raise HomeAssistantError(str(err)) from err
        self.schedule_update_ha_state()
        self._request_refresh_soon()

    def _request_refresh_soon(self) -> None:
        """REFRESH_DELAY 秒后触发一次 coordinator 刷新。

        控制命令已发出且做了乐观更新，UI 立即显示预期值；稍后拉取设备真实状态校正。
        使用 call_soon_threadsafe 从 executor 线程安全投递到事件循环。
        """

        async def _do_refresh() -> None:
            await asyncio.sleep(REFRESH_DELAY)
            await self.coordinator.async_request_refresh()

        self.hass.loop.call_soon_threadsafe(lambda: self.hass.async_create_task(_do_refresh()))


class WanjialeDiagnosticEntity(WanjialeEntity):
    """在属性里附带解析后状态的实体（仅用于主实体排障）。"""

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        parsed = dict(self._device.parsed or {})
        parsed["did"] = self._device.did
        parsed["online"] = self._device.online
        parsed["支持功能"] = "、".join(self._device.features) or "无"
        return parsed

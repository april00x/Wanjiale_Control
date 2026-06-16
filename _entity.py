"""万家乐设备实体的公共基类 / 辅助函数。"""
from __future__ import annotations

from typing import Any, Dict, Optional, cast

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .api import WanjialeApi, WanjialeDevice
from .const import DOMAIN


def device_info(dev: WanjialeDevice) -> DeviceInfo:
    """构造 Home Assistant 的 DeviceInfo。"""
    return DeviceInfo(
        identifiers={(DOMAIN, dev.did)},
        name=dev.name or dev.did,
        manufacturer="万家乐 (Wanjiale)",
        model=dev.model or dev.category_cn,
        sw_version=None,
    )


class WanjialeEntity(CoordinatorEntity):
    """所有万家乐实体的基类 —— 绑定到 api.device + coordinator。"""

    def __init__(
        self,
        device: WanjialeDevice,
        coordinator: DataUpdateCoordinator,
    ) -> None:
        super().__init__(coordinator)
        self._device = device

    @property
    def device_info(self) -> DeviceInfo:
        return device_info(self._device)

    @property
    def unique_id(self) -> str:
        return self._device.unique_id()

    @property
    def name(self) -> str:
        return self._device.name or self._device.did

    @property
    def available(self) -> bool:
        return self._device.online

    @property
    def extra_state_attributes(self) -> Optional[Dict[str, Any]]:
        attr = dict(self._device.attributes or {})
        attr["did"] = self._device.did
        attr["online"] = self._device.online
        return attr

    @property
    def api(self) -> WanjialeApi:
        # coordinator.data 即 WanjialeApi 实例
        return cast(WanjialeApi, self.coordinator.data)

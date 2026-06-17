"""万家乐 Home Assistant 集成。"""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import WanjialeApi
from .const import (
    CONF_IMEI,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .protocol import WanjialeProtocol

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ("water_heater", "sensor", "switch")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """基于 config entry 启动集成。"""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    imei = entry.data.get(CONF_IMEI, "")

    protocol = WanjialeProtocol(username=username, password=password, imei=imei)
    api = WanjialeApi(protocol)

    try:
        # 登录 + 加载设备在 executor 中完成（同步 socket）
        await hass.async_add_executor_job(api.login)
        await hass.async_add_executor_job(api.load_devices)
        # 尝试建立长连接（可选，失败不影响启动）
        try:
            await hass.async_add_executor_job(api.connect_server)
        except Exception:
            _LOGGER.warning("建立长连接失败，将无法获取设备实时状态和控制")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("Wanjiale Control 初始化失败")
        raise ConfigEntryNotReady(f"wanjiale_control: {exc}") from exc

    async def _do_refresh() -> WanjialeApi:
        """coordinator 的异步刷新逻辑。"""
        await api.async_refresh_all()
        return api

    # 创建数据更新 coordinator
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=_do_refresh,
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    )
    # 初始刷新一次
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成时关闭长连接。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
        api = entry_data.get("api")
        if api:
            await hass.async_add_executor_job(api.close_server)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

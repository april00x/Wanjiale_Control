"""万家乐 Home Assistant 集成。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import WanjialeApi
from .capabilities import OVERRIDE_OFF, OVERRIDE_ON
from .const import (
    CONF_ENABLE_CONTROL_RIGHTS,
    CONF_FEATURE_FORCE_OFF,
    CONF_FEATURE_FORCE_ON,
    CONF_IMEI,
    CONF_PASSWORD,
    CONF_USERNAME,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    PLATFORMS,
)
from .protocol import WanjialeProtocol

_LOGGER = logging.getLogger(__name__)


def build_feature_overrides(options: Dict[str, Any]) -> Dict[str, str]:
    """把选项页的「强制开/强制关」列表转换成 capabilities 需要的覆盖字典。"""
    overrides: Dict[str, str] = {}
    for name in options.get(CONF_FEATURE_FORCE_OFF) or []:
        overrides[str(name)] = OVERRIDE_OFF
    for name in options.get(CONF_FEATURE_FORCE_ON) or []:
        overrides[str(name)] = OVERRIDE_ON
    return overrides


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """基于 config entry 启动集成。"""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    imei = entry.data.get(CONF_IMEI, "")

    protocol = WanjialeProtocol(username=username, password=password, imei=imei)
    api = WanjialeApi(
        protocol,
        feature_overrides=build_feature_overrides(entry.options),
        enable_control_rights=bool(entry.options.get(CONF_ENABLE_CONTROL_RIGHTS, False)),
        # 登录名即账号手机号，操作权抢占需要它
        login_name=username,
    )

    try:
        # 登录 + 加载设备在 executor 中完成（同步 socket）
        await hass.async_add_executor_job(api.login)
        await hass.async_add_executor_job(api.load_devices)
        # 尝试建立长连接（可选，失败不影响启动）
        try:
            await hass.async_add_executor_job(api.connect_server)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("建立长连接失败，设备状态与控制可能不可用")
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("Wanjiale Control 初始化失败")
        raise ConfigEntryNotReady(f"wanjiale_control: {exc}") from exc

    async def _do_refresh() -> WanjialeApi:
        await api.async_refresh_all()
        return api

    coordinator: DataUpdateCoordinator[WanjialeApi] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=_do_refresh,
        update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
    )
    # 初始刷新一次：既拿到首屏状态，也让能力探测有实机点位可依据
    await coordinator.async_config_entry_first_refresh()

    # 依据「内置能力表 + 实机点位 + 选项覆盖」定稿功能集合，之后再创建实体
    await hass.async_add_executor_job(api.finalize_features)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项变更后重载集成（功能集合需要重新计算）。"""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成时关闭长连接与局域网连接。"""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        entry_data = hass.data[DOMAIN].get(entry.entry_id, {})
        api: WanjialeApi | None = entry_data.get("api")
        if api:
            await hass.async_add_executor_job(api.close)
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok

"""万家乐集成的诊断信息导出。

HA 会在集成详情页提供「下载诊断信息」入口，用户无需开启 debug 日志、也无须重启，
即可导出一份用于排障的 JSON 快照。这里同时也是「用户报障」场景的主要取证入口。

脱敏约定（官方要求不得暴露密码、密钥、令牌、位置与个人信息）：
  - 键名类敏感字段统一交给 async_redact_data 递归替换；
  - 账号（entry.title 里带着登录名）与设备标识属于「字符串里夹着敏感信息」，
    键名脱敏覆盖不到，因此单独处理：账号整体遮蔽，设备序列号只留末 4 位以便区分；
  - 服务端原始设备记录（_raw）整体不入档：字段由云平台决定，
    可能夹带未知的账号相关字段，只挑显式字段导出。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.loader import async_get_integration

from .api import WanjialeDevice
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

# 需要整体替换为 **REDACTED** 的敏感键名（递归匹配嵌套字典）
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_IMEI,
    "api_key",
    "session_key",
    "uid",
    "lan_pin",
    "lanPin",
    "login_name",
    "password_md5",
}


def _mask_title(title: Any) -> str:
    """遮蔽 entry.title 中的登录账号。

    账号可能是手机号也可能是邮箱，一律整体遮蔽，只保留前缀便于识别条目来源。
    """
    text = str(title or "")
    if "(" in text and text.endswith(")"):
        return f"{text[: text.index('(')]}(***)"
    return "***" if text else ""


def _mask_tail(value: Any, keep: int = 4) -> str:
    """只留末尾若干位，在不暴露完整标识的前提下保留设备间可区分性。"""
    text = str(value or "")
    if not text:
        return ""
    return f"***{text[-keep:]}" if len(text) > keep else "***"


def _device_snapshot(dev: WanjialeDevice) -> Dict[str, Any]:
    """单台设备的诊断快照：元信息 + 能力判定依据 + 原始与解析后状态。"""
    return {
        "did": _mask_tail(dev.did),
        "name": dev.name,
        "category": dev.category_cn,
        "model": dev.model,
        "model_id": dev.model_id,
        "product": dev.product,
        "firm": dev.firm,
        "device_version": dev.ver,
        "online": dev.online,
        "lan": {
            "available": dev.is_lan_available(),
            "host": dev.local_host,
            "port": dev.local_port,
            "pin_loaded": bool(dev.lan_pin),
        },
        "capabilities": {
            "matched": dev.caps.matched,
            "describe": dev.caps.describe(),
            "template_features": sorted(dev.caps.template_features),
        },
        "features": sorted(dev.features),
        "points": sorted(dev.points),
        "state": dev.state,
        "parsed": dev.parsed,
    }


def _get_api(hass: HomeAssistant, entry: ConfigEntry) -> Optional[Any]:
    """取出该 config entry 的 WanjialeApi；未初始化时返回 None。"""
    return hass.data.get(DOMAIN, {}).get(entry.entry_id, {}).get("api")


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> Dict[str, Any]:
    """导出 config entry 级诊断信息。"""
    integration = await async_get_integration(hass, DOMAIN)
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    api = entry_data.get("api")
    coordinator = entry_data.get("coordinator")

    diagnostics: Dict[str, Any] = {
        "integration": {
            "domain": DOMAIN,
            "version": integration.manifest.get("version"),
        },
        "entry": {
            "title": _mask_title(entry.title),
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "settings": {
            "scan_interval_seconds": DEFAULT_SCAN_INTERVAL,
            "platforms": list(PLATFORMS),
            "control_rights_enabled": bool(entry.options.get(CONF_ENABLE_CONTROL_RIGHTS, False)),
            "feature_force_on": list(entry.options.get(CONF_FEATURE_FORCE_ON) or []),
            "feature_force_off": list(entry.options.get(CONF_FEATURE_FORCE_OFF) or []),
        },
    }

    if api is None or coordinator is None:
        # setup 失败正是用户最需要这份文件的时候，此时同样要能导出
        diagnostics["note"] = "集成尚未完成初始化（setup 未成功或已卸载）"
        return async_redact_data(diagnostics, TO_REDACT)

    last_exception = getattr(coordinator, "last_exception", None)
    protocol = api.protocol

    diagnostics["coordinator"] = {
        "last_update_success": getattr(coordinator, "last_update_success", None),
        "last_exception": str(last_exception) if last_exception else None,
    }
    diagnostics["connection"] = {
        "cloud": {
            "connected": getattr(protocol, "_socket", None) is not None,
            "server": f"{protocol.server_ip}:{protocol.server_port}",
            "heartbeat_interval_seconds": protocol._heartbeat_interval,
        },
        "lan": {
            "connected": getattr(protocol, "_local_socket", None) is not None,
            "pin_loaded": bool(getattr(protocol, "_local_lan_pin", None)),
        },
        "credentials": {
            "uid_loaded": bool(protocol.uid),
            "api_key_loaded": bool(protocol.api_key),
            "session_key_loaded": bool(protocol.session_key),
        },
    }
    diagnostics["devices"] = [_device_snapshot(dev) for dev in api.devices]

    return async_redact_data(diagnostics, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> Dict[str, Any]:
    """导出单台设备的诊断信息。"""
    api = _get_api(hass, entry)
    if api is None:
        return {"note": "集成尚未完成初始化（setup 未成功或已卸载）"}

    did = next(
        (
            str(identifier[1])
            for identifier in device.identifiers
            if identifier and identifier[0] == DOMAIN
        ),
        None,
    )
    dev = api.get_device_by_did(did) if did else None
    if dev is None:
        return {"note": "未在设备列表中找到该设备", "did": _mask_tail(did)}

    return async_redact_data(_device_snapshot(dev), TO_REDACT)

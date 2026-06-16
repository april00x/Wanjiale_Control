"""万家乐集成的配置流（UI 向导）。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResult

from .const import CONF_IMEI, CONF_PASSWORD, CONF_USERNAME, DOMAIN
from .protocol import WanjialeProtocol

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: Dict[str, Any]) -> Optional[str]:
    """校验账号密码。返回错误字符串；None 表示成功。"""
    username = data[CONF_USERNAME]
    password = data[CONF_PASSWORD]
    imei = data.get(CONF_IMEI, "")

    protocol = WanjialeProtocol(username=username, password=password, imei=imei)
    try:
        await hass.async_add_executor_job(protocol.login)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("wanjiale login failed: %s", exc)
        msg = str(exc).lower()
        if "password" in msg or "uid" in msg or "未能从响应中解析出 uid" in msg:
            return "invalid_auth"
        return "cannot_connect"
    return None


class WanjialeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for wanjiale."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        errors: Dict[str, str] = {}

        if user_input is not None:
            # 避免重复账号
            for entry in self._async_current_entries():
                if entry.data.get(CONF_USERNAME) == user_input[CONF_USERNAME]:
                    return self.async_abort(reason="already_configured")

            err = await validate_input(self.hass, user_input)
            if err is None:
                return self.async_create_entry(
                    title=f"万家乐 ({user_input[CONF_USERNAME]})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_IMEI: user_input.get(CONF_IMEI, ""),
                    },
                )
            errors["base"] = err

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Optional(CONF_IMEI, default=""): str,
            }
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

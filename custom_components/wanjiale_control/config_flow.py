"""万家乐集成的配置流（UI 向导）与选项流。"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv

from .capabilities import feature_options
from .const import (
    CONF_ENABLE_CONTROL_RIGHTS,
    CONF_FEATURE_FORCE_OFF,
    CONF_FEATURE_FORCE_ON,
    CONF_IMEI,
    CONF_PASSWORD,
    CONF_USERNAME,
    DOMAIN,
)
from .protocol import WanjialeProtocol

_LOGGER = logging.getLogger(__name__)


async def validate_input(hass: HomeAssistant, data: Dict[str, Any]) -> Optional[str]:
    """校验账号密码。返回错误字符串；None 表示成功。"""
    protocol = WanjialeProtocol(
        username=data[CONF_USERNAME],
        password=data[CONF_PASSWORD],
        imei=data.get(CONF_IMEI, ""),
    )
    try:
        await hass.async_add_executor_job(protocol.login)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "password" in msg or "uid" in msg or "未能从响应中解析出 uid" in msg:
            # 输错密码属于正常用户操作，错误已通过 errors["base"] 在 UI 上呈现
            _LOGGER.debug("账号校验失败（凭据错误）: %s", exc)
            return "invalid_auth"
        # 连接类失败用户只看到「连接失败」，原因必须落进日志才能排查
        _LOGGER.warning("账号校验失败（无法连接）: %s", exc, exc_info=True)
        return "cannot_connect"
    return None


class WanjialeConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for wanjiale."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: config_entries.ConfigEntry) -> "WanjialeOptionsFlow":
        return WanjialeOptionsFlow()

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
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class WanjialeOptionsFlow(config_entries.OptionsFlow):
    """选项流：功能覆盖 + 操作权开关。

    机型模板描述的是模板能力，与实机能力可能不一致，
    探测不到的功能可在此强制开启，误判的功能可强制关闭。
    """

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        # 值=裸功能名（与已存选项一致），标签=「品类:功能」（多品类下区分同名功能）
        choices = feature_options()
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_CONTROL_RIGHTS,
                    default=bool(options.get(CONF_ENABLE_CONTROL_RIGHTS, False)),
                ): bool,
                vol.Optional(
                    CONF_FEATURE_FORCE_ON,
                    default=list(options.get(CONF_FEATURE_FORCE_ON, [])),
                ): cv.multi_select(choices),
                vol.Optional(
                    CONF_FEATURE_FORCE_OFF,
                    default=list(options.get(CONF_FEATURE_FORCE_OFF, [])),
                ): cv.multi_select(choices),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)

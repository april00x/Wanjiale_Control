"""万家乐集成的配置流（UI 向导）与选项流。"""
from __future__ import annotations

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .capabilities import feature_choices
from .const import (
    CONF_DEVICE_DID,
    CONF_DEVICE_FEATURES,
    CONF_DEVICE_FORCE_OFF,
    CONF_DEVICE_FORCE_ON,
    CONF_ENABLE_CONTROL_RIGHTS,
    CONF_FEATURE_OVERRIDES,
    CONF_IMEI,
    CONF_PASSWORD,
    CONF_RESCAN_DEVICES,
    CONF_USERNAME,
    DOMAIN,
)
from .protocol import CredentialError, WanjialeProtocol
from . import migrate_legacy_overrides

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
    except CredentialError as exc:
        # 输错密码属于正常用户操作，错误已通过 errors["base"] 在 UI 上呈现
        _LOGGER.debug("账号校验失败（凭据错误）: %s", exc)
        return "invalid_auth"
    except Exception as exc:  # noqa: BLE001
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
    """选项流：账号级设置 + 按设备的功能覆盖。

    机型模板描述的是模板能力，与实机能力可能不一致：探测不到的功能可
    强制开启，误判的功能可强制关闭。覆盖按设备保存（同一账号下的同类
    多台设备互不影响），因此需要先选设备再勾功能。
    """

    def __init__(self) -> None:
        super().__init__()
        self._selected_did: str = ""

    # ------------------------------------------------------------------
    # 入口：账号级设置 / 按设备设置功能
    # ------------------------------------------------------------------
    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        return self.async_show_menu(
            step_id="init", menu_options=["account", "device"]
        )

    # ------------------------------------------------------------------
    # 账号级设置
    # ------------------------------------------------------------------
    async def async_step_account(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        options = self._base_options()
        if user_input is not None:
            # 「重新获取设备」是一次性动作：只把 entry_id 放进内存集合供 setup 消费，
            # 必须在写 options 前摘除，否则会被持久化并在每次启动都放开新设备。
            if user_input.pop(CONF_RESCAN_DEVICES, False):
                self.hass.data.setdefault(DOMAIN, {}).setdefault(
                    "_rescan_once", set()
                ).add(self.config_entry.entry_id)
            options.update(user_input)
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ENABLE_CONTROL_RIGHTS,
                    default=bool(options.get(CONF_ENABLE_CONTROL_RIGHTS, False)),
                ): bool,
                # 每次默认 False：只在本次保存时生效，不参与 options 持久化
                vol.Optional(CONF_RESCAN_DEVICES, default=False): bool,
            }
        )
        return self.async_show_form(step_id="account", data_schema=schema)

    # ------------------------------------------------------------------
    # 按设备设置功能：先选设备
    # ------------------------------------------------------------------
    async def async_step_device(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        devices = self._devices()
        if not devices:
            # 集成未加载（初始化失败/重试中）时拿不到设备清单，无法按设备配置
            return self.async_abort(reason="not_loaded")

        if user_input is not None:
            did = str(user_input.get(CONF_DEVICE_DID) or "")
            if not any(str(getattr(dev, "did", "")) == did for dev in devices):
                return self.async_abort(reason="device_gone")
            self._selected_did = did
            return await self.async_step_device_detail()

        schema = vol.Schema(
            {
                vol.Required(CONF_DEVICE_DID): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            {"value": str(dev.did), "label": label}
                            for dev, label in self._device_labels(devices)
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                )
            }
        )
        return self.async_show_form(step_id="device", data_schema=schema)

    # ------------------------------------------------------------------
    # 按设备设置功能：勾选该设备的功能覆盖
    # ------------------------------------------------------------------
    async def async_step_device_detail(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        dev = next(
            (d for d in self._devices() if str(getattr(d, "did", "")) == self._selected_did),
            None,
        )
        if dev is None:
            return self.async_abort(reason="device_gone")

        # 选项只列该设备品类的功能：同名功能在不同品类下语义不同，
        # 按设备配置后不再需要品类前缀消歧。
        caps = getattr(dev, "caps", None)
        product = str(getattr(caps, "product", "") or getattr(dev, "product", "") or "")
        choices = {name: name for name in feature_choices(product)}
        if not choices:
            return self.async_abort(reason="no_features")

        options = self._base_options()
        overrides = options.get(CONF_FEATURE_OVERRIDES) or {}
        stored = overrides.get(self._selected_did) or {}
        # 当前实际生效的功能（已包含现有覆盖），即多选框的默认勾选状态
        active = {str(name) for name in getattr(dev, "features", None) or ()}

        if user_input is not None:
            selected = {
                str(name)
                for name in user_input.get(CONF_DEVICE_FEATURES) or []
                if str(name) in choices
            }
            # 只把「与当前实际状态不同」的功能记为覆盖，未改动的保持原覆盖/自动判定：
            # 若把整张列表全量写成 on/off，以后能力表新增的功能会被永久关掉。
            new_on = {str(n) for n in stored.get(CONF_DEVICE_FORCE_ON) or [] if str(n) in choices}
            new_off = {str(n) for n in stored.get(CONF_DEVICE_FORCE_OFF) or [] if str(n) in choices}
            for name in choices:
                if name in selected and name not in active:
                    new_on.add(name)
                    new_off.discard(name)
                elif name not in selected and name in active:
                    new_off.add(name)
                    new_on.discard(name)
            entry: Dict[str, List[str]] = {}
            if new_on:
                entry[CONF_DEVICE_FORCE_ON] = sorted(new_on)
            if new_off:
                entry[CONF_DEVICE_FORCE_OFF] = sorted(new_off)
            overrides = dict(overrides)
            if entry:
                overrides[self._selected_did] = entry
            else:
                # 与自动判定完全一致 = 撤销该设备的覆盖
                overrides.pop(self._selected_did, None)
            options[CONF_FEATURE_OVERRIDES] = overrides
            return self.async_create_entry(title="", data=options)

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_DEVICE_FEATURES,
                    default=[name for name in choices if name in active],
                ): cv.multi_select(choices),
            }
        )
        return self.async_show_form(
            step_id="device_detail",
            data_schema=schema,
            description_placeholders={
                "device": str(getattr(dev, "name", self._selected_did)),
            },
        )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _devices(self) -> List[Any]:
        """取本 entry 已加载的设备（需要 did 与 product 才能按设备配置）。"""
        entry_data = (self.hass.data.get(DOMAIN) or {}).get(self.config_entry.entry_id) or {}
        api = entry_data.get("api")
        return list(getattr(api, "devices", None) or [])

    def _base_options(self) -> Dict[str, Any]:
        """当前 options 的副本，并把旧版账号级覆盖迁移为设备级结构。

        迁移只发生在内存：用户保存任一步后才会落库；直接取消则旧值保持原样，
        下次进入选项流会再次迁移（幂等）。
        """
        options = migrate_legacy_overrides(dict(self.config_entry.options), self._devices())
        if not isinstance(options.get(CONF_FEATURE_OVERRIDES), dict):
            options[CONF_FEATURE_OVERRIDES] = {}
        return options

    @staticmethod
    def _device_labels(devices: List[Any]) -> List[Any]:
        """设备下拉项：名称 + 品类/机型，重名时补 did 末 4 位区分。"""
        names = [str(getattr(dev, "name", "") or getattr(dev, "did", "")) for dev in devices]
        duplicated = {name for name, count in Counter(names).items() if count > 1}
        labels = []
        for dev, name in zip(devices, names):
            did = str(getattr(dev, "did", ""))
            detail = "·".join(
                part
                for part in (
                    str(getattr(dev, "category_cn", "") or ""),
                    str(getattr(dev, "model", "") or ""),
                )
                if part
            )
            if name in duplicated and did:
                detail = f"{detail}…{did[-4:]}" if detail else f"…{did[-4:]}"
            labels.append((dev, f"{name}（{detail}）" if detail else name))
        return labels

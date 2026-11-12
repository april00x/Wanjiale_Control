"""万家乐 Home Assistant 集成。"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, Dict, List, Optional, Sequence, Set

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .api import WanjialeApi
from .capabilities import OVERRIDE_OFF, OVERRIDE_ON, ensure_plugins
from .const import (
    CONF_DEVICE_FORCE_OFF,
    CONF_DEVICE_FORCE_ON,
    CONF_ENABLE_CONTROL_RIGHTS,
    CONF_FEATURE_FORCE_OFF,
    CONF_FEATURE_FORCE_ON,
    CONF_FEATURE_OVERRIDES,
    CONF_IMEI,
    CONF_PASSWORD,
    CONF_USERNAME,
    CORE_PLATFORMS,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from . import registry
from .protocol import WanjialeProtocol

_LOGGER = logging.getLogger(__name__)

# 设备级覆盖里两个列表键对应的三态值：先 off 后 on，两者冲突时以「强制开启」为准
_OVERRIDE_LIST_KEYS = ((CONF_DEVICE_FORCE_OFF, OVERRIDE_OFF), (CONF_DEVICE_FORCE_ON, OVERRIDE_ON))


def _split_legacy_name(value: Any, product: str) -> Optional[str]:
    """旧账号级覆盖项 → 裸功能名；品类前缀与设备不符时返回 None。"""
    text = str(value or "").strip()
    if not text:
        return None
    if ":" in text:
        prefix, name = text.split(":", 1)
        if product and prefix != product:
            return None
        return name.strip() or None
    return text


def migrate_legacy_overrides(
    options: Dict[str, Any], devices: Optional[Sequence[Any]] = None
) -> Dict[str, Any]:
    """把旧版账号级覆盖（`feature_force_on/off`）并入设备级结构。

    旧配置没有设备维度，只能按账号下设备数判定：
    - 恰好一台设备：整体归给该设备，品类前缀与设备不符的条目丢弃；
    - 零台或多台设备：无法判定归属，整组丢弃并记 warning，需在选项页按设备重设。

    返回新的 options 副本（旧键已摘除）；不修改传入对象。
    """
    result = dict(options or {})
    legacy_on = result.pop(CONF_FEATURE_FORCE_ON, None)
    legacy_off = result.pop(CONF_FEATURE_FORCE_OFF, None)
    if not legacy_on and not legacy_off:
        return result

    targets = list(devices or ())
    if len(targets) != 1:
        _LOGGER.warning(
            "检测到旧版账号级功能覆盖，但当前账号下有 %d 台设备，无法判定归属，"
            "已忽略；请在选项页按设备重新设置",
            len(targets),
        )
        return result

    dev = targets[0]
    did = str(getattr(dev, "did", "") or "")
    # 能力解析后的 product 才是最终品类（云端 product 字段可能为空、由机型反查得出）
    caps = getattr(dev, "caps", None)
    product = str(getattr(caps, "product", "") or getattr(dev, "product", "") or "")
    if not did:
        return result

    migrated: Dict[str, List[str]] = {}
    for key, source in ((CONF_DEVICE_FORCE_OFF, legacy_off), (CONF_DEVICE_FORCE_ON, legacy_on)):
        names = [name for name in (_split_legacy_name(v, product) for v in source or ()) if name]
        if names:
            migrated[key] = sorted(set(names))
    if not migrated:
        return result

    overrides = dict(result.get(CONF_FEATURE_OVERRIDES) or {})
    merged = dict(overrides.get(did) or {})
    for key, names in migrated.items():
        merged[key] = sorted(set(merged.get(key) or []) | set(names))
    overrides[did] = merged
    result[CONF_FEATURE_OVERRIDES] = overrides
    _LOGGER.info(
        "旧版账号级功能覆盖已迁移到设备「%s」", getattr(dev, "name", did)
    )
    return result


def build_feature_overrides(
    options: Dict[str, Any], devices: Optional[Sequence[Any]] = None
) -> Dict[str, Dict[str, str]]:
    """把选项页的设备级覆盖换算成 `{did: {功能名: OVERRIDE_ON/OFF}}`。

    覆盖按设备保存，同类多台设备互不影响；旧版账号级配置先经
    `migrate_legacy_overrides` 归一（单设备账号可自动迁移）。
    """
    normalized = migrate_legacy_overrides(options, devices)
    raw = normalized.get(CONF_FEATURE_OVERRIDES) or {}
    overrides: Dict[str, Dict[str, str]] = {}
    if not isinstance(raw, dict):
        return overrides
    for did, item in raw.items():
        if not isinstance(item, dict):
            continue
        resolved: Dict[str, str] = {}
        for key, value in _OVERRIDE_LIST_KEYS:
            for name in item.get(key) or ():
                text = str(name or "").strip()
                if text:
                    resolved[text] = value
        if resolved:
            overrides[str(did)] = resolved
    return overrides


def platforms_for_entry() -> tuple:
    """本次要转发的实体平台 = 各品类声明的平台 ∪ 核心最小平台。

    必须在转发前算好：HA 要求平台清单在加载时确定，卸载要用同一份清单。
    新增品类只需在自己插件里声明 `platforms`，这里自动求并集，核心无需改动。
    """
    ensure_plugins()
    return tuple(sorted(set(CORE_PLATFORMS) | set(registry.all_platforms())))


def _spec_unique_ids(dev: Any, platform: str, spec: Any) -> Set[str]:
    """按各平台的 unique_id 规则，推导一条 spec 会生成的实体标识。"""
    if platform == "time":
        key = f"schedule_{spec.kind}_{spec.group}-{spec.field}"
    else:
        key = str(getattr(spec, "key", "") or "")
    if not key:
        return set()
    roles = [dev] if getattr(spec, "device_level", False) else dev.role_views()
    prefix = dev.unique_id()
    return {
        f"{prefix}-{key}{str(getattr(role, 'role_suffix', '') or '')}" for role in roles
    }


def prune_disabled_entities(hass: HomeAssistant, entry: ConfigEntry, api: WanjialeApi) -> int:
    """删除「因功能被关闭而不再创建」的实体注册表条目。

    HA 对平台不再提供的实体只标记为不可用（restored）并保留 registry 条目，
    不刷新页面它就一直在设备页里。这里只清理能精确归因的条目：unique_id
    由该设备某个被强制关闭的功能生成；探测波动导致本次未创建、但功能并未
    关闭的实体不会被误删。

    返回删除的实体数量。
    """
    entity_registry = er.async_get(hass)
    entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    if not entries:
        return 0

    removable: Set[str] = set()
    platforms = platforms_for_entry()
    for dev in api.devices:
        off = api.disabled_features(dev.did)
        if not off:
            continue
        prefix = dev.unique_id()
        roles = dev.role_views()
        # 主实体的前置条件：water_heater 依赖「开关机」；climate 依赖供暖两个功能之一
        if "开关机" in off:
            removable.update(
                f"{prefix}-water_heater{str(getattr(role, 'role_suffix', '') or '')}"
                for role in roles
            )
        if ({"供暖温度", "供暖开关"} & off) and not (
            dev.has_feature("供暖温度") or dev.has_feature("供暖开关")
        ):
            removable.update(
                f"{prefix}-climate{str(getattr(role, 'role_suffix', '') or '')}"
                for role in roles
            )
        for platform in platforms:
            for spec in registry.platform_specs(dev, platform):
                feature = getattr(spec, "feature", None)
                if feature is not None and feature in off:
                    removable.update(_spec_unique_ids(dev, platform, spec))

    removed = sorted(item.entity_id for item in entries if item.unique_id in removable)
    for entity_id in removed:
        entity_registry.async_remove(entity_id)
    if removed:
        _LOGGER.info(
            "已移除被关闭功能的实体 %d 个：%s", len(removed), "、".join(removed)
        )
    return len(removed)


def known_device_dids(hass: HomeAssistant, entry: ConfigEntry) -> Set[str]:
    """从设备注册表取本配置条目已创建的设备 did（identifier 为 `(DOMAIN, did)`）。"""
    device_registry = dr.async_get(hass)
    return {
        identifier[1]
        for device_entry in dr.async_entries_for_config_entry(
            device_registry, entry.entry_id
        )
        for identifier in device_entry.identifiers
        if identifier and identifier[0] == DOMAIN
    }


def consume_rescan_flag(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """消费选项页「重新获取设备」的一次性内存标记（命中即清除）。"""
    pending = hass.data.get(DOMAIN, {}).get("_rescan_once")
    if not pending or entry.entry_id not in pending:
        return False
    pending.discard(entry.entry_id)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """基于 config entry 启动集成。"""
    username = entry.data[CONF_USERNAME]
    password = entry.data[CONF_PASSWORD]
    imei = entry.data.get(CONF_IMEI, "")

    protocol = WanjialeProtocol(username=username, password=password, imei=imei)
    api = WanjialeApi(
        protocol,
        enable_control_rights=bool(entry.options.get(CONF_ENABLE_CONTROL_RIGHTS, False)),
        # 登录名即账号手机号，操作权抢占需要它
        login_name=username,
    )

    try:
        # 设备准入：注册表为空（首次安装）或用户勾选「重新获取设备」时允许新设备；
        # 其余情况只加载注册表里已有的设备，云端多出的跳过。
        # 标记先消费再判断，避免注册表为空时残留到下一次重载。
        rescan = consume_rescan_flag(hass, entry)
        known_dids = known_device_dids(hass, entry)
        allow_new = rescan or not known_dids
        # 登录 + 加载设备在 executor 中完成（同步 socket）
        await hass.async_add_executor_job(api.login)
        await hass.async_add_executor_job(api.load_devices, allow_new, known_dids)
        _LOGGER.info("已加载 %d 台设备", len(api.devices))
        # 功能覆盖按设备保存，必须等设备加载完、拿到 did 之后才能换算
        api.set_feature_overrides(build_feature_overrides(entry.options, api.devices))
        # 尝试建立长连接（可选，失败不影响启动）
        try:
            await hass.async_add_executor_job(api.connect_server)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("建立长连接失败，设备状态与控制可能不可用", exc_info=True)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("初始化失败，交由 Home Assistant 自动重试", exc_info=True)
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

    # 平台集合 = 各品类声明的平台 ∪ 核心最小平台。
    # 必须在转发前算好：HA 要求平台清单在加载时确定，且卸载要用同一份清单。
    platforms = platforms_for_entry()
    _LOGGER.debug("本次加载的实体平台：%s", ", ".join(platforms))

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "api": api,
        "coordinator": coordinator,
        "platforms": platforms,
    }

    await hass.config_entries.async_forward_entry_setups(entry, platforms)
    # 平台已按本次功能集合创建实体：清掉「因功能被关闭而不再提供」的注册表残留，
    # 否则设备页会一直挂着灰色不可用实体（HA 不会自动删除它们）。
    prune_disabled_entities(hass, entry, api)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """选项变更后重载集成（功能集合需要重新计算）。"""
    _LOGGER.info("选项已变更，重载集成以重新计算功能集合")
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """卸载集成时关闭长连接与局域网连接。"""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    # 用加载时的同一份平台清单卸载（HA 要求前后一致）
    platforms = entry_data.get("platforms") or CORE_PLATFORMS
    unload_ok = await hass.config_entries.async_unload_platforms(entry, platforms)
    if unload_ok:
        api: WanjialeApi | None = entry_data.get("api")
        if api:
            await hass.async_add_executor_job(api.close)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.info("集成已卸载")
    return unload_ok

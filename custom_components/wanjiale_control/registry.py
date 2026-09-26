"""品类注册表：`product` 到 `CategoryPlugin` 的映射。

核心层只依赖本模块暴露的接口，不认识任何具体品类；品类模块在被导入时通过
`register_plugin()` 自注册，`categories/__init__.py` 统一导入，避免手工维护清单。

本模块零 homeassistant 依赖，可被测试与离线脚本直接装载。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Sequence, Tuple

_LOGGER = logging.getLogger(__name__)

# 契约版本：今后新增字段一律带默认值，保证旧品类插件不被破坏；
# 插件声明了比核心更高的版本时拒绝加载（核心不理解其语义）。
CONTRACT_VERSION = 1


@dataclass(frozen=True)
class CategoryPlugin:
    """一个品类的全部「变化点」。核心层只依赖这个接口。

    「不变的部分」（帧格式、加密、长连接、状态缓存、门控、实体骨架）留在核心层，
    本对象只承载 dvid 语义、机型点位、命令取值、值编码、故障表与平台需求。
    """

    # ---- 标识 ----
    product: str                      # "001" / "002" / "008" …
    category_cn: str                  # "燃气热水器" …

    # ---- 设备与平台 ----
    device_class: type                # 该品类的设备类（WanjialeDevice 子类）
    platforms: FrozenSet[str]         # {"water_heater", "sensor", …}

    # ---- 能力与数据文件 ----
    features: Mapping[str, Mapping[str, Any]]
    """功能元数据：{name: {platform, requires_power, fault_exempt, probe_dvids}}。"""

    capability_file: str              # "capability_002.json"（同目录，须带 schemaVersion）
    faults_file: str                  # "faults_002.json"

    # ---- 语义（品类特有，全部落在此处） ----
    parse_state: Callable[..., Dict[str, Any]]
    """(state, fids, profile) -> parsed。

    只输出「有来源」的键，使实体能区分「未知」与「值为 0」。
    """

    encode_value: Callable[..., Dict[str, str]]
    """语义动作到 {dvid: 绝对字符串} 的映射。

    负责 32 位复合包、档位、单值等的值计算，不涉及报文信封
    （单键/多键由传输层按设备代际自动选择）。
    """

    # ---- 点位与故障 ----
    base_points: FrozenSet[str]
    """基线点位：设备从未上报时的实体创建依据。"""

    fault_points: Mapping[str, Any]
    """故障来源：{"device": ("12", "117"), "cloud": "fids"}。"""

    fault_none: Tuple[Any, ...]
    """无故障值，例 (0, 255) / (255,)。元组表示「多个值都算正常」。"""

    # ---- 可选（一律带默认值，保证旧插件不被破坏） ----
    baseline_families: FrozenSet[str] = frozenset()
    """`base_points` 生效的家族集合（如 001 的 {"F01"}）。

    能力表的 `family` 字段命中该集合时使用 `base_points`，否则退回核心点位兜底。
    留空表示「始终使用 base_points」。该语义必须由插件声明，核心层不得写死家族名。
    """

    specs: Mapping[str, Sequence[Any]] = field(default_factory=dict)
    """平台到 spec 表的映射。spec 表放品类目录，平台模块只做通用渲染，
    因此新增品类无需改动任何平台模块。
    例：{"switch": (SwitchSpec(...), ...), "select": (SelectSpec(...), ...)}
    """

    always_on_features: FrozenSet[str] = frozenset()
    """无条件启用的功能名（主实体依赖，不参与模板/探测判定）。

    该判定属品类口径，由插件声明，核心层不得写死功能名。
    """

    encoding_note: str = ""
    """该品类值编码方式的文字说明（供排障阅读，不参与逻辑）。"""

    temp_also_mode: Tuple[str, ...] = ()
    """温度与模式共用 dpid 的高危机型（例 008 的 9 个机型），默认不收录。"""

    readonly_models: FrozenSet[str] = frozenset()
    """只读机型的能力表主键集合（`firm_product_model`，如 `WJL_001_OYY`）。

    命中者即使 `product` 属本品类也不认领（`_resolve_plugin` 返回 None）：
    设备类回落通用只读 `WanjialeDevice`，只出一个「原始状态」诊断实体，
    且 `_send()` 拒绝下发。用于机型确实属本产品码、但活动协议是另一路体系、
    按本品位下发会误控的场景（001 的 OYY/HPZ 属 101+ 第二路体系）。
    默认空集，旧插件不受影响。
    """

    control_rights: bool = False
    """是否启用 247/248 多端操作权（001/002 为 True）。"""

    contract_version: int = CONTRACT_VERSION


class PluginConflictError(RuntimeError):
    """同一 product 被两个插件注册时抛出，必须 fail fast，不允许静默覆盖。"""


_REGISTRY: Dict[str, CategoryPlugin] = {}


def register_plugin(plugin: CategoryPlugin) -> CategoryPlugin:
    """注册一个品类插件；重复注册同一 product 立即报错。

    可作装饰器使用：`@register_plugin`（作用于已构造好的实例）。
    """
    if not plugin.product:
        raise ValueError("CategoryPlugin.product 不能为空")
    if plugin.contract_version > CONTRACT_VERSION:
        raise ValueError(
            f"品类 {plugin.product} 声明契约版本 {plugin.contract_version}，"
            f"高于核心支持的 {CONTRACT_VERSION}，拒绝加载"
        )
    existing = _REGISTRY.get(plugin.product)
    if existing is not None and existing is not plugin:
        raise PluginConflictError(
            f"品类 {plugin.product} 已被「{existing.category_cn}」注册，"
            f"不允许被「{plugin.category_cn}」覆盖"
        )
    _REGISTRY[plugin.product] = plugin
    _LOGGER.debug(
        "注册品类插件 %s(%s)：平台=%s 契约版本=%d",
        plugin.product, plugin.category_cn,
        ",".join(sorted(plugin.platforms)) or "无", plugin.contract_version,
    )
    return plugin


def by_product(code: str) -> Optional[CategoryPlugin]:
    """按产品码取插件；未注册返回 None（调用方据此降级为通用只读设备）。"""
    return _REGISTRY.get(str(code or "").strip())


def all_products() -> Tuple[str, ...]:
    """已注册的全部产品码（升序）。"""
    return tuple(sorted(_REGISTRY))


def all_platforms() -> FrozenSet[str]:
    """所有已注册品类需要的 HA 平台并集。

    `async_setup_entry` 内据此计算后传给 `async_forward_entry_setups`
    （HA 要求平台清单在加载时确定）。
    """
    platforms: set = set()
    for plugin in _REGISTRY.values():
        platforms.update(plugin.platforms)
    return frozenset(platforms)


def registered() -> Mapping[str, CategoryPlugin]:
    """只读视图：当前已注册的全部插件。"""
    return dict(_REGISTRY)


def specs_for_platform(plugin: Optional[CategoryPlugin], platform: str) -> Tuple[Any, ...]:
    """取某插件在某平台上的 spec 表。"""
    if plugin is None:
        return ()
    return tuple((plugin.specs or {}).get(platform) or ())


def platform_specs(device: Any, platform: str) -> Tuple[Any, ...]:
    """平台模块的取值入口：`registry.platform_specs(dev, "switch")`。

    平台模块因此不认识任何品类，只做「把 spec 渲染成实体」。
    """
    return specs_for_platform(getattr(device, "plugin", None), platform)


def clear() -> None:
    """清空注册表（仅测试用）。"""
    _REGISTRY.clear()

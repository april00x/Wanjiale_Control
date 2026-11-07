"""机型能力解析：内置能力表、运行时探测、选项页覆盖三层。

内置能力表描述的是机型模板能力，不等于实机能力（模板可能声明了设备并不具备的点位）；
运行时探测以设备真实上报过的 dvid 做确认（功能关闭时状态位为 0，点位却仍存在，
只信探测会漏判）；两者都无法判定的边界机型交给选项页覆盖。

品类无关：表文件名、功能定义、基线点位、无条件启用项由 `CategoryPlugin` 声明
（见 `categories/<品类>/plugin.py`），本模块按 `product` 查注册表并按品类缓存，
不 import 任何具体品类的模块。

对外暴露 resolve()（设备记录转 DeviceCapabilities）与 resolve_features()
（能力 + 探测结果 + 用户覆盖，得到最终启用的功能名集合）。
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from . import registry
from .registry import CategoryPlugin

_LOGGER = logging.getLogger(__name__)

# 能力表结构版本：表内 schemaVersion 与核心不一致时拒绝加载并降级为纯运行时探测。
# 缺省（未声明）视为兼容，便于旧数据文件平滑迁移。
SCHEMA_VERSION = 1

# 选项页三态
OVERRIDE_AUTO = "auto"
OVERRIDE_ON = "on"
OVERRIDE_OFF = "off"

# 未声明基线点位时的跨机型核心点位兜底：
# 保证「设备从未上报」时主实体依然稳定存在（有插件的品类用 plugin.base_points）。
CORE_POINTS: frozenset = frozenset({"4", "10", "17", "18", "24", "28"})

# 品类插件是否已完成加载（惰性、幂等）
_plugins_loaded = False


# ======================================================================
# 品类插件装载与查表
# ======================================================================
def _ensure_plugins() -> None:
    """幂等惰性加载品类插件。

    能力解析依赖注册表，而注册表由 `categories.load_all()` 填充；惰性加载可避免
    setup 顺序变化导致品类能力表缺失、实体集体消失；单个品类加载失败不影响其它品类。
    """
    global _plugins_loaded
    if _plugins_loaded:
        return
    _plugins_loaded = True
    try:
        from . import categories

        categories.load_all()
    except Exception:  # noqa: BLE001
        _LOGGER.warning("品类插件加载失败", exc_info=True)


def ensure_plugins() -> None:
    """确保品类插件已加载（幂等）。

    供核心层的设备分类等路径调用，使其不必依赖 `async_setup_entry` 的调用顺序。
    """
    _ensure_plugins()


def _plugin_for(product: str = "") -> Optional[CategoryPlugin]:
    """按产品码精确取插件；未注册返回 None。

    没有「仅一个品类就按它解析」的兜底，调用方必须显式给出品类
    （设备门控传 `self.plugin.product`）。需要全局视角的少数调用
    （选项页功能列表、故障文案查表）改用 `_features_union()` / `_faults_for_model()`，
    属于显式的跨品类查询。
    """
    _ensure_plugins()
    return registry.by_product(product)


def _load_json(filename: str) -> Dict[str, Any]:
    """读取集成目录下的 JSON；失败返回空表（不阻断 setup）。"""
    path = os.path.join(os.path.dirname(__file__), filename)
    try:
        with open(path, encoding="utf-8") as fp:
            data = json.load(fp)
    except (OSError, ValueError):
        _LOGGER.warning("数据文件加载失败：%s", path, exc_info=True)
        return {}
    if not isinstance(data, dict):
        _LOGGER.warning("数据文件结构异常（顶层非对象）：%s", path)
        return {}
    return data


_TABLE_CACHE: Dict[str, Dict[str, Any]] = {}
_FAULT_CACHE: Dict[str, Dict[str, Any]] = {}


def _table_for(product: str = "") -> Dict[str, Any]:
    """按品类加载并缓存能力表（文件名由插件声明）。"""
    plugin = _plugin_for(product)
    if plugin is None:
        return {}
    if plugin.product not in _TABLE_CACHE:
        data = _load_json(plugin.capability_file)
        version = data.get("schemaVersion")
        if version is not None:
            try:
                matched = int(version) == SCHEMA_VERSION
            except (TypeError, ValueError):
                matched = False
            if not matched:
                _LOGGER.warning(
                    "能力表 %s 的 schemaVersion=%s 与核心支持的 %d 不匹配，"
                    "已忽略该表（降级为纯运行时探测）",
                    plugin.capability_file, version, SCHEMA_VERSION,
                )
                data = {}
        _TABLE_CACHE[plugin.product] = data
    return _TABLE_CACHE[plugin.product]


def _faults_for(product: str = "") -> Dict[str, Any]:
    """按品类加载并缓存故障文案表，返回 `devices` 段（机型到文案的映射）。"""
    plugin = _plugin_for(product)
    if plugin is None:
        return {}
    if plugin.product not in _FAULT_CACHE:
        data = _load_json(plugin.faults_file)
        _FAULT_CACHE[plugin.product] = dict(data.get("devices") or {})
    return _FAULT_CACHE[plugin.product]


def _faults_for_model(model_key: str, product: str = "") -> Dict[str, Any]:
    """按机型主键取故障文案条目。

    `firm_product_model` 全局唯一，因此未给品类时可跨已注册品类查询（命中即返回），
    不会产生歧义；显式给 `product` 则只查该品类。
    """
    if product:
        return _faults_for(product).get(model_key) or {}
    _ensure_plugins()
    for code in registry.all_products():
        entry = _faults_for(code).get(model_key)
        if entry:
            return entry
    return {}


def preload_faults(products: Iterable[str]) -> None:
    """在 executor 里预热故障文案表（供实体创建前调用）。

    故障实体在 `extra_state_attributes` 里调用 `fault_info()`，而 HA 在事件循环中
    读取该属性，首次读表会在事件循环里同步 `open()`，触发 HA 的 blocking call 告警。
    提前把表读进 `_FAULT_CACHE`，实体侧读的就只是内存。
    """
    for product in sorted({str(p) for p in products if p}):
        _faults_for(product)


def _features_for(product: str) -> Mapping[str, Mapping[str, Any]]:
    """按品类精确取功能定义（未注册品类返回空）。"""
    plugin = _plugin_for(product)
    return plugin.features if plugin is not None else {}


def _features_union() -> Mapping[str, Mapping[str, Any]]:
    """跨全部已注册品类的功能并集。

    仅用于没有品类上下文的调用（选项页功能列表等）。单品类时等价于直接查该插件；
    多品类时同名功能由后注册者覆盖。
    """
    _ensure_plugins()
    merged: Dict[str, Mapping[str, Any]] = {}
    for plugin in registry.registered().values():
        merged.update(plugin.features)
    return merged


def _features_for_query(product: str) -> Mapping[str, Mapping[str, Any]]:
    """有品类时精确查，无品类时取跨品类并集（仅兼容调用与全局列表用）。"""
    return _features_for(product) if product else _features_union()


def _always_on_features(product: str = "") -> frozenset:
    """按品类取无条件启用项。"""
    plugin = _plugin_for(product)
    return frozenset(plugin.always_on_features) if plugin is not None else frozenset()


@dataclass
class DeviceCapabilities:
    """单台设备的能力来源与模板能力。"""

    key: Optional[str] = None
    product: str = ""
    """认领该设备的品类：命中某品类能力表时为该插件的 product，
    否则为设备记录里的原始 product。

    设备分类以它为依据之一：表命中说明该品类确实收录了这台设备，
    因此设备记录的 `product` 字段异常时也能正确归类。
    """

    model_id: str = ""
    name: str = ""
    family: Optional[str] = None
    form: Optional[str] = None
    dvid_count: Optional[int] = None
    template_features: Set[str] = field(default_factory=set)
    template_modes: Dict[int, str] = field(default_factory=dict)
    source: str = "未收录"

    model_points: Dict[str, Any] = field(default_factory=dict)
    """机型级点位表：能力表 `devices[*].points` 原样透传（核心层只透传、不解读）。

    008 等品类的点位随机型变（开关/模式/温度/状态各有不同 dpid），必须随能力表一起
    下发到设备对象，供品类 `parse_state` 按机型取点。留空 → 001/002 行为不变。
    """

    @property
    def matched(self) -> bool:
        """是否命中内置能力表。"""
        return self.key is not None

    def describe(self) -> str:
        return (
            f"{self.source}(key={self.key or '-'} family={self.family or '-'} "
            f"form={self.form or '-'} 模板功能={len(self.template_features)})"
        )


def build_key(firm: str, product: str, model: str) -> Optional[str]:
    """拼出能力表主键 firm_product_model（如 WJL_001_RUB）。"""
    parts = [p.strip() for p in (firm, product, model)]
    if not all(parts):
        return None
    return "_".join(parts)


def _key_belongs_to(key: str, product: str) -> bool:
    """判断能力表主键 `firm_product_model` 是否属于某品类。

    仅比对产品段（第 2 段）。能力表当前混入了非本品类条目，按 `modelId` 反查归属时
    必须校验，否则别的品类的 modelId 会被本表认领而误判归属。

    firm 含下划线时判不出归属，该情形下不认领（宁可归为通用只读，也不误写设备）。
    """
    parts = str(key).split("_")
    return len(parts) >= 3 and parts[1] == product


def _detect_owner(
    firm: str, model: str, model_id: str
) -> Optional[CategoryPlugin]:
    """按机型在已注册品类的能力表中反查归属品类。

    仅在设备记录未给出可用 `product`（字段缺失或品类未注册）时使用。
    判据是机型确实被某品类以本品类的主键收录，且要求唯一命中；
    命中多个则视为无法判定，不猜（宁可归为通用只读，也不误写设备）。
    """
    if not model_id and not (firm and model):
        return None
    _ensure_plugins()
    hits: List[CategoryPlugin] = []
    for code in registry.all_products():
        plugin = registry.by_product(code)
        if plugin is None:
            continue
        table = _table_for(code)
        if firm and model:
            key = build_key(firm, code, model)
            if key and key in (table.get("devices") or {}):
                hits.append(plugin)
                continue
        if model_id:
            mapped = (table.get("byModelId") or {}).get(model_id)
            # 必须与「本品类自己的主键」一致：表内可能混入别的品类条目
            if mapped and _key_belongs_to(mapped, code):
                hits.append(plugin)
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _LOGGER.warning(
            "机型 firm=%s model=%s modelId=%s 同时命中 %s 的能力表，无法判定品类，按未收录处理",
            firm, model, model_id, "、".join(sorted(p.product for p in hits)),
        )
    return None


def resolve(
    firm: str,
    product: str,
    model: str,
    model_id: str = "",
) -> DeviceCapabilities:
    """由设备记录解析能力。优先用 firm_product_model，回退用 modelId 反查。

    `DeviceCapabilities.product` 返回认领该设备的品类（见该字段说明）。
    设备记录的 `product` 不可用时，按机型在已注册能力表中反查归属（`_detect_owner`）。
    """
    product = str(product or "").strip()
    plugin = _plugin_for(product)
    if plugin is None:
        plugin = _detect_owner(firm, model, model_id)
    # 表命中时以插件声明的 product 为准，避免设备记录字段异常导致分类丢失
    owner_product = plugin.product if plugin is not None else product
    table = _table_for(owner_product)
    flag_order: List[str] = list(table.get("flagOrder") or [])
    devices: Dict[str, Any] = dict(table.get("devices") or {})
    by_model_id: Dict[str, str] = dict(table.get("byModelId") or {})

    # 用「认领品类」拼主键：设备 product 字段异常时仍能命中 firm_product_model
    key = build_key(firm, owner_product, model)
    entry = devices.get(key) if key else None
    if entry is None and model_id:
        key = by_model_id.get(model_id)
        entry = devices.get(key) if key else None
    if entry is None:
        return DeviceCapabilities(
            key=None, product=owner_product, model_id=model_id, name="",
            source="未收录（仅用运行时探测）",
        )

    flags = str(entry.get("flags") or "")
    template = {
        name for idx, name in enumerate(flag_order)
        if idx < len(flags) and flags[idx] == "1"
    }
    modes = {
        int(item["id"]): item["name"]
        for item in (entry.get("modes") or [])
        if item.get("id") is not None and item.get("name")
    }
    caps = DeviceCapabilities(
        key=key,
        product=owner_product,
        model_id=entry.get("modelId") or model_id,
        name=entry.get("name") or "",
        family=entry.get("family"),
        form=entry.get("form"),
        dvid_count=entry.get("dvidCount"),
        model_points=dict(entry.get("points") or {}),
        template_features=template,
        template_modes=modes,
        source="内置能力表",
    )
    _LOGGER.debug("能力解析：%s", caps.describe())
    return caps


def observed_points(state: Dict[str, Any]) -> Set[str]:
    """把设备上报的 as 字典键规范成点位集合。"""
    return {str(k) for k in (state or {})}


def effective_points(caps: DeviceCapabilities, observed: Iterable[str]) -> Set[str]:
    """最终参与实体创建的「已知点位」= 实测点位 ∪ 基线点位。

    基线点位由插件声明：能力表的 `family` 命中 `plugin.baseline_families`
    时使用 `plugin.base_points`（如 001 的 F01 家族用完整 23 点基线），
    否则退回跨机型核心点位，避免为不存在的点位创建实体。

    插件声明 `baseline_from_model_points=True` 时（002），基线改由机型点位表
    （`caps.model_points`）递归收集，不再套用核心点位：002 的点位全部随能力表下发，
    同一 dpid 跨机型语义互斥，核心兜底点位会引入无意义实体。
    """
    points = {str(p) for p in observed}
    plugin = _plugin_for(caps.product)
    if plugin is not None and plugin.baseline_from_model_points:
        points |= _baseline_from_model_points(caps.model_points)
        return points
    families = frozenset(plugin.baseline_families) if plugin is not None else frozenset()
    if plugin is not None and (not families or caps.family in families):
        points |= set(plugin.base_points)
    else:
        points |= set(CORE_POINTS)
    return points


# 机型点位表里可直接取 dpid 的标量键（含只写命令 dpid）。
_BASELINE_SCALAR_KEYS = frozenset({
    "dpid", "read", "write", "set", "req", "high", "low",
    "list", "add", "del", "next", "total", "delete", "shared_temp",
})

# 机型点位表里「dpid 数组」的键（预约起止/使能/温度、复合包字节等）。
_BASELINE_SEQUENCE_KEYS = frozenset({"bytes", "start", "end", "enable", "temp", "packed"})


def _baseline_from_model_points(model_points: Any) -> Set[str]:
    """递归收集机型点位表中的 dpid。

    只收集「值是 dpid」的既定键，跳过 `mode.values` / `keep_warm.map` 这类
    「键是取值」的字典，避免把模式号、保温档位当成 dpid。
    `thermostats[]` 展开为 current/set/enable/fault/device_id。
    """
    found: Set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, int):
            found.add(str(value))
        elif isinstance(value, str) and value.isdigit():
            found.add(value)
        elif isinstance(value, dict):
            add(value.get("dpid"))

    def walk(node: Any) -> None:
        if not isinstance(node, dict):
            return
        for key, value in node.items():
            if key in _BASELINE_SCALAR_KEYS:
                add(value)
            elif key in _BASELINE_SEQUENCE_KEYS:
                if isinstance(value, (list, tuple)):
                    for item in value:
                        add(item)
            elif key == "thermostats":
                for item in value or ():
                    if isinstance(item, dict):
                        for sub in ("current", "set", "enable", "fault", "device_id"):
                            add(item.get(sub))
            elif isinstance(value, dict):
                # 嵌套点位组（如 `mode.cmd` / `schedule_*.master_cmd`）：继续向下找 dpid
                walk(value)

    walk(model_points if isinstance(model_points, dict) else {})
    return found



def resolve_features(
    caps: DeviceCapabilities,
    observed: Iterable[str],
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """计算最终启用的功能集合。

    判定优先级：选项强制关闭则不启用，选项强制开启则启用（可越过机型白名单）；
    其次按 `models` 机型白名单过滤（共用状态位在不同机型含义不同时使用）；
    再次无条件启用项；再次 probe_required 功能必须探测到点位（模板可能过度声明
    能力）；其余按模板声明或探测命中启用；extra 补充功能（能力表未收录）必须
    探测到点位。

    Returns:
        {功能名: 依据说明}，说明用于日志与诊断。
    """
    observed_set = {str(p) for p in observed}
    overrides = overrides or {}
    enabled: Dict[str, str] = {}
    always_on = _always_on_features(caps.product)

    for name, meta in _features_for(caps.product).items():
        probe_dvids = tuple(meta.get("probe_dvids") or ())
        probed = any(d in observed_set for d in probe_dvids)
        templated = name in caps.template_features
        # 覆盖键：新格式 "{product}:{feature}" 优先，旧裸名回退（已存选项兼容）
        override = overrides.get(f"{caps.product}:{name}")
        if override is None:
            override = overrides.get(name, OVERRIDE_AUTO)
        is_extra = bool(meta.get("extra"))

        if override == OVERRIDE_OFF:
            continue
        if override == OVERRIDE_ON:
            enabled[name] = "选项开启"
            continue

        allowed_models = meta.get("models")
        if allowed_models and caps.key not in allowed_models:
            # 状态位存在不等于支持该功能：例如 dvid95 在气泡水机型表示冷/热气，
            # 在另一批机型表示 AI 智能。只有实测确有该入口的机型才自动启用。
            continue

        if name in always_on:
            enabled[name] = "默认启用"
            continue

        if is_extra:
            # 补充功能没有模板证据，必须探测到点位才创建
            if probed:
                enabled[name] = f"探测到点位 {','.join(probe_dvids)}"
            continue

        if meta.get("probe_required") and probe_dvids:
            if probed:
                enabled[name] = f"模板+探测({','.join(probe_dvids)})"
            continue

        if templated or probed:
            enabled[name] = "模板声明" if templated else f"探测到点位 {','.join(probe_dvids)}"

    return enabled


def feature_choices(product: str = "") -> List[str]:
    """可选功能名列表；未指定品类时为跨品类并集（选项页用）。"""
    return sorted(_features_for_query(product))


def feature_requires_power(name: str, product: str) -> bool:
    """该功能是否要求设备处于开机状态。

    `product` 必填：门控判定必须在明确品类下回答，多品类并存时跨品类并集
    是「后注册者覆盖」语义，会静默给出错误答案。展示类查询（选项页）见
    `feature_choices` / `feature_platform`。
    """
    if not product:
        raise ValueError("feature_requires_power 需要明确的 product（门控判定不接受跨品类并集）")
    return bool((_features_for(product).get(name) or {}).get("requires_power"))


def feature_fault_exempt(name: str, product: str) -> bool:
    """设备故障时该功能是否仍可操作。

    仅开关机与累计用量清零不受故障限制。`product` 必填，理由同上。
    """
    if not product:
        raise ValueError("feature_fault_exempt 需要明确的 product（门控判定不接受跨品类并集）")
    return bool((_features_for(product).get(name) or {}).get("fault_exempt"))


def feature_platform(name: str, product: str = "") -> str:
    return str((_features_for_query(product).get(name) or {}).get("platform") or "")


def feature_owners() -> Dict[str, str]:
    """功能名到认领它的品类中文名的映射（未指定品类时的跨品类归属查询）。

    与 `_features_union()` 同为「后注册者覆盖」，保证两者对同名的判断一致。
    """
    _ensure_plugins()
    owners: Dict[str, str] = {}
    for plugin in registry.registered().values():
        for name in plugin.features:
            owners[name] = plugin.category_cn
    return owners


def feature_options() -> Dict[str, str]:
    """选项页「功能覆盖」多选项：`{"{product}:{feature}": "{品类}:{feature}"}`。

    必须按插件逐个生成：同名功能（如「开关机」）在多个品类下语义不同，
    若先按功能名去重，后注册的品类会覆盖前者，导致另一个品类的同名功能无法单独控制。
    """
    _ensure_plugins()
    options: Dict[str, str] = {}
    for product, plugin in registry.registered().items():
        for name in plugin.features:
            options[f"{product}:{name}"] = f"{plugin.category_cn}:{name}"
    return options


def feature_options_migrate(stored: Iterable[str]) -> List[str]:
    """把已存的旧裸功能名迁移为 `"{product}:{feature}"` 选项键。

    - 已带前缀的值原样保留（交由 `resolve_features` 的前缀优先逻辑处理）；
    - 裸名按「后注册者覆盖」映射到认领它的品类（与 `feature_owners()` 同源）；
      同名被多个品类认领时记 warning，提示用户在选项页重新确认；
    - 无法归属（功能已下架）或空值丢弃并记 warning，避免选项页出现非法 default。
    """
    candidates: Dict[str, List[str]] = {}
    for option_key in feature_options():
        name = option_key.split(":", 1)[1]
        candidates.setdefault(name, []).append(option_key)

    migrated: List[str] = []
    seen: Set[str] = set()
    for raw in stored or ():
        value = str(raw or "").strip()
        if not value:
            continue
        if ":" in value:
            target = value
        else:
            keys = candidates.get(value) or []
            if not keys:
                _LOGGER.warning("功能覆盖项 %s 未匹配任何已注册品类，迁移时已丢弃", value)
                continue
            target = keys[-1]
            if len(keys) > 1:
                _LOGGER.warning(
                    "功能 %s 同时属于多个品类（%s），旧选项迁移为 %s，请在选项页重新确认",
                    value, "、".join(keys), target,
                )
        if target in seen:
            continue
        seen.add(target)
        migrated.append(target)
    return migrated


def fault_info(model_key: Optional[str], value: Any, product: str = "") -> Dict[str, str]:
    """查机型专属的故障文案。

    故障文案与机型相关（同一编码在不同机型含义可能不同），按
    firm_product_model 存放于该品类的 faults_*.json。未收录的机型返回空字典，
    实体层退化为只显示故障码。
    """
    if not model_key or value is None:
        return {}
    entries = _faults_for_model(model_key, product)
    try:
        item = entries.get(str(int(value)))
    except (TypeError, ValueError):
        return {}
    if not isinstance(item, dict):
        return {}
    result: Dict[str, str] = {}
    if item.get("title"):
        result["title"] = str(item["title"])
    if item.get("guide"):
        result["guide"] = str(item["guide"])
    return result


def fault_coverage() -> List[str]:
    """已收录故障文案的机型列表（跨全部已注册品类，用于诊断日志）。"""
    _ensure_plugins()
    keys: Set[str] = set()
    for product in registry.all_products():
        keys.update(_faults_for(product))
    return sorted(keys)

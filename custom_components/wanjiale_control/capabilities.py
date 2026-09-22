"""机型能力解析：内置能力表 → 运行时探测 → 选项页覆盖。

三层的作用：
  1. 内置能力表描述机型模板能力，不等于实机能力。实测存在模板声明了零冷水点位
     但实机功能位未置起的情况，只信模板会多建实体。
  2. 运行时探测用设备真实上报过的 dvid 做确认。只信探测会漏判：
     功能关闭时状态位为 0，点位却仍然存在。
  3. 选项页覆盖给用户最终裁决权，处理两者都无法判定的边界机型。

对外只暴露两个函数：
  resolve(...)           设备记录 → DeviceCapabilities
  resolve_features(...)  能力 + 探测结果 + 用户覆盖 → 最终启用的功能名集合
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set

from .features_001 import BASELINE_POINTS, FEATURES

_LOGGER = logging.getLogger(__name__)

_TABLE_PATH = os.path.join(os.path.dirname(__file__), "capability_001.json")
_FAULT_PATH = os.path.join(os.path.dirname(__file__), "faults_001.json")

# 选项页三态
OVERRIDE_AUTO = "auto"
OVERRIDE_ON = "on"
OVERRIDE_OFF = "off"

# 与机型无关的核心点位：保证"设备从未上报"时主实体依然稳定存在
CORE_POINTS: frozenset = frozenset({"4", "10", "17", "18", "24", "28"})

# 无条件启用（主实体依赖，且全库 83%+ 机型支持）
ALWAYS_ON: frozenset = frozenset({"开关机"})


def _load_table() -> Dict[str, Any]:
    try:
        with open(_TABLE_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        _LOGGER.warning("内置能力表加载失败：%s", _TABLE_PATH, exc_info=True)
        return {}


def _load_faults() -> Dict[str, Any]:
    try:
        with open(_FAULT_PATH, encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        _LOGGER.warning("故障文案表加载失败：%s", _FAULT_PATH, exc_info=True)
        return {}


_TABLE: Dict[str, Any] = _load_table()
_FLAG_ORDER: List[str] = list(_TABLE.get("flagOrder") or [])
_DEVICES: Dict[str, Any] = dict(_TABLE.get("devices") or {})
_BY_MODEL_ID: Dict[str, str] = dict(_TABLE.get("byModelId") or {})
_FAULTS: Dict[str, Any] = dict(_load_faults().get("devices") or {})


@dataclass
class DeviceCapabilities:
    """单台设备的能力来源与模板能力。"""

    key: Optional[str] = None
    model_id: str = ""
    name: str = ""
    family: Optional[str] = None
    form: Optional[str] = None
    dvid_count: Optional[int] = None
    template_features: Set[str] = field(default_factory=set)
    template_modes: Dict[int, str] = field(default_factory=dict)
    source: str = "未收录"

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


def resolve(
    firm: str,
    product: str,
    model: str,
    model_id: str = "",
) -> DeviceCapabilities:
    """由设备记录解析能力。优先用 firm_product_model，回退用 modelId 反查。"""
    key = build_key(firm, product, model)
    entry = _DEVICES.get(key) if key else None
    if entry is None and model_id:
        key = _BY_MODEL_ID.get(model_id)
        entry = _DEVICES.get(key) if key else None
    if entry is None:
        return DeviceCapabilities(
            key=None, model_id=model_id, name="", source="未收录（仅用运行时探测）"
        )

    flags = str(entry.get("flags") or "")
    template = {
        name for idx, name in enumerate(_FLAG_ORDER)
        if idx < len(flags) and flags[idx] == "1"
    }
    modes = {
        int(item["id"]): item["name"]
        for item in (entry.get("modes") or [])
        if item.get("id") is not None and item.get("name")
    }
    caps = DeviceCapabilities(
        key=key,
        model_id=entry.get("modelId") or model_id,
        name=entry.get("name") or "",
        family=entry.get("family"),
        form=entry.get("form"),
        dvid_count=entry.get("dvidCount"),
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

    F01 家族用完整的 23 点基线；其它机型只保留跨机型通用的核心点位，
    避免为不存在的点位创建实体。
    """
    points = {str(p) for p in observed}
    points |= set(BASELINE_POINTS) if caps.family == "F01" else set(CORE_POINTS)
    return points


def resolve_features(
    caps: DeviceCapabilities,
    observed: Iterable[str],
    overrides: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """计算最终启用的功能集合。

    判定规则（依次）：
      1. 选项强制关闭 → 不启用；
      2. 选项强制开启 → 启用；
      3. probe_required 且该功能有可探测点位 → 必须探测到点位才启用
         （模板可能过度声明能力）；
      4. 其余 → 模板声明或探测命中即启用；
      5. extra 补充功能（能力表未收录）→ 必须探测到点位。

    Returns:
        {功能名: 依据说明}，说明用于日志与诊断。
    """
    observed_set = {str(p) for p in observed}
    overrides = overrides or {}
    enabled: Dict[str, str] = {}

    for name, meta in FEATURES.items():
        probe_dvids = tuple(meta.get("probe_dvids") or ())
        probed = any(d in observed_set for d in probe_dvids)
        templated = name in caps.template_features
        override = overrides.get(name, OVERRIDE_AUTO)
        is_extra = bool(meta.get("extra"))

        if override == OVERRIDE_OFF:
            continue
        if override == OVERRIDE_ON:
            enabled[name] = "选项开启"
            continue

        if name in ALWAYS_ON:
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


def feature_choices() -> List[str]:
    """选项页可选的功能名列表。"""
    return sorted(FEATURES)


def feature_requires_power(name: str) -> bool:
    """该功能是否要求设备处于开机状态。"""
    return bool((FEATURES.get(name) or {}).get("requires_power"))


def feature_fault_exempt(name: str) -> bool:
    """设备故障时该功能是否仍可操作。

    仅开关机与累计用量清零不受故障限制。
    """
    return bool((FEATURES.get(name) or {}).get("fault_exempt"))


def feature_platform(name: str) -> str:
    return str((FEATURES.get(name) or {}).get("platform") or "")


def fault_info(model_key: Optional[str], value: Any) -> Dict[str, str]:
    """查机型专属的故障文案。

    故障文案与机型相关（同一编码在不同机型含义可能不同），按
    firm_product_model 存放于 faults_001.json。未收录的机型返回空字典，
    实体层退化为只显示故障码。
    """
    if not model_key or value is None:
        return {}
    entries = _FAULTS.get(model_key) or {}
    item = entries.get(str(int(value)))
    if not isinstance(item, dict):
        return {}
    result: Dict[str, str] = {}
    if item.get("title"):
        result["title"] = str(item["title"])
    if item.get("guide"):
        result["guide"] = str(item["guide"])
    return result


def fault_coverage() -> List[str]:
    """已收录故障文案的机型列表（用于诊断日志）。"""
    return sorted(_FAULTS)

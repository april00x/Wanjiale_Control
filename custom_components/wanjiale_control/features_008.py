"""008 电热水器 · 品类语义层（与 `features_001.py` 对应）。

职责：
  - `FEATURES`：功能元数据（键必须覆盖 `capability_008.json` 的**全部** `flagOrder` 词）；
  - `parse_state(state, fids, profile)`：按 `profile["points"]`（机型级点位）解析统一语义字段；
  - `encode_value(action, **params)`：语义动作 → `{dpid: 绝对字符串}`（只做值计算，不含报文信封）。

**本文件不含任何全局 dpid 常量**：008 的开关/模式/温度/热水量/水温/故障/辅助开关点位
全部随机型变，一律从 `profile["points"]` 取（由 `capabilities.resolve()` 从
`capability_008.json` 的 `devices[*].points` 透传，见实施方案 C1/C2）。
跨机型推断是红线：同一 dpid 在不同机型语义互斥（报告 §8-C6）。

`probe_dvids` 一律留空：008 的功能位由能力表 `flags` 决定，不能用 dpid 并集探测。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

CATEGORY = "电热水器"
PRODUCT = "008"

# 与 `协议资源抓取工具/build_capability_008.py` 的 `_FEATURE_KEY` 保持一一对应：
# 键是 `set_<key>` 的方法名后缀（见 api.WanjialeElectricWaterHeater 的动态派发）。
AUX_KEY_FEATURE: Dict[str, str] = {
    "yuyue": "预约",
    "wudianxi": "无电洗",
    "yijun": "智能抑菌",
    "bianpin": "变频速热",
    "dongji": "冬季",
    "zengrong": "智能增容",
    "fenggu": "峰谷夜电",
    "guanjia": "智能管家",
    "fangdong": "防冻",
    "baoyang": "保养提醒",
    "zengrong2": "增容",
    "jingfuyu": "净肤浴",
    "yangfuyu": "养肤浴",
    "yuanqiyu": "元气浴",
    "pubuxi": "瀑布洗",
    "fuyangxi": "富氧洗",
    "hengwenyu": "恒温浴",
}

# 功能元数据：键集必须与 capability_008.json 的 flagOrder 完全一致，
# 否则 resolve_features() 认不出的功能不会启用（switch 实体点了会报「该机型不支持」）。
# probe_dvids 全部留空（见模块 docstring）。
FEATURES: Dict[str, Dict[str, Any]] = {
    "开关机":   {"platform": "water_heater", "requires_power": False, "fault_exempt": True},
    "模式切换": {"platform": "water_heater", "requires_power": True},
    "预约":     {"platform": "switch", "requires_power": False},
    "无电洗":   {"platform": "switch", "requires_power": True},
    "智能抑菌": {"platform": "switch", "requires_power": True},
    "变频速热": {"platform": "switch", "requires_power": True},
    "冬季":     {"platform": "switch", "requires_power": True},
    "智能增容": {"platform": "switch", "requires_power": True},
    "峰谷夜电": {"platform": "switch", "requires_power": True},
    "智能管家": {"platform": "switch", "requires_power": True},
    "防冻":     {"platform": "switch", "requires_power": True},
    "保养提醒": {"platform": "switch", "requires_power": True},
    "增容":     {"platform": "switch", "requires_power": True},
    "净肤浴":   {"platform": "switch", "requires_power": True},
    "元气浴":   {"platform": "switch", "requires_power": True},
    "养肤浴":   {"platform": "switch", "requires_power": True},
    "瀑布洗":   {"platform": "switch", "requires_power": True},
    "富氧洗":   {"platform": "switch", "requires_power": True},
    "恒温浴":   {"platform": "switch", "requires_power": True},
}


def _int(value: Any) -> Optional[int]:
    """宽松取整：设备上报可能是字符串；取不到返回 None（不要伪造 0）。"""
    if value is None or value == "":
        return None
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return None


def _points(profile: Any) -> Dict[str, Any]:
    if isinstance(profile, dict):
        pts = profile.get("points")
        if isinstance(pts, dict):
            return pts
    return {}


def _fault_title(model_key: Optional[str], fault_id: int) -> Optional[str]:
    """按机型主键查故障文案标题（查不到返回 None）。

    `features_008` 被 `categories/008/plugin.py` 导入，而后者由 `capabilities`
    在运行期加载，模块级导入 capabilities 会成环，故此处**函数内**惰性导入。
    """
    if not model_key:
        return None
    try:
        from .capabilities import fault_info

        info = fault_info(model_key, fault_id, PRODUCT)
        title = info.get("title") if isinstance(info, dict) else None
        return str(title) if title else None
    except Exception:  # noqa: BLE001
        return None


# ======================================================================
# 状态解析
# ======================================================================
def parse_state(
    state: Dict[str, Any],
    fids: Any = None,
    profile: Any = None,
) -> Dict[str, Any]:
    """按机型点位把 `as` 字典解析成统一语义字段。

    只输出「有来源」的键（使实体能区分「未知」与「值为 0」）。输出键：
        power_on / mode / target_temp / current_temp / hot_water
        / status / status_text / heating / keeping_warm / reserve_time
        / fault / fault_text / fault_list / <aux_key>_on
    """
    if not isinstance(state, dict):
        state = {}
    pts = _points(profile)
    out: Dict[str, Any] = {}

    # ---- 开关（1=开） ----
    power = pts.get("power") or {}
    dpid = str(power.get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["power_on"] = value == _int(power.get("on")) if power.get("on") is not None else value == 1

    # ---- 模式 ----
    mode_pt = pts.get("mode") or {}
    dpid = str(mode_pt.get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["mode"] = value

    # ---- 设定温度（008 的写入值即绝对值 ℃，见实施方案 §4.3） ----
    temp_pt = pts.get("temp") or {}
    dpid = str(temp_pt.get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["target_temp"] = value

    # ---- 运行状态（机型级 `points.status`；仅真机实测确认的机型有点） ----
    # `status` 保留原始值，`status_text` 取机型 map；加热 / 保温两档由标量比对给出，
    # 取值映射随机型变（报告 §8-C6：OKS/ACB 为 1/2，GRO 为 2/3），故不在本层写死。
    status_pt = pts.get("status") or {}
    dpid = str(status_pt.get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["status"] = value
            label = (status_pt.get("map") or {}).get(str(value))
            if label:
                out["status_text"] = str(label)
            heat_value = _int(status_pt.get("heating"))
            if heat_value is not None:
                out["heating"] = value == heat_value
            warm_value = _int(status_pt.get("keepingWarm"))
            if warm_value is not None:
                out["keeping_warm"] = value == warm_value

    # ---- 预约时间（机型级 `points.reserveTime`；值 = 自 0:00 起的分钟数，如 15:30=930） ----
    dpid = str((pts.get("reserveTime") or {}).get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["reserve_time"] = value

    # ---- 当前水温 / 当前热水量 ----
    dpid = str((pts.get("curTemp") or {}).get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["current_temp"] = value
    dpid = str((pts.get("hotWater") or {}).get("dpid") or "")
    if dpid and dpid in state:
        value = _int(state[dpid])
        if value is not None:
            out["hot_water"] = value

    # ---- 辅助开关状态（switch spec 的 state_key = "<key>_on"） ----
    for item in pts.get("aux") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "")
        dpid = str(item.get("dpid") or "")
        if not key or not dpid or dpid not in state:
            continue
        value = _int(state[dpid])
        if value is None:
            continue
        on_value = _int(item.get("on"))
        out[f"{key}_on"] = value == (1 if on_value is None else on_value)

    # ---- 故障：云端 fids 优先（H5 无设备故障点，见实施方案 §2.5-D） ----
    ids: List[int] = []
    for raw in (fids or []):
        value = _int(raw)
        if value is not None:
            ids.append(value)
    if ids:
        out["fault"] = ids[0]
        out["fault_text"] = _fault_title(
            (profile or {}).get("key") if isinstance(profile, dict) else None, ids[0]
        ) or f"E{ids[0]}"
        out["fault_list"] = ids
    # fids 为空 → 不输出 fault（保持「无来源即未知」的契约；见 §5.2 映射表）

    return out


# ======================================================================
# 值编码（语义动作 → {dpid: 绝对字符串}）
# ======================================================================
def _require_point(points: Dict[str, Any], name: str) -> Dict[str, Any]:
    item = points.get(name)
    if not isinstance(item, dict) or not item.get("dpid"):
        raise ValueError(f"该机型缺少点位：{name}")
    return item


def encode_value(action: str, **params: Any) -> Dict[str, str]:
    """把语义动作编码成 `{dpid: 绝对字符串}`（不含报文信封）。

    008 一律**单点直写绝对值**，无成对复合包；点位取自 `params["points"]`
    （机型级 `devices[*].points`），故本层不出现任何全局 dpid。

    支持动作：
        power       on=True/False                    → 开关点（含 onAlso 多发键）
        temperature value=30~75                      → 温度点（按机型 min/max 钳位）
        mode        mode=<模式号>                     → 模式点
        toggle      dpid=<点位> value=<0/1>           → 通用单点直写（辅助开关等）
    """
    if action == "toggle":
        dpid = str(params["dpid"])
        if not dpid:
            raise ValueError("toggle 需要 dpid")
        return {dpid: str(int(params["value"]))}

    points = params.get("points") or {}

    if action == "power":
        item = _require_point(points, "power")
        on = bool(params["on"])
        value = item.get("on", 1) if on else item.get("off", 0)
        frame = {str(item["dpid"]): str(int(value))}
        if on:
            # 部分机型开机需多发一帧（报告 §11.2：OKS 开机 = 101=1 且 102=1）
            also = item.get("onAlso") or {}
            if isinstance(also, dict):
                frame.update({str(k): str(int(v)) for k, v in also.items()})
        return frame

    if action == "temperature":
        item = _require_point(points, "temp")
        low = int(item.get("min", 30))
        high = int(item.get("max", 75))
        value = max(low, min(high, int(params["value"])))
        return {str(item["dpid"]): str(value)}

    if action == "mode":
        item = _require_point(points, "mode")
        return {str(item["dpid"]): str(int(params["mode"]))}

    raise ValueError(f"未支持的语义动作：{action!r}")

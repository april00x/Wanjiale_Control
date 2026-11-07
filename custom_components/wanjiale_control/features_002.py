"""002 燃气壁挂炉 · 品类语义层（与 `features_001.py` / `features_008.py` 对应）。

职责：
  - `FEATURES`：功能元数据（键必须覆盖 `capability_002.json` 的全部 `flagOrder` 词）；
  - `parse_state(state, fids, profile)`：按 `profile["points"]`（机型级点位）解析统一语义字段；
  - `encode_value(action, **params)`：语义动作 → `{dpid: 绝对字符串}`。

本文件不含任何全局 dpid 常量：002 的同一 dpid 在不同方言/机型下语义互斥
（如 108 在 B 标准是卫浴设定温度、在 B3 是故障码），点位一律从 `profile["points"]` 取。
实现只允许按 `points.dialect + points.*` 分支，禁止「按协议版本统一」的跨机型推断。

`probe_dvids` 一律留空：002 的功能位由能力表 `flags` 决定，不能用 dpid 并集探测。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

CATEGORY = "燃气壁挂炉"
PRODUCT = "002"

# 设备上报的预约列表不是合法 JSON：键名不带引号、字符串用单引号
# （形如 [{'s':360,'e':540}]），先做一次字面量归一化再解析。
_JS_BARE_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_]\w*)(\s*:)")

# 功能元数据：键集必须与 capability_002.json 的 flagOrder 完全一致，
# 否则 resolve_features() 认不出的功能不会启用（控制实体点了会报「该机型不支持」）。
FEATURES: Dict[str, Dict[str, Any]] = {
    "开关机":     {"platform": "water_heater", "requires_power": False, "fault_exempt": True},
    "卫浴温度":   {"platform": "water_heater", "requires_power": True},
    "供暖温度":   {"platform": "climate",      "requires_power": True},
    "供暖开关":   {"platform": "climate",      "requires_power": True},
    "供暖模式":   {"platform": "select",       "requires_power": True},
    "预约供暖":   {"platform": "switch",       "requires_power": False},
    "热水预热":   {"platform": "switch",       "requires_power": True},
    "即热":       {"platform": "switch",       "requires_power": True},
    "保温时长":   {"platform": "select",       "requires_power": True},
    "回差温度":   {"platform": "number",       "requires_power": True},
    "增压":       {"platform": "switch",       "requires_power": True},
    "AI":         {"platform": "switch",       "requires_power": True},
    "杀菌":       {"platform": "switch",       "requires_power": True},
    "晨浴夜浴":   {"platform": "select",       "requires_power": True},
    "复位":       {"platform": "button",       "requires_power": False, "fault_exempt": True},
    "操作权":     {"platform": "internal",     "requires_power": False},
    "预约冲突清除": {"platform": "button",     "requires_power": False, "fault_exempt": True},
    "童锁":       {"platform": "switch",       "requires_power": True},
}


# ======================================================================
# 基础工具
# ======================================================================
def _int(value: Any) -> Optional[int]:
    """宽松取整：设备可能把 as 值下发成字符串；不可解析返回 None，绝不抛。"""
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


def _group(points: Dict[str, Any], name: str) -> Dict[str, Any]:
    item = points.get(name)
    return item if isinstance(item, dict) else {}


def _dpid_of(spec: Dict[str, Any]) -> str:
    """点位组的读 dpid：优先 `dpid`，其次 `read`（int 或 `{dpid, bit/mask}`）。"""
    dpid = spec.get("dpid")
    if dpid not in (None, ""):
        return str(dpid)
    read = spec.get("read")
    if isinstance(read, (int, str)):
        return str(read)
    if isinstance(read, dict) and read.get("dpid") not in (None, ""):
        return str(read.get("dpid"))
    return ""


def _value(state: Dict[str, Any], spec: Dict[str, Any]) -> Optional[int]:
    """按点位组取读数；点位未上报返回 None（不伪造 0）。"""
    dpid = _dpid_of(spec)
    if not dpid or dpid not in state:
        return None
    return _int(state.get(dpid))


def _mask(spec: Dict[str, Any], key: str) -> Optional[int]:
    """取位掩码：优先点位组显式键，其次 `read` 字典里的 bit/mask。"""
    value = _int(spec.get(key))
    if value is not None:
        return value
    read = spec.get("read")
    if isinstance(read, dict):
        for name in ("bit", "mask"):
            value = _int(read.get(name))
            if value is not None:
                return value
    return None


def _normalize_fault(raw: Any, normal: Any) -> Tuple[Optional[int], Optional[int]]:
    """把各方言的故障值归一化：正常 → 0，异常 → 非 0。

    A 线的 0 与 B3 的 0/255 可能同时是「正常哨兵」和「真实故障码」，统一挪到
    256+ 的保留段，避免与 `plugin.fault_none=(0,)` 冲突。
    """
    value = _int(raw)
    base = _int(normal)
    if value is None or base is None:
        return None, None
    if value == base:
        return 0, value
    canonical = 256 + value if value in (0, 255) else value
    return canonical, value


def _fault_title(model_key: Optional[str], fault_id: int) -> Optional[str]:
    """按机型主键查故障文案标题（查不到返回 None）。

    本模块由 `categories/002/plugin.py` 导入，而后者由 `capabilities` 在运行期加载，
    模块级导入 capabilities 会成环，故在函数内惰性导入。
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


def _parse_windows(value: Any) -> Optional[List[Dict[str, int]]]:
    """解析 A 线预约列表（dpid 6/270 的 JSON 数组字符串）为 [{"s":分,"e":分}]。"""
    data = value
    if isinstance(data, str):
        text = data.strip()
        candidates = (
            text,
            _JS_BARE_KEY_RE.sub(r'\1"\2"\3', text).replace("'", '"'),
            text.replace("'", '"'),
        )
        data = None
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                break
            except (ValueError, TypeError):
                continue
        if data is None:
            return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    windows: List[Dict[str, int]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start = _int(item.get("s"))
        end = _int(item.get("e"))
        if start is None or end is None:
            continue
        windows.append({"s": start, "e": end})
    return windows


def _emit_schedule_groups(
    out: Dict[str, Any],
    state: Dict[str, Any],
    prefix: str,
    sched: Dict[str, Any],
) -> None:
    """输出预约组回读值：B/C 数组点位直读，A 线从 list 的 JSON 拆组。"""
    arrays = False
    for field in ("start", "end", "enable", "temp"):
        values = sched.get(field)
        if not isinstance(values, (list, tuple)):
            continue
        arrays = True
        for index, dpid in enumerate(values, start=1):
            if dpid in (None, ""):
                continue
            raw = _int(state.get(str(dpid)))
            if raw is None:
                continue
            out[f"{prefix}_{index}_{field}"] = (raw == 1) if field == "enable" else raw

    if arrays:
        return
    list_dpid = _int(sched.get("list"))
    if list_dpid is None:
        return
    windows = _parse_windows(state.get(str(list_dpid)))
    if windows is None:
        return
    groups = _int(sched.get("groups")) or len(windows)
    for index, window in enumerate(windows[:groups], start=1):
        out[f"{prefix}_{index}_start"] = window["s"]
        out[f"{prefix}_{index}_end"] = window["e"]


def _emit_wash_groups(
    out: Dict[str, Any],
    state: Dict[str, Any],
    sched: Dict[str, Any],
) -> None:
    """B3 洗浴预约：每组一个 packed 值，按字节拆成起止分钟。

    `s = 起时 + 起分*256`、`e = 止时 + 止分*256`，`v = s | (e<<16)`；
    `v` 缺失或为 0 视为空组（不能显示 00:00）。
    """
    for index, dpid in enumerate(sched.get("packed") or [], start=1):
        if dpid in (None, ""):
            continue
        value = _int(state.get(str(dpid)))
        if value is None or value == 0:
            continue
        start = value & 0xFFFF
        end = (value >> 16) & 0xFFFF
        out[f"wash_schedule_{index}_start"] = (start & 0xFF) * 60 + (start >> 8)
        out[f"wash_schedule_{index}_end"] = (end & 0xFF) * 60 + (end >> 8)


def _emit_thermostats(
    out: Dict[str, Any],
    state: Dict[str, Any],
    thermostats: Any,
) -> None:
    """B4 八路温控器：当前温度 / 设定温度 / 开关 / 故障四类只读值。"""
    for index, item in enumerate(thermostats or [], start=1):
        if not isinstance(item, dict):
            continue
        current = _int(state.get(str(item.get("current"))))
        if current is not None:
            out[f"thermostat_{index}_current"] = round(
                (current >> 8) - 20 + (current & 0xFF) / 100, 2
            )
        setting = _int(state.get(str(item.get("set"))))
        if setting is not None:
            out[f"thermostat_{index}_set"] = setting
        enabled = _int(state.get(str(item.get("enable"))))
        if enabled is not None:
            out[f"thermostat_{index}_enable"] = enabled == 1
        fault = _int(state.get(str(item.get("fault"))))
        if fault is not None:
            out[f"thermostat_{index}_fault"] = "正常" if fault == 0 else f"E{fault}"


def _weekly_plan(state: Dict[str, Any]) -> List[Dict[str, int]]:
    """B5 周计划：动态 dpid = 109 + week*11 + slot*2（起）/ +1（止）。

    只读摘要，不做写入；协议未给出的周/时段一律跳过。
    """
    segments: List[Dict[str, int]] = []
    for week in range(7):
        base = 109 + week * 11
        for slot in range(5):
            start = _int(state.get(str(base + slot * 2)))
            end = _int(state.get(str(base + slot * 2 + 1)))
            if start is None or end is None:
                continue
            segments.append({"week": week + 1, "slot": slot + 1, "start": start, "end": end})
    return segments


# ======================================================================
# 状态解析
# ======================================================================
def parse_state(
    state: Dict[str, Any],
    fids: Any = None,
    profile: Any = None,
) -> Dict[str, Any]:
    """按机型点位把 `as` 字典解析成统一语义字段。

    只输出「有来源」的键（使实体能区分「未知」与「值为 0」），解析失败返回 None，
    不抛异常、不伪造 0。
    """
    if not isinstance(state, dict):
        state = {}
    pts = _points(profile)
    key = (profile or {}).get("key") if isinstance(profile, dict) else None
    out: Dict[str, Any] = {}

    # ---- 电源 ----
    power = _group(pts, "power")
    value = _value(state, power)
    if value is not None:
        on_value = _int(power.get("on"))
        out["power_on"] = value == (1 if on_value is None else on_value)

    # ---- 温度（设定 / 当前） ----
    for name, target_key, current_key in (
        ("dhw_set", "dhw_target_temp", None),
        ("dhw_current", None, "dhw_current_temp"),
    ):
        value = _value(state, _group(pts, name))
        if value is not None:
            if target_key:
                out[target_key] = value
            if current_key:
                out[current_key] = value

    value = _value(state, _group(pts, "heat_set"))
    if value is not None:
        out["heat_target_temp"] = value
    value = _value(state, _group(pts, "heat_current"))
    if value is not None:
        out["heat_current_temp"] = value

    # ---- A 线 cmd4 复合包所需：卫浴模式 / 水流 / 水量 ----
    if pts.get("dialect") == "A":
        dhw_set = _group(pts, "dhw_set")
        for out_key, key in (
            ("dhw_mode", "mode_dpid"),
            ("target_flow", "flow_dpid"),
            ("target_volume", "volume_dpid"),
        ):
            dpid = _int(dhw_set.get(key))
            if dpid is None:
                continue
            raw = _int(state.get(str(dpid)))
            if raw is not None:
                out[out_key] = raw

    # ---- 供暖开关 / 运行 ----
    heating = _group(pts, "heating")
    season = _group(pts, "season")
    if heating:
        raw = _value(state, heating)
        if raw is not None:
            bit = _mask(heating, "on_bit")
            on_value = _int(heating.get("on"))
            if bit is not None:
                out["heating_on"] = bool(raw & bit)
            elif on_value is not None:
                out["heating_on"] = raw == on_value
            else:
                out["heating_on"] = raw != 0
    elif season.get("dpid") not in (None, ""):
        # B 标准 / 原生 C / B2c：冬夏模式（winter=0 视为供暖开）
        raw = _value(state, season)
        if raw is not None:
            winter = _int(season.get("winter"))
            out["heating_on"] = raw == (0 if winter is None else winter)

    # ---- 燃烧中（B 标准 103&32） ----
    combustion = _group(pts, "combustion")
    combustion_bit = _mask(combustion, "bit")
    raw = _value(state, combustion)
    if combustion_bit is not None and raw is not None:
        out["combustion"] = bool(raw & combustion_bit)

    # ---- B3 供暖位域细分（只读，不改 heating_on 的整值判定） ----
    if heating:
        heating_raw = _value(state, heating)
        if heating_raw is not None:
            for out_key, key in (
                ("frost_on", "frost_bit"),
                ("heat_reserve_flag_on", "reserve_bit"),
                ("big_loop_on", "big_loop_bit"),
                ("heat_pump_on", "pump_bit"),
            ):
                bit = _mask(heating, key)
                if bit is not None:
                    out[out_key] = bool(heating_raw & bit)

    # ---- 点动：A 线 68&1（必须按位判，68 可能带其它位）/ B3 117==1 ----
    jog = _group(pts, "jog")
    raw = _value(state, jog)
    if raw is not None:
        bit = _mask(jog, "bit")
        out["jog_on"] = bool(raw & bit) if bit is not None else raw == 1

    # ---- 浴缸注水完成（24&8） ----
    water_supply = _group(pts, "water_supply")
    supply_bit = _mask(water_supply, "bit")
    raw = _value(state, water_supply)
    if supply_bit is not None and raw is not None:
        out["bath_fill_done"] = bool(raw & supply_bit)

    # ---- 环境修正（(52 & 0x70) >> 4） ----
    env_offset = _group(pts, "env_offset")
    raw = _value(state, env_offset)
    if raw is not None:
        mask = _int(env_offset.get("mask"))
        shift = _int(env_offset.get("shift")) or 0
        out["env_offset"] = (raw & mask) >> shift if mask is not None else raw

    # ---- ECO 读侧（仅 A 线有 eco_bit；B3 的 119&64 不输出） ----
    eco_bit = _mask(heating, "eco_bit")
    if eco_bit is not None:
        eco_raw = _value(state, heating)
        if eco_raw is not None:
            out["eco_on"] = bool(eco_raw & eco_bit)

    # ---- 即热时长原始档位（A=78 / B3=118） ----
    circulation = _group(pts, "circulation")
    raw = _value(state, circulation)
    if raw is not None:
        out["circulation"] = raw

    heating_state = _group(pts, "heating_state")
    status = _group(pts, "status")
    heating_bit = _mask(status, "heating_bit")
    if heating_state:
        raw = _value(state, heating_state)
        if raw is not None:
            on_value = _int(heating_state.get("on"))
            out["heating_running"] = raw == (1 if on_value is None else on_value)
    elif heating_bit is not None:
        raw = _value(state, status)
        if raw is not None:
            out["heating_running"] = bool(raw & heating_bit)
    elif pts.get("dialect") == "B3" and "heating_on" in out:
        # B3 的 119 既是供暖功能开，也是运行状态（协议无独立运行位）
        out["heating_running"] = out["heating_on"]

    # ---- 位域状态（火焰 / 水流 / 水泵） ----
    flame_bit = _mask(status, "flame_bit")
    if flame_bit is not None:
        raw = _value(state, status)
        if raw is not None:
            out["flame"] = bool(raw & flame_bit)

    flow_bit = _mask(status, "flow_bit")
    if flow_bit is not None:
        raw = _value(state, status)
        if raw is not None:
            out["water_flowing"] = bool(raw & flow_bit)

    pump = _group(pts, "pump")
    pump_bit = _mask(pump, "pump_bit")
    if pump_bit is not None:
        raw = _value(state, pump)
        if raw is not None:
            out["pump_running"] = bool(raw & pump_bit)

    # ---- 末端 / 冬夏 ----
    emitter_pt = _group(pts, "emitter")
    value = _value(state, emitter_pt)
    if value is not None:
        out["emitter"] = value
    elif pts.get("dialect") == "A":
        # A 线没有独立末端点：87 已上报时实读 bit5；未上报时维持能力表常量
        # （启动期量程行为不变）。
        heating_pt = _group(pts, "heating")
        range_bit = _int(heating_pt.get("range_bit"))
        range_shift = _int(heating_pt.get("range_shift"))
        raw = _value(state, heating_pt)
        if raw is not None and range_bit is not None:
            out["emitter"] = (raw & range_bit) >> (5 if range_shift is None else range_shift)
        else:
            assumed = _int(heating_pt.get("range_radiator_value"))
            if assumed is not None:
                out["emitter"] = assumed
    if season.get("dpid") not in (None, ""):
        raw = _value(state, season)
        if raw is not None:
            out["season"] = raw

    # ---- 模式 ----
    mode = _group(pts, "mode")
    value = _value(state, mode)
    if value is not None:
        out["mode"] = value
        label = (mode.get("values") or {}).get(str(value))
        if label:
            out["mode_text"] = str(label)

    # ---- 增压 / AI / 晨浴夜浴 ----
    for out_key, name in (("boost_on", "boost"), ("ai_on", "ai")):
        spec = _group(pts, name)
        raw = _value(state, spec)
        if raw is None:
            continue
        mask = _mask(spec, "mask")
        out[out_key] = bool(raw & mask) if mask is not None else raw == 1

    morning_night = _group(pts, "morning_night")
    read = morning_night.get("read") if isinstance(morning_night.get("read"), dict) else {}
    dpid = _int(read.get("mode"))
    if dpid is not None:
        raw = _int(state.get(str(dpid)))
        if raw is not None:
            out["morning_night_mode"] = raw

    # ---- 预约总开关 ----
    heat_schedule = _group(pts, "schedule_heat")
    total = _int(heat_schedule.get("total"))
    schedule_bit = _mask(heating, "schedule_bit")
    if total is not None:
        raw = _int(state.get(str(total)))
        if raw is not None:
            out["heat_schedule_on"] = raw == 1
    elif schedule_bit is not None:
        raw = _value(state, heating)
        if raw is not None:
            out["heat_schedule_on"] = bool(raw & schedule_bit)

    dhw_schedule = _group(pts, "schedule_dhw")
    total = _int(dhw_schedule.get("total"))
    if total is not None:
        raw = _int(state.get(str(total)))
        if raw is not None:
            out["preheat_on"] = raw == 1
    elif pts.get("dialect") == "A":
        # A 线洗浴预约总开关用 60&16（预约/夜浴位）
        instant = _group(pts, "instant")
        dpid = _dpid_of(instant)
        if dpid and dpid in state:
            raw = _int(state.get(dpid))
            if raw is not None:
                out["preheat_on"] = bool(raw & 16)

    # ---- 预约组回读（B/C 数组直读；A 线从 list 的 JSON 拆组） ----
    _emit_schedule_groups(out, state, "heat_schedule", heat_schedule)
    _emit_schedule_groups(out, state, "dhw_schedule", dhw_schedule)
    shared = _int(heat_schedule.get("shared_temp"))
    if shared is not None:
        raw = _int(state.get(str(shared)))
        if raw is not None:
            out["heat_schedule_shared_temp"] = raw

    # ---- B3 洗浴预约 / B4 八路温控器 ----
    _emit_wash_groups(out, state, _group(pts, "schedule_wash"))
    _emit_thermostats(out, state, pts.get("thermostats"))

    # ---- 洗浴预约总开关（XQZ 116） ----
    wash_master = _group(pts, "schedule_wash").get("master")
    if isinstance(wash_master, dict):
        dpid = _int(wash_master.get("dpid"))
        if dpid is not None:
            raw = _int(state.get(str(dpid)))
            if raw is not None:
                out["wash_schedule_on"] = raw == 1

    # ---- 即热 ----
    instant = _group(pts, "instant")
    if instant:
        raw = _value(state, instant)
        if raw is not None:
            bit = _mask(instant, "bit")
            out["instant_heat_on"] = bool(raw & bit) if bit is not None else raw == 1
    else:
        instant_mode = _group(pts, "instant_mode")
        raw = _value(state, instant_mode)
        if raw is not None:
            out["instant_heat_on"] = raw == 1

    # ---- 保温（分钟） ----
    keep_warm = _group(pts, "keep_warm")
    raw = _value(state, keep_warm)
    if raw is not None:
        mapping = keep_warm.get("map")
        if isinstance(mapping, dict):
            minutes = _int(mapping.get(str(raw)))
            if minutes is not None:
                out["keep_warm"] = minutes
        else:
            unit = _int(keep_warm.get("unit_min")) or 1
            out["keep_warm"] = raw * unit
    keep_warm_readonly = _group(pts, "keep_warm_readonly")
    raw = _value(state, keep_warm_readonly)
    if raw is not None:
        out["keep_warm_readonly"] = raw

    # ---- 回差 ----
    for name in ("diff_temp", "diff_dhw", "diff_heat"):
        spec = _group(pts, name)
        raw = _value(state, spec)
        if raw is None:
            continue
        offset = _int(spec.get("write_offset")) or 0
        out[name] = raw - offset

    # ---- 预约窗口 ----
    in_window = _group(pts, "in_window")
    raw = _value(state, in_window)
    if raw is not None:
        target = _int(in_window.get("value"))
        out["in_window"] = raw == (1 if target is None else target)

    # ---- 用气量 ----
    gas_total = _group(pts, "gas_total")
    if gas_total:
        scale = float(gas_total.get("scale") or 1)
        byte_keys = gas_total.get("bytes")
        if isinstance(byte_keys, (list, tuple)) and byte_keys:
            values = [_int(state.get(str(d))) for d in byte_keys]
            if all(v is not None for v in values):
                total = 0
                for v in values:
                    total = total * 256 + int(v)  # type: ignore[arg-type]
                out["gas_total"] = round(total / scale, 3)
        else:
            raw = _value(state, gas_total)
            if raw is not None:
                out["gas_total"] = round(raw / scale, 3)

    gas_current = _group(pts, "gas_current")
    if gas_current:
        scale = float(gas_current.get("scale") or 1)
        high = _int(gas_current.get("high"))
        low = _int(gas_current.get("low"))
        if high is not None and low is not None:
            high_value = _int(state.get(str(high)))
            low_value = _int(state.get(str(low)))
            if high_value is not None and low_value is not None:
                out["gas_current"] = round((high_value * 256 + low_value) / scale, 3)
        else:
            raw = _value(state, gas_current)
            if raw is not None:
                out["gas_current"] = round(raw / scale, 3)

    # ---- 水压 ----
    water_pressure = _group(pts, "water_pressure")
    raw = _value(state, water_pressure)
    if raw is not None:
        mask = _int(water_pressure.get("mask"))
        if mask is not None:
            raw = raw & mask
        out["water_pressure"] = round(raw * float(water_pressure.get("scale") or 1), 2)

    # ---- 下次预约时间 ----
    for out_key, name in (("next_heat_reserve", "schedule_heat"), ("next_dhw_preheat", "schedule_dhw")):
        dpid = _int(_group(pts, name).get("next"))
        if dpid is None:
            continue
        raw = _int(state.get(str(dpid)))
        if raw is not None:
            out[out_key] = raw

    # ---- B5（FFS）----
    raw = _value(state, _group(pts, "dhw_actual"))
    if raw is not None:
        out["dhw_actual"] = round(raw / 10 - 20, 1)
    raw = _value(state, _group(pts, "child_lock"))
    if raw is not None:
        out["child_lock"] = raw == 1
    if pts.get("dialect") == "B5":
        # 周计划只读摘要：dpid 是动态计算的，不在机型点位表里
        weekly = _weekly_plan(state)
        out["weekly_plan_count"] = len(weekly)
        if weekly:
            out["weekly_plan"] = weekly

    # ---- 杀菌 / 抑菌 ----
    sterilize = _group(pts, "sterilize")
    raw = _value(state, sterilize)
    if raw is not None:
        bit = _mask(sterilize, "bit")
        out["sterilizing"] = bool(raw & bit) if bit is not None else raw == 1

    # ---- 在线 ----
    raw = _value(state, _group(pts, "online"))
    if raw is not None:
        out["online"] = raw != 0

    # ---- 多端操作权优先位（仅 A 线有点位） ----
    raw = _value(state, _group(pts, "priority"))
    if raw is not None:
        out["priority"] = raw

    # ---- 故障（归一化：正常=0，异常≠0） ----
    fault = _group(pts, "fault")
    dpid = _dpid_of(fault)
    if dpid and dpid in state:
        canonical, raw_value = _normalize_fault(state.get(dpid), fault.get("normal"))
        if canonical is not None:
            out["fault"] = canonical
            out["fault_raw"] = raw_value
            if canonical != 0:
                out["fault_text"] = _fault_title(key, canonical) or f"E{canonical}"

    return out


# ======================================================================
# 值编码（语义动作 → {dpid: 绝对字符串}）
# ======================================================================
def _require_group(points: Dict[str, Any], name: str) -> Dict[str, Any]:
    item = points.get(name)
    if not isinstance(item, dict) or not item:
        raise ValueError(f"该机型缺少点位组：{name}")
    return item


def _dpid(item: Dict[str, Any], key: str = "dpid") -> str:
    value = item.get(key)
    if value in (None, ""):
        raise ValueError(f"该机型缺少点位：{key}")
    return str(value)


def _master_dpid(item: Dict[str, Any]) -> str:
    """预约总开关 dpid：B/C 是 `total`，A 线是 `master_cmd`。"""
    total = item.get("total")
    if total not in (None, ""):
        return str(total)
    if item.get("master_cmd"):
        raise ValueError("该机型未配置预约总开关编码")
    raise ValueError("该机型未配置预约总开关点位")


def _dialect(points: Dict[str, Any]) -> str:
    return str(points.get("dialect") or "")


def _byte(value: Any) -> int:
    """复合包里的单字节参数：None 表示「不修改」（0xFF）。"""
    if value is None:
        return 0xFF
    return max(0, min(255, int(value)))


def _pack_toggle(byte3: Any, subtype: int = 2) -> int:
    """A 线复合包：byte3 为开关值/档位，byte2 为子类型，低 16 位 0xFFFF 表示不修改。"""
    return (int(byte3) << 24) | (int(subtype) << 16) | 0xFFFF


def _cmd_pair(cmd: Any, value: Any) -> Dict[str, str]:
    """A 线命令字成对报文：dvid1=命令类型，dvid2=32 位复合参数。"""
    if not isinstance(cmd, dict) or cmd.get("type") in (None, ""):
        raise ValueError("该机型缺少命令字点位")
    return {"1": str(int(cmd["type"])), "2": str(int(value))}


def _a_master(cmd: Dict[str, Any], on: bool, temp: Any) -> Dict[str, str]:
    """A 线预约总开关：部分机型是「on/off + 温度」形态。"""
    if cmd.get("with_temp"):
        value = ((1 if on else 0) << 24) | (2 << 16) | (_byte(temp) << 8) | 0xFF
        return _cmd_pair(cmd, value)
    return _cmd_pair(cmd, cmd.get("on") if on else cmd.get("off"))


def encode_value(action: str, **params: Any) -> Dict[str, str]:
    """把语义动作编码成 `{dpid: 绝对字符串}`（不含报文信封）。

    B/C/B2c/B4/B5 一律单点直写；A 线是命令字加 32 位复合包（dvid1/dvid2 成对）。
    两种形态都只按 `points` 决定，不做跨机型推断。
    """
    points = params.get("points") or {}
    if not isinstance(points, dict) or not points:
        raise ValueError("缺少机型点位表，拒绝下发")
    dialect = _dialect(points)

    if action == "power":
        item = _require_group(points, "power")
        on = bool(params["on"])
        value = item.get("on", 1) if on else item.get("off", 0)
        return {_dpid(item): str(int(value))}

    if action == "dhw_temp":
        item = _require_group(points, "dhw_set")
        low = int(item.get("min", 35))
        high = int(item.get("max", 60))
        value = max(low, min(high, int(params["value"])))
        if dialect == "A":
            # {1: cmd4, 2: mode<<24 | temp<<16 | flow<<8 | volume}
            packed = (
                (_byte(params.get("mode")) << 24)
                | (value << 16)
                | (_byte(params.get("flow")) << 8)
                | _byte(params.get("volume"))
            )
            return _cmd_pair(item.get("cmd"), packed)
        return {_dpid(item): str(value)}

    if action == "heat_temp":
        item = _require_group(points, "heat_set")
        lows = [item[k] for k in ("min_floor", "min_radiator") if item.get(k) is not None]
        highs = [item[k] for k in ("max_floor", "max_radiator") if item.get(k) is not None]
        value = int(params["value"])
        if lows:
            value = max(min(int(v) for v in lows), value)
        if highs:
            value = min(max(int(v) for v in highs), value)
        if dialect == "A":
            # {1: cmd39, 2: temp<<24 | 0x02FFFF}
            return _cmd_pair(item.get("cmd"), _pack_toggle(value))
        return {_dpid(item): str(value)}

    if action == "heating":
        on = bool(params["on"])
        if dialect == "A":
            cmd = (points.get("heating") or {}).get("cmd") or (
                points.get("season") or {}
            ).get("cmd") or {}
            return _cmd_pair(cmd, cmd.get("on") if on else cmd.get("off"))
        item = points.get("heating")
        if isinstance(item, dict) and item:
            value = item.get("on", 1) if on else item.get("off", 0)
            return {_dpid(item): str(int(value))}
        item = _require_group(points, "season")
        value = item.get("winter", 0) if on else item.get("summer", 1)
        return {_dpid(item): str(int(value))}

    if action == "mode":
        item = _require_group(points, "mode")
        mode = int(params["mode"])
        values = item.get("values") or {}
        if values and str(mode) not in values:
            raise ValueError(f"该机型不支持模式取值 {mode}")
        if dialect == "A":
            cmd = item.get("cmd") or {}
            # YJW 的 cmd45 按模式取值覆盖 subtype；其余机型统一用 cmd.subtype
            subtype = (cmd.get("subtype_by_value") or {}).get(str(mode), cmd.get("subtype", 2))
            return _cmd_pair(cmd, _pack_toggle(mode, int(subtype)))
        return {_dpid(item): str(mode)}

    if action == "instant":
        item = points.get("instant_mode")
        if not isinstance(item, dict) or not item:
            item = _require_group(points, "instant")
        on = bool(params["on"])
        if dialect == "A":
            cmd = item.get("cmd") or {}
            if cmd.get("with_temp"):
                # on/off<<24 | 2<<16 | 温度<<8 | 255
                packed = ((1 if on else 0) << 24) | (2 << 16) | (_byte(params.get("temp")) << 8) | 0xFF
                return _cmd_pair(cmd, packed)
            return _cmd_pair(cmd, cmd.get("on") if on else cmd.get("off"))
        return {_dpid(item): "1" if on else "0"}

    if action == "emitter":
        value = int(params["value"])
        if dialect == "A":
            cmd = _require_group(points, "heating").get("emitter_cmd") or {}
            if cmd.get("type") in (None, ""):
                raise ValueError("该机型未配置采暖末端命令")
            values = cmd.get("values") or {}
            if values and str(value) not in values:
                raise ValueError(f"该机型不支持末端类型 {value}")
            if cmd.get("with_temp"):
                heat_temp = params.get("heat_temp")
                heat_diff = params.get("heat_diff")
                if heat_temp is None or heat_diff is None:
                    # 不得落入 _byte() 的 None→0xFF 兜底（255℃ 非法值）
                    raise ValueError("缺少当前供暖温度或回差，已取消下发末端切换")
                packed = (
                    ((value & 0xFF) << 24)
                    | (2 << 16)
                    | (_byte(heat_temp) << 8)
                    | _byte(heat_diff)
                )
            else:
                # YJW：只发末端，低 16 位 0xFFFF 表示不修改
                packed = ((value & 0xFF) << 24) | (2 << 16) | 0xFFFF
            return _cmd_pair(cmd, packed)
        item = _require_group(points, "emitter")
        if item.get("writable") is not True:
            raise ValueError("该机型末端类型只读，拒绝下发")
        radiator = _int(item.get("radiator"))
        floor = _int(item.get("floor"))
        if value not in (radiator, floor):
            raise ValueError(f"该机型不支持末端类型 {value}")
        # B3 的 option key 直接用协议值（1=散热片 / 0=地暖），不做显示值反查
        return {_dpid(item): str(value)}

    if action == "eco":
        if dialect != "A":
            raise ValueError("该机型不支持 ECO 写入")
        cmd = _require_group(points, "heating").get("eco_cmd") or {}
        if cmd.get("type") in (None, ""):
            raise ValueError("该机型未配置节能命令")
        return _cmd_pair(cmd, cmd.get("on") if params["on"] else cmd.get("off"))

    if action == "away":
        item = _require_group(points, "away_cmd")
        return _cmd_pair(item, item.get("on") if params["on"] else item.get("off"))

    if action == "circulation":
        item = _require_group(points, "circulation")
        value = int(params["value"])
        options = item.get("options") or {}
        if options and str(value) not in options:
            raise ValueError(f"该机型不支持即热时长档位 {value}")
        if dialect == "A":
            # {1: cmd26, 2: 档位<<24 | 2<<16 | 0xFFFF}
            return _cmd_pair(item.get("cmd"), ((value & 0xFF) << 24) | (2 << 16) | 0xFFFF)
        dpid = item.get("dpid")
        if dpid in (None, ""):
            raise ValueError("该机型缺少即热时长点位")
        return {str(dpid): str(value)}

    if action == "wash_master":
        master = _require_group(points, "schedule_wash").get("master") or {}
        dpid = master.get("dpid")
        if dpid in (None, ""):
            raise ValueError("该机型未配置洗浴预约总开关")
        return {str(dpid): "1" if params["on"] else "0"}

    if action == "jog":
        item = points.get("jog")
        if not isinstance(item, dict) or item.get("writable") is not True:
            # A 线 68 只有 readonly：同键不同方言，必须由属性拦截
            raise ValueError("该机型点动状态只读或未配置，拒绝下发")
        return {_dpid(item): "1" if params["on"] else "0"}

    if action == "heat_schedule_master":
        item = _require_group(points, "schedule_heat")
        if dialect == "A":
            return _a_master(item.get("master_cmd") or {}, bool(params["on"]), params.get("temp"))
        return {_master_dpid(item): "1" if params["on"] else "0"}

    if action == "preheat_master":
        item = _require_group(points, "schedule_dhw")
        if dialect == "A":
            return _a_master(item.get("master_cmd") or {}, bool(params["on"]), params.get("temp"))
        return {_master_dpid(item): "1" if params["on"] else "0"}

    if action in ("boost", "ai", "sterilize", "child_lock"):
        item = _require_group(points, action)
        on = bool(params["on"])
        if dialect == "A" and item.get("cmd"):
            return _cmd_pair(item.get("cmd"), _pack_toggle(1 if on else 0))
        return {_dpid(item): "1" if on else "0"}

    if action == "keep_warm":
        item = _require_group(points, "keep_warm")
        minutes = int(params["minutes"])
        if dialect == "A":
            unit = int(item.get("unit_min") or 1)
            return _cmd_pair(item.get("cmd"), _pack_toggle(minutes // unit))
        if item.get("write_rule") == "minutes_to_raw_then_scale":
            mapping = item.get("minutes_to_raw") or {}
            raw = mapping.get(str(minutes))
            if raw is None:
                raise ValueError(f"该机型不支持保温时长 {minutes} 分钟")
            raw = int(raw) * int(item.get("write_scale") or 1)
        else:
            unit = int(item.get("unit_min") or 1)
            raw = minutes // unit
        return {_dpid(item): str(raw)}

    if action == "diff":
        kind = str(params.get("kind") or "dhw")
        value = int(params["value"])
        neutral = points.get("diff_temp")
        if isinstance(neutral, dict) and neutral:
            return {_dpid(neutral): str(value)}
        item = _require_group(points, "diff_heat" if kind == "heat" else "diff_dhw")
        if dialect == "A":
            return _cmd_pair(item.get("cmd"), _pack_toggle(value))
        # 偏移只从能力表取，固定用 write_offset
        offset = int(item.get("write_offset") or 0)
        return {_dpid(item): str(value + offset)}

    if action in ("schedule_time", "schedule_enable", "schedule_temp"):
        kind = str(params["kind"])
        group = int(params["group"])
        index = group - 1
        sched = _require_group(points, f"schedule_{kind}")
        if action == "schedule_time":
            field = str(params["field"])
            minutes = int(params["minutes"])
            if dialect == "A":
                # A 线一条命令写「同组起止」：start_min<<16 | end_min
                add = sched.get("add")
                other = params.get("other_minutes")
                if add in (None, ""):
                    raise ValueError("该机型未配置预约新增命令")
                if other is None:
                    raise ValueError("缺少同组另一端时间，拒绝只写半个预约包")
                start = minutes if field == "start" else int(other)
                end = int(other) if field == "start" else minutes
                return {str(add): str((start << 16) | end)}
            if sched.get("packed"):
                # B3：每组一个 packed 值（起时/起分/止时/止分），也要整组写入
                packed_list = sched.get("packed") or []
                other = params.get("other_minutes")
                if index >= len(packed_list):
                    raise ValueError(f"该机型没有第 {group} 组洗浴预约")
                if other is None:
                    raise ValueError("缺少同组另一端时间，拒绝只写半个预约包")
                start = minutes if field == "start" else int(other)
                end = int(other) if field == "start" else minutes
                start_h, start_m = divmod(start, 60)
                end_h, end_m = divmod(end, 60)
                value = ((start_h + start_m * 256) & 0xFFFF) | ((end_h + end_m * 256) << 16)
                return {str(packed_list[index]): str(value)}
            values = sched.get(field)
            if not isinstance(values, (list, tuple)) or index >= len(values) or values[index] in (None, ""):
                raise ValueError(f"该机型未配置预约组 {kind}[{group}].{field}")
            return {str(values[index]): str(minutes)}
        if action == "schedule_enable":
            values = sched.get("enable")
            if not isinstance(values, (list, tuple)) or index >= len(values) or values[index] in (None, ""):
                raise ValueError(f"该机型未配置预约组 {kind}[{group}].enable")
            return {str(values[index]): "1" if params["on"] else "0"}
        values = sched.get("temp")
        if isinstance(values, (list, tuple)) and index < len(values) and values[index] not in (None, ""):
            dpid = str(values[index])
        elif sched.get("shared_temp") not in (None, ""):
            # B2c：所有组共用同一个预约温度点位（会同时改供暖设定）
            dpid = str(sched["shared_temp"])
        else:
            raise ValueError(f"该机型未配置预约组 {kind}[{group}] 温度点位")
        return {dpid: str(int(params["value"]))}

    if action == "schedule_delete":
        kind = str(params["kind"])
        sched = _require_group(points, f"schedule_{kind}")
        packed = params.get("packed_value")
        if dialect == "A":
            delete_dpid = sched.get("del")
            if delete_dpid in (None, "") or packed is None:
                raise ValueError("该机型未配置预约删除命令或缺少整组时间")
            return {str(delete_dpid): str(int(packed))}
        delete_dpid = sched.get("delete")
        if delete_dpid not in (None, "") and packed is not None:
            return {str(delete_dpid): str(int(packed))}
        raise ValueError("该机型不支持预约删除")

    if action == "thermostat":
        thermostats = points.get("thermostats") or []
        n = int(params["n"])
        if not 1 <= n <= len(thermostats):
            raise ValueError(f"该机型没有第 {n} 路温控器")
        item = thermostats[n - 1]
        if not isinstance(item, dict) or item.get("enable") in (None, ""):
            raise ValueError(f"第 {n} 路温控器缺少开关点位")
        return {str(item["enable"]): "1" if params["on"] else "0"}

    if action == "morning_night":
        item = _require_group(points, "morning_night")
        cmd = item.get("cmd_switch") or {}
        mode = int(params["mode"])
        # bit0=晨洗、bit1=夜浴（0~3）
        return _cmd_pair(cmd, ((mode & 0x03) << 24) | 0x02FFFF)

    if action == "morning_night_time":
        # 晨浴夜浴时间：晨起/30<<24 | 晨止/30<<16 | 夜起/30<<8 | 夜止/30
        item = _require_group(points, "morning_night")
        cmd = item.get("cmd_time") or {}
        quarters = []
        for name in ("morning_start", "morning_end", "night_start", "night_end"):
            minutes = params.get(name)
            if minutes is None:
                raise ValueError("晨浴夜浴时间参数不完整")
            quarters.append((int(minutes) // 30) & 0xFF)
        packed = (
            (quarters[0] << 24) | (quarters[1] << 16)
            | (quarters[2] << 8) | quarters[3]
        )
        return _cmd_pair(cmd, packed)

    if action == "reset":
        item = _require_group(points, "reset")
        cmd = item.get("cmd") or {}
        return _cmd_pair(cmd, cmd.get("value", 131071))

    if action == "conflict":
        item = _require_group(points, "conflict")
        dpid = item.get("write", item.get("dpid"))
        if dpid in (None, ""):
            raise ValueError("该机型未配置预约冲突清除点位")
        return {str(dpid): "0"}

    if action == "control_right":
        item = _require_group(points, "control_right")
        request_type = item.get("request_type", 24)
        return {"1": str(int(request_type)), "2": str(_pack_toggle(1))}

    raise ValueError(f"未支持的语义动作：{action!r}")

"""002 燃气壁挂炉 · 各平台 spec 表（按设备动态生成）。

002 的实体名单本身随机型/方言变（同一 dpid 在不同机型语义互斥），静态表装不下，
因此 `PLATFORM_SPECS` 的值全部是 `Callable[[device], Tuple[Spec, ...]]`
（由核心 `registry._resolve_specs()` 解析）。

约定：
  - 点位一律取 `device.caps.model_points`，本文件不做跨机型推断；
  - 组实体（预约开关/温度/时间）是设备级：`device_level=True` + `method_args`，
    `unique_id` 不带角色后缀；
  - 主开关/组实体按 `points` 存在性生成，不只看功能位；
  - `sensor/binary_sensor` 由平台按 `dev.has_point(spec.dvid)` 再过滤一次。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

from ...specs import (
    BinarySensorSpec,
    ButtonSpec,
    NumberSpec,
    ScheduleTimeSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
)


def _points(device: Any) -> Dict[str, Any]:
    caps = getattr(device, "caps", None)
    points = getattr(caps, "model_points", None)
    return points if isinstance(points, dict) else {}


def _group(points: Dict[str, Any], name: str) -> Dict[str, Any]:
    item = points.get(name)
    return item if isinstance(item, dict) else {}


def _dpid_of(item: Dict[str, Any]) -> str:
    """点位组的读 dpid：`dpid` 优先，其次 `read`（int 或 `{dpid}`）。"""
    if not isinstance(item, dict):
        return ""
    dpid = item.get("dpid")
    if dpid not in (None, ""):
        return str(dpid)
    read = item.get("read")
    if isinstance(read, (int, str)):
        return str(read)
    if isinstance(read, dict) and read.get("dpid") not in (None, ""):
        return str(read.get("dpid"))
    return ""


def _dialect(points: Dict[str, Any]) -> str:
    return str(points.get("dialect") or "")


def _heat_bounds(points: Dict[str, Any]) -> Tuple[float, float]:
    spec = _group(points, "heat_set")
    lows = [spec[k] for k in ("min_floor", "min_radiator", "min") if spec.get(k) is not None]
    highs = [spec[k] for k in ("max_floor", "max_radiator", "max") if spec.get(k) is not None]
    return (
        float(min(lows)) if lows else 25.0,
        float(max(highs)) if highs else 80.0,
    )


# ======================================================================
# sensor
# ======================================================================
def sensor_specs(device: Any) -> Tuple[SensorSpec, ...]:
    pts = _points(device)
    dialect = _dialect(pts)
    specs = []

    dpid = _dpid_of(_group(pts, "dhw_current"))
    if dpid:
        specs.append(SensorSpec(
            "current_temp", "卫浴当前水温", dpid, "dhw_current_temp",
            "mdi:thermometer-water", "°C", "temperature", 1,
        ))
    dpid = _dpid_of(_group(pts, "heat_current"))
    if dpid:
        specs.append(SensorSpec(
            "heating_current_temp", "供暖当前水温", dpid, "heat_current_temp",
            "mdi:radiator", "°C", "temperature", 1,
        ))

    dpid = _dpid_of(_group(pts, "dhw_actual"))
    if dpid:
        specs.append(SensorSpec(
            "actual_temp", "实际温度", dpid, "dhw_actual",
            "mdi:thermometer", "°C", "temperature", 1,
        ))

    gas_total = _group(pts, "gas_total")
    if gas_total:
        dpid = gas_total.get("dpid")
        if dpid in (None, ""):
            byte_keys = gas_total.get("bytes") or []
            dpid = byte_keys[0] if byte_keys else None
        if dpid not in (None, ""):
            specs.append(SensorSpec(
                "gas_total", "累计用气量", str(dpid), "gas_total",
                "mdi:fire", "m³", "gas", 3,
            ))

    gas_current = _group(pts, "gas_current")
    if gas_current:
        dpid = gas_current.get("dpid") or gas_current.get("high")
        if dpid not in (None, ""):
            specs.append(SensorSpec(
                "gas_current", "本次用气量", str(dpid), "gas_current",
                "mdi:fire", "m³", "gas", 3,
            ))

    dpid = _dpid_of(_group(pts, "water_pressure"))
    if dpid:
        specs.append(SensorSpec(
            "water_pressure", "水压", dpid, "water_pressure",
            "mdi:gauge", "bar", "pressure", 2,
        ))

    # 环境修正（(52 & 0x70) >> 4）：仅展示，不做目标温度换算
    env_offset = _group(pts, "env_offset")
    dpid = _dpid_of(env_offset)
    if dpid:
        specs.append(SensorSpec(
            "env_offset", "环境修正", dpid, "env_offset",
            "mdi:thermometer-auto", None, None, None, True,
        ))

    heat_next = _group(pts, "schedule_heat").get("next")
    if heat_next not in (None, ""):
        specs.append(SensorSpec(
            "next_heat_reserve", "下次供暖预约时间", str(heat_next), "next_heat_reserve",
            "mdi:calendar-clock", None, None, None, False, "hhmm",
        ))

    dhw_next = _group(pts, "schedule_dhw").get("next")
    if dhw_next not in (None, ""):
        name = "下次洗浴预约时间" if dialect == "A" else "下次热水预热时间"
        specs.append(SensorSpec(
            "next_dhw_preheat", name, str(dhw_next), "next_dhw_preheat",
            "mdi:calendar-clock", None, None, None, False, "hhmm",
        ))

    dpid = _dpid_of(_group(pts, "keep_warm_readonly"))
    if dpid:
        specs.append(SensorSpec(
            "keep_warm_level", "保温档位", dpid, "keep_warm_readonly",
            "mdi:timer-outline", None, None, None, True,
        ))

    for name, label in (("diff_dhw", "卫浴回差温度"), ("diff_heat", "供暖回差温度")):
        item = _group(pts, name)
        dpid = _dpid_of(item)
        if dpid and item.get("readonly"):
            specs.append(SensorSpec(
                name, label, dpid, name, "mdi:thermometer-lines", "°C", "temperature", 1,
            ))

    # B4 八路温控器：当前温度 / 设定温度（只读）/ 故障
    for index, item in enumerate(pts.get("thermostats") or [], start=1):
        if not isinstance(item, dict):
            continue
        for field, label, icon, unit in (
            ("current", "当前温度", "mdi:thermometer", "°C"),
            ("set", "设定温度", "mdi:thermometer-check", "°C"),
            ("fault", "故障", "mdi:alert-circle-outline", None),
        ):
            dpid = item.get(field)
            if dpid in (None, ""):
                continue
            specs.append(SensorSpec(
                f"thermostat_{index}_{field}", f"温控器{index}{label}", str(dpid),
                f"thermostat_{index}_{field}", icon, unit,
                "temperature" if unit else None, 1 if unit else None,
                diagnostic=field == "fault",
            ))

    if dialect == "B5":
        # 周计划 dpid 是动态计算的，不在机型点位表里；用当前温度点触发实体创建
        trigger = _dpid_of(_group(pts, "dhw_current")) or "102"
        specs.append(SensorSpec(
            "weekly_plan", "周计划", trigger, "weekly_plan_count",
            "mdi:calendar-week", None, None, None, True,
        ))

    return tuple(specs)


# ======================================================================
# binary_sensor
# ======================================================================
def binary_sensor_specs(device: Any) -> Tuple[BinarySensorSpec, ...]:
    pts = _points(device)
    dialect = _dialect(pts)
    status = _group(pts, "status")
    heating = _group(pts, "heating")
    heating_state = _group(pts, "heating_state")
    specs = []

    if heating_state:
        dpid = _dpid_of(heating_state)
        if dpid:
            # B5（FFS）的 105 是「加热状态」，不是供暖运行
            name = "加热中" if dialect == "B5" else "供暖运行"
            specs.append(BinarySensorSpec(
                "heating_running", name, dpid, "heating_running",
                "mdi:radiator", "running",
            ))
    elif status.get("heating_bit") is not None:
        dpid = status.get("dpid")
        if dpid not in (None, ""):
            specs.append(BinarySensorSpec(
                "heating_running", "供暖运行", str(dpid), "heating_running",
                "mdi:radiator", "running",
            ))
    elif _dialect(pts) == "B3":
        dpid = _dpid_of(heating)
        if dpid:
            specs.append(BinarySensorSpec(
                "heating_running", "供暖运行", dpid, "heating_running",
                "mdi:radiator", "running",
            ))

    if status.get("flow_bit") is not None and status.get("dpid") not in (None, ""):
        specs.append(BinarySensorSpec(
            "water_flowing", "有水流", str(status["dpid"]), "water_flowing",
            "mdi:water", "running",
        ))

    pump = _group(pts, "pump")
    if pump.get("pump_bit") is not None and pump.get("dpid") not in (None, ""):
        specs.append(BinarySensorSpec(
            "pump_running", "水泵运行", str(pump["dpid"]), "pump_running",
            "mdi:pump", "running",
        ))

    in_window = _group(pts, "in_window")
    if in_window.get("dpid") not in (None, ""):
        specs.append(BinarySensorSpec(
            "in_window", "预约时间段内", str(in_window["dpid"]), "in_window",
            "mdi:calendar-check",
        ))

    # 燃烧中（103&32）：仅 B 标准方言有点位
    combustion = _group(pts, "combustion")
    dpid = _dpid_of(combustion)
    if dpid:
        specs.append(BinarySensorSpec(
            "combustion", "燃烧中", dpid, "combustion", "mdi:fire", "running",
        ))

    # B3 供暖位域细分（只读）
    for bit_key, key, name, icon, value_key in (
        ("frost_bit", "frost", "防冻", "mdi:snowflake-alert", "frost_on"),
        ("reserve_bit", "heat_reserve_flag", "供暖预约标志", "mdi:calendar-check-outline",
         "heat_reserve_flag_on"),
        ("big_loop_bit", "big_loop", "大循环", "mdi:sync", "big_loop_on"),
        ("pump_bit", "heat_pump", "供暖水泵", "mdi:pump", "heat_pump_on"),
    ):
        if heating.get(bit_key) is None:
            continue
        dpid = _dpid_of(heating)
        if not dpid:
            continue
        specs.append(BinarySensorSpec(key, name, dpid, value_key, icon))

    # 点动状态：仅 A 线只读 jog(68)；B3 的 jog(117, writable) 归 switch
    jog = _group(pts, "jog")
    dpid = _dpid_of(jog)
    if dpid and not jog.get("writable"):
        specs.append(BinarySensorSpec("jog_state", "点动", dpid, "jog_on", "mdi:gesture-tap"))

    # 浴缸注水完成（24&8）
    water_supply = _group(pts, "water_supply")
    dpid = _dpid_of(water_supply)
    if dpid:
        specs.append(BinarySensorSpec(
            "bath_fill_done", "浴缸注水完成", dpid, "bath_fill_done", "mdi:bathtub-outline",
        ))

    return tuple(specs)


# ======================================================================
# switch
# ======================================================================
def switch_specs(device: Any) -> Tuple[SwitchSpec, ...]:
    pts = _points(device)
    dialect = _dialect(pts)
    specs = []

    instant = _group(pts, "instant_mode") or _group(pts, "instant")
    if instant:
        specs.append(SwitchSpec(
            "即热", "instant", "即热", "mdi:fire", "set_instant", "instant_heat_on",
        ))

    dhw_schedule = _group(pts, "schedule_dhw")
    if dhw_schedule.get("total") is not None or dhw_schedule.get("master_cmd"):
        if dialect == "A":
            name = "洗浴预约"
        elif dialect == "B4":
            name = "热水定时总开关"
        else:
            name = "热水预热总开关"
        specs.append(SwitchSpec(
            "热水预热", "preheat_master", name, "mdi:calendar-clock",
            "set_preheat_schedule", "preheat_on",
        ))

    heat_schedule = _group(pts, "schedule_heat")
    if heat_schedule.get("total") is not None or heat_schedule.get("master_cmd"):
        specs.append(SwitchSpec(
            "预约供暖", "heat_schedule_master", "预约供暖总开关", "mdi:calendar-check",
            "set_heat_schedule", "heat_schedule_on",
        ))

    features = (
        ("增压", "boost", "增压", "mdi:rocket-launch", "set_boost", "boost_on"),
        ("AI", "ai", "AI", "mdi:robot-outline", "set_ai", "ai_on"),
        ("杀菌", "sterilize", "抑菌" if dialect == "B4" else "杀菌",
         "mdi:shield-sun-outline", "set_sterilize", "sterilizing"),
        ("童锁", "child_lock", "童锁", "mdi:lock-outline", "set_child_lock", "child_lock"),
    )
    for feature, key, name, icon, method, state_key in features:
        if _group(pts, key):
            specs.append(SwitchSpec(feature, key, name, icon, method, state_key))

    for index, dpid in enumerate(heat_schedule.get("enable") or [], start=1):
        if dpid in (None, ""):
            continue
        specs.append(SwitchSpec(
            "预约供暖", f"schedule_heat_{index}_enable", f"供暖预约{index}", "mdi:calendar",
            "set_schedule_enable", f"heat_schedule_{index}_enable",
            device_level=True, method_args=("heat", index),
        ))

    dhw_label = "热水定时" if dialect == "B4" else ("洗浴预约" if dialect == "A" else "热水预热")
    for index, dpid in enumerate(dhw_schedule.get("enable") or [], start=1):
        if dpid in (None, ""):
            continue
        specs.append(SwitchSpec(
            "热水预热", f"schedule_dhw_{index}_enable", f"{dhw_label}{index}", "mdi:calendar",
            "set_schedule_enable", f"dhw_schedule_{index}_enable",
            device_level=True, method_args=("dhw", index),
        ))

    # B4 八路温控器开关（设备级；设定温度只读、配对/编号管理不做）
    for index, item in enumerate(pts.get("thermostats") or [], start=1):
        if not isinstance(item, dict) or item.get("enable") in (None, ""):
            continue
        specs.append(SwitchSpec(
            None, f"thermostat_{index}", f"温控器{index}", "mdi:thermostat",
            "set_thermostat", f"thermostat_{index}_enable",
            device_level=True, method_args=(index,),
        ))

    # 节能/睡眠（A 线 cmd38）
    eco_cmd = _group(pts, "heating").get("eco_cmd") or {}
    if eco_cmd:
        specs.append(SwitchSpec(
            None, "eco", str(eco_cmd.get("label") or "ECO/节能"), "mdi:leaf",
            "set_eco", "eco_on", True, True,
        ))

    # 外出（FFY/WLK cmd44）：无读点，状态用 _local_state
    if _group(pts, "away_cmd"):
        specs.append(SwitchSpec(
            None, "away", "外出", "mdi:home-export-outline", "set_away", None, True, True,
        ))

    # 洗浴预约总开关（XQZ 116==1）
    master = _group(pts, "schedule_wash").get("master") or {}
    if master.get("dpid") not in (None, ""):
        specs.append(SwitchSpec(
            None, "wash_master", "洗浴预约总开关", "mdi:calendar-check",
            "set_wash_master", "wash_schedule_on", False, True,
        ))

    # 点动（仅 B3 117 可写；A 线 jog(68) 没有 writable 键，不会建 switch）
    jog = _group(pts, "jog")
    if jog.get("writable") is True:
        specs.append(SwitchSpec(
            None, "jog", "点动", "mdi:gesture-tap", "set_jog", "jog_on",
            False, True, ("jog.writable",),
        ))

    return tuple(specs)


# ======================================================================
# number
# ======================================================================
def number_specs(device: Any) -> Tuple[NumberSpec, ...]:
    pts = _points(device)
    specs = []

    neutral = _group(pts, "diff_temp")
    if neutral:
        specs.append(NumberSpec(
            "回差温度", "diff_temp", "回差温度", "mdi:thermometer-lines",
            "set_diff_temp", "diff_temp",
            float(neutral.get("min", 5)), float(neutral.get("max", 20)), 1, "°C",
        ))

    for key, label, method in (
        ("diff_dhw", "卫浴回差温度", "set_diff_temp"),
        ("diff_heat", "供暖回差温度", "set_heat_diff"),
    ):
        item = _group(pts, key)
        if not item or item.get("readonly"):
            continue
        specs.append(NumberSpec(
            "回差温度", key, label, "mdi:thermometer-lines", method, key,
            float(item.get("min", 1)), float(item.get("max", 10)), 1, "°C",
        ))

    low, high = _heat_bounds(pts)
    heat_schedule = _group(pts, "schedule_heat")
    for index, dpid in enumerate(heat_schedule.get("temp") or [], start=1):
        if dpid in (None, ""):
            continue
        specs.append(NumberSpec(
            "预约供暖", f"schedule_heat_{index}_temp", f"供暖预约{index}温度",
            "mdi:thermometer", "set_schedule_temp", f"heat_schedule_{index}_temp",
            low, high, 1, "°C", device_level=True, method_args=("heat", index),
        ))

    # B2c：所有组共用一个预约温度点位（109，会同时改供暖设定）
    if not heat_schedule.get("temp") and heat_schedule.get("shared_temp") not in (None, ""):
        specs.append(NumberSpec(
            "预约供暖", "schedule_heat_shared_temp", "预约温度", "mdi:thermometer",
            "set_schedule_temp", "heat_schedule_shared_temp",
            low, high, 1, "°C",
            device_level=True, require_points=("schedule_heat.shared_temp",),
            method_args=("heat", 1),
        ))

    return tuple(specs)


# ======================================================================
# select
# ======================================================================
def select_specs(device: Any) -> Tuple[SelectSpec, ...]:
    pts = _points(device)
    specs = []

    mode = _group(pts, "mode")
    if mode.get("device_mode"):
        specs.append(SelectSpec(
            None, "mode", "模式", "mdi:home-thermometer", "set_mode", "mode",
            options_from="device_mode_names",
        ))
    elif mode:
        specs.append(SelectSpec(
            "供暖模式", "heating_mode", "供暖模式", "mdi:home-thermometer",
            "set_heating_mode", "mode", options_from="heating_mode_names",
        ))

    if _group(pts, "keep_warm"):
        specs.append(SelectSpec(
            "保温时长", "keep_warm", "保温时长", "mdi:timer-outline",
            "set_keep_warm", "keep_warm", options_from="keep_warm_options",
        ))

    if _group(pts, "morning_night"):
        specs.append(SelectSpec(
            "晨浴夜浴", "morning_night", "晨浴夜浴", "mdi:weather-sunset",
            "set_morning_night", "morning_night_mode",
            options={0: "关闭", 1: "晨浴", 2: "夜浴", 3: "晨浴+夜浴"},
        ))

    # 末端类型（A 线 cmd43）：option key = 协议值
    emitter_cmd = _group(pts, "heating").get("emitter_cmd") or {}
    if emitter_cmd:
        specs.append(SelectSpec(
            None, "emitter", "末端类型", "mdi:radiator", "set_emitter", "emitter",
            {int(k): str(v) for k, v in (emitter_cmd.get("values") or {}).items()},
            device_level=True,
        ))

    # 即热时长：A 线 cmd26 / B3 直写 118，档位取自 App 即热时长弹窗
    circulation = _group(pts, "circulation")
    if circulation.get("cmd") or circulation.get("dpid") not in (None, ""):
        specs.append(SelectSpec(
            None, "circulation", "即热时长", "mdi:timer-sync-outline",
            "set_circulation", "circulation",
            {int(k): str(v) for k, v in (circulation.get("options") or {}).items()},
            device_level=True,
        ))

    # 末端类型（B3 直写 120）：option key 直接用协议值，不做显示值反查
    emitter = _group(pts, "emitter")
    if emitter.get("writable") is True:
        specs.append(SelectSpec(
            None, "emitter", "末端类型", "mdi:radiator", "set_emitter", "emitter",
            {int(emitter.get("radiator", 1)): "散热片", int(emitter.get("floor", 0)): "地暖"},
            device_level=True,
        ))

    return tuple(specs)


# ======================================================================
# button（A 线一次性动作）
# ======================================================================
def button_specs(device: Any) -> Tuple[ButtonSpec, ...]:
    pts = _points(device)
    specs = []
    if _group(pts, "reset"):
        specs.append(ButtonSpec(
            "reset", "复位", "mdi:restart", "reset_device", require_points=("reset",),
        ))
    if _group(pts, "conflict"):
        specs.append(ButtonSpec(
            "clear_conflict", "清除预约冲突", "mdi:calendar-remove",
            "clear_reserve_conflict", require_points=("conflict",),
        ))
    delete_dpid = _group(pts, "schedule_wash").get("delete")
    if delete_dpid not in (None, ""):
        specs.append(ButtonSpec(
            "delete_wash_schedule", "删除洗浴预约", "mdi:calendar-remove",
            "delete_wash_schedule", require_point=str(delete_dpid), device_level=True,
        ))
    return tuple(specs)


# ======================================================================
# time（预约组起止）
# ======================================================================
def _group_time_specs(
    kind: str, feature: Optional[str], schedule: Dict[str, Any], label: str
) -> Tuple[ScheduleTimeSpec, ...]:
    specs = []
    packed = schedule.get("packed")
    if isinstance(packed, (list, tuple)) and packed:
        # B3 洗浴预约：每组一个 packed 值，起止 time 各一条（无组开关/组温度）
        for field in ("start", "end"):
            for index, dpid in enumerate(packed, start=1):
                if dpid in (None, ""):
                    continue
                specs.append(ScheduleTimeSpec(
                    kind, index, field, str(dpid), feature,
                    f"{kind}_schedule_{index}_{field}", label,
                ))
        return tuple(specs)
    for field in ("start", "end"):
        values = schedule.get(field)
        if isinstance(values, (list, tuple)) and values:
            for index, dpid in enumerate(values, start=1):
                specs.append(ScheduleTimeSpec(
                    kind, index, field, str(dpid), feature,
                    f"{kind}_schedule_{index}_{field}", label,
                ))
            continue
        # A 线：只有 list（JSON 回读）+ groups，没有分组 dpid
        list_dpid = schedule.get("list")
        groups = int(schedule.get("groups") or 0)
        if list_dpid not in (None, "") and groups:
            for index in range(1, groups + 1):
                specs.append(ScheduleTimeSpec(
                    kind, index, field, "", feature,
                    f"{kind}_schedule_{index}_{field}", label,
                ))
    return tuple(specs)


def time_specs(device: Any) -> Tuple[ScheduleTimeSpec, ...]:
    pts = _points(device)
    dialect = _dialect(pts)
    specs = []

    heat_schedule = _group(pts, "schedule_heat")
    if heat_schedule:
        specs.extend(_group_time_specs("heat", "预约供暖", heat_schedule, ""))

    dhw_schedule = _group(pts, "schedule_dhw")
    if dhw_schedule:
        label = "热水定时" if dialect == "B4" else ("洗浴预约" if dialect == "A" else "")
        specs.extend(_group_time_specs("dhw", "热水预热", dhw_schedule, label))

    # B3 洗浴预约：无功能位，按 schedule_wash.packed 建「洗浴预约n」起止
    wash_schedule = _group(pts, "schedule_wash")
    if wash_schedule:
        specs.extend(_group_time_specs("wash", None, wash_schedule, ""))

    return tuple(specs)


# 平台名 → 「按设备生成 spec 表」的函数（核心 `registry._resolve_specs()` 会传入 device）
PLATFORM_SPECS = {
    "sensor": sensor_specs,
    "binary_sensor": binary_sensor_specs,
    "switch": switch_specs,
    "number": number_specs,
    "select": select_specs,
    "button": button_specs,
    "time": time_specs,
}

"""001 燃气热水器 · 各平台的 spec 表（spec 表存在品类目录里）。

平台模块只做通用渲染（`registry.platform_specs(dev, "<平台>")`），因此本文件是
「001 有哪些实体」的唯一来源；新增品类只需在自己的目录里加同类文件。
此处用字符串而非 HA 枚举，因为品类目录不得依赖 homeassistant（见 `specs.py`）。
"""
from __future__ import annotations

from typing import Dict, Tuple

from ... import features_001 as f001
from ...specs import (
    BinarySensorSpec,
    ButtonSpec,
    NumberSpec,
    SelectSpec,
    SensorSpec,
    SwitchSpec,
)


# ======================================================================
# switch：都是「按一下开/关」，全部是 dvid1 + dvid2 复合命令
# ======================================================================
def _circulation_options() -> Dict[int, str]:
    """循环时长档位对应的文案。"""
    options = dict(f001.CIRCULATION_SPECIAL)
    for level in range(2, 16):
        options[level] = f"{level}分钟"
    return {k: options[k] for k in sorted(options)}


# 状态位定义见 features_001 的位域部分；冷气泡水状态位不可靠，不做状态判断
SWITCH_SPECS: Tuple[SwitchSpec, ...] = (
    SwitchSpec("零冷水/即热", "instant_heat", "零冷水/即热", "mdi:water-sync", "set_instant_heat", "instant_heat_on"),
    SwitchSpec("增压", "boost", "增压", "mdi:water-pump", "set_boost", "boost_on"),
    SwitchSpec("全天循环", "all_day", "全天循环", "mdi:calendar-clock", "set_all_day", "all_day"),
    SwitchSpec("UV杀菌", "uv", "UV杀菌", "mdi:lightbulb-on-outline", "set_uv", "uv_on"),
    SwitchSpec("巡航杀菌", "sterilize", "巡航杀菌", "mdi:shield-sun-outline", "set_sterilize", "sterilizing"),
    SwitchSpec("预约", "reserve", "预约", "mdi:calendar-check-outline", "set_reserve", "reserve_on"),
    SwitchSpec("冷气泡水", "sparkling", "冷气泡水", "mdi:soda-water", "set_sparkling", None, True),
)


# ======================================================================
# number：回差温度、浴缸注水量（设备单位为 10L，对外按 L 呈现）
# ======================================================================
NUMBER_SPECS: Tuple[NumberSpec, ...] = (
    NumberSpec(
        "回差温度", "diff_temp", "回差温度", "mdi:thermometer-lines", "set_diff_temp",
        "diff_temp", f001.DIFF_TEMP_MIN, f001.DIFF_TEMP_MAX, 1, "°C",
    ),
    NumberSpec(
        "浴缸注水量", "bath_volume", "浴缸注水量", "mdi:bathtub-outline", "set_bath_volume",
        "target_volume",
        f001.BATH_VOLUME_MIN * 10, f001.BATH_VOLUME_MAX * 10, f001.BATH_VOLUME_STEP * 10, "L",
        scale=10,
    ),
)


# ======================================================================
# select：离散档位（用 select 承载可从根本上避免非法值）
# ======================================================================
SELECT_SPECS: Tuple[SelectSpec, ...] = (
    SelectSpec(
        "保温时长", "keep_warm", "保温时长", "mdi:thermometer-chevron-up", "set_keep_warm",
        "keep_warm", dict(f001.KEEP_WARM_OPTIONS),
    ),
    SelectSpec(
        "循环时长", "circulation", "循环时长", "mdi:timer-sync-outline", "set_circulation",
        "circulation", _circulation_options(),
    ),
    SelectSpec(
        "厨房洗/定时", "kitchen_timer", "厨房定时", "mdi:stove", "set_kitchen_timer",
        "kitchen_timer", {k: f"{v} 分钟" for k, v in sorted(f001.KITCHEN_TIMER_OPTIONS.items())},
    ),
)


# ======================================================================
# button：一次性动作，无可读状态
# ======================================================================
BUTTON_SPECS: Tuple[ButtonSpec, ...] = (
    ButtonSpec("motion", "点动循环", "mdi:gesture-tap", "set_motion", feature="点动循环"),
    ButtonSpec("reset_water", "累计用水量清零", "mdi:water-off-outline", "reset_water_total", feature="水量/气量清零"),
    ButtonSpec("reset_gas", "累计用气量清零", "mdi:fire-off", "reset_gas_total", feature="水量/气量清零"),
    ButtonSpec("clear_conflict", "清除预约冲突", "mdi:calendar-remove-outline", "clear_reserve_conflict", require_point="245"),
)


# ======================================================================
# sensor：只读状态与用量统计
# ======================================================================
SENSOR_SPECS: Tuple[SensorSpec, ...] = (
    SensorSpec("current_temp", "当前水温", f001.DVID_CURRENT_TEMP, "current_temp",
               "mdi:thermometer-water", "°C", "temperature", 1),
    SensorSpec("target_temp_raw", "设定温度(原始)", f001.DVID_TARGET_TEMP, "target_temp_raw",
               "mdi:thermometer-chevron-up", "°C", "temperature", diagnostic=True),
    SensorSpec("water_flow", "当前水流量", f001.DVID_WATER_FLOW, "water_flow",
               "mdi:water-outline", None, None, diagnostic=True),
    SensorSpec("inlet_temp", "进水温度", f001.DVID_INLET_TEMP, "inlet_temp",
               "mdi:thermometer-low", "°C", "temperature", 1),
    SensorSpec("env_offset", "环境修正值", f001.DVID_WATER_PRESSURE, "env_offset",
               "mdi:tune-vertical", None, None, diagnostic=True),
    SensorSpec("priority", "优先权", f001.DVID_PRIORITY, "priority",
               "mdi:account-key-outline", None, None, diagnostic=True),
    SensorSpec("fa_type", "FA变升类型", f001.DVID_FA_TYPE, "fa_type",
               "mdi:swap-vertical-bold", None, None, diagnostic=True),
    SensorSpec("water_level", "水量档位", f001.DVID_MOTION, "water_level_percent",
               "mdi:chart-arc", "%", None, diagnostic=True),
    SensorSpec("continuous_water_time", "连续用水时间", f001.DVID_CONTINUOUS_TIME,
               "continuous_water_time", "mdi:timer-outline", "min", "duration"),
    SensorSpec("instant_heat_countdown", "即热剩余时间", f001.DVID_HEAT_TIME_L,
               "instant_heat_countdown", "mdi:timer-sand", "min", "duration"),
    SensorSpec("sterilize_countdown", "杀菌剩余时间", f001.DVID_STERILIZE,
               "sterilize_countdown", "mdi:timer-sand-complete", "min", "duration"),
    SensorSpec("water_usage_session", "本次用水量", f001.DVID_USE_WATER_H,
               "water_usage_session", "mdi:water", "L", "water"),
    SensorSpec("gas_usage_session", "本次用气量", f001.DVID_USE_GAS_H,
               "gas_usage_session", "mdi:fire", "m³", "gas"),
    SensorSpec("water_usage_total", "累计用水量", f001.DVID_TOTAL_WATER_3,
               "water_usage_total", "mdi:water-plus", "m³", "water"),
    SensorSpec("gas_usage_total", "累计用气量", f001.DVID_TOTAL_GAS_3,
               "gas_usage_total", "mdi:fire-circle", "m³", "gas"),
    # 以下两点的取值语义未解码（常态 242=7、249=1），只暴露原始值，不做布尔解读，
    # 也就不会出现"恒为异常"的误报。见 features_001.ALARM_NORMAL_VALUES 注释。
    SensorSpec("co_status", "CO状态(原始值)", f001.DVID_CO_ALARM, "co_status_raw",
               "mdi:molecule-co", None, None, diagnostic=True),
    SensorSpec("blockage_raw", "堵塞条件(原始值)", f001.DVID_BLOCKAGE, "blockage_raw",
               "mdi:pipe-valve", None, None, diagnostic=True),
    # HA 没有「时段」实体与「时刻」传感器设备类，故状态给 HH:MM 文本、属性保留原始值
    SensorSpec("next_reserve", "下次预约时间", f001.DVID_NEXT_RESERVE, "next_reserve_time",
               "mdi:clock-outline", format="hhmm"),
    SensorSpec("reserve_schedule", "预约时段", f001.DVID_RESERVE_LIST, "reserve_list",
               "mdi:calendar-multiple", format="reserve_windows"),
)


# ======================================================================
# binary_sensor：运行态与报警位
# ======================================================================
BINARY_SENSOR_SPECS: Tuple[BinarySensorSpec, ...] = (
    BinarySensorSpec("heating", "加热中", f001.DVID_STATUS, "heating",
                     "mdi:fire", "running"),
    BinarySensorSpec("water_flowing", "有水流", f001.DVID_STATUS, "water_flowing",
                     "mdi:water-pump", "running"),
    BinarySensorSpec("pump_running", "水泵运行", f001.DVID_PUMP, "pump_running",
                     "mdi:pump", "running"),
    BinarySensorSpec("motion_running", "水控运行中", f001.DVID_MOTION, "motion_running",
                     "mdi:autorenew", "running"),
    BinarySensorSpec("bath_fill_done", "浴缸注水完成", f001.DVID_DRAIN, "bath_fill_done",
                     "mdi:bathtub-outline", "running"),
    # 241 在字典里同时有「放水报警」与「放水完成」两种叫法，后者是正常事件，
    # 因此不给 PROBLEM 设备类，避免把正常事件显示成"问题/异常"。
    BinarySensorSpec("drain_alarm", "放水状态", f001.DVID_DRAIN_ALARM, "drain_alarm",
                     "mdi:water-check-outline", None, "drain_alarm_raw"),
    BinarySensorSpec("instant_heat_alarm", "即热报警", f001.DVID_HEAT_ALARM, "instant_heat_alarm",
                     "mdi:alert-outline", "problem", "instant_heat_alarm_raw"),
    BinarySensorSpec("reserve_heating", "预约加热中", f001.DVID_RESERVE_HEATING, "reserve_heating",
                     "mdi:calendar-clock", "running"),
    BinarySensorSpec("reserve_conflict", "预约时段冲突", f001.DVID_RESERVE_CONFLICT, "reserve_conflict",
                     "mdi:calendar-alert", "problem"),
)


# 平台名到表的映射（`CategoryPlugin.specs` 取此结构）
PLATFORM_SPECS = {
    "switch": SWITCH_SPECS,
    "number": NUMBER_SPECS,
    "select": SELECT_SPECS,
    "button": BUTTON_SPECS,
    "sensor": SENSOR_SPECS,
    "binary_sensor": BINARY_SENSOR_SPECS,
}

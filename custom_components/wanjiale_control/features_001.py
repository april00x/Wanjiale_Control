"""万家乐 001 产品线（燃气热水器）协议字典与编解码。

本模块集中定义 001 品类的 dvid 语义、命令类型、复合参数编码、位域、
故障码、模式表与能力探测规则。上层（api.py / 各实体平台）只调用本模块，
不直接出现魔法数字，便于后续按品类平级新增（如 features_006.py）。

核心编码（设备约定，两种等价写法并存，一律按整数处理，不要做字符串匹配）：
  opt(1=<命令类型>) + opt(2=<复合参数>)
  复合参数 32 位：byte3<<24 | byte2<<16 | byte1<<8 | byte0
    byte3 = 模式号 / 开关值
    byte2 = 设定温度
    byte1 = 设定水流
    byte0 = 设定水量
  低 16 位 = 0xFFFF 表示「该参数不修改」（哨兵值）。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

# ======================================================================
# 品类标识
# ======================================================================
CATEGORY = "燃气热水器"
PRODUCT = "001"

# ======================================================================
# 可写数据点
# ======================================================================
DVID_OP_TYPE = "1"          # 命令类型（与 dvid2 成对下发）
DVID_OP_VALUE = "2"         # 命令内容（32 位复合参数）
DVID_POWER = "4"            # 模块启动 / 待机（0=关机, 1=开机）
DVID_RESERVE_ADD = "7"      # 添加/修改预约时段：order_s<<16 | order_end（分钟）
DVID_RESERVE_DEL = "8"      # 删除预约时段：now_s<<16 | now_e（分钟）
DVID_RESERVE_CONFLICT = "245"   # 预约时间重叠错误码（写 0 清除）
DVID_CONTROL_RIGHT_SET = "247"  # 设置操作权（值为账号手机号；0=释放）
DVID_CONTROL_RIGHT_REQ = "248"  # 申请操作权（值为账号手机号；0=放弃）
DVID_STERILIZE = "251"      # 巡航杀菌开关（1=开, 0=关）

# ======================================================================
# 只读数据点
# ======================================================================
DVID_PRIORITY = "3"         # 优先权：0 无优先不可控 / 1 无优先可控 / 2 有优先可控
DVID_RESERVE_LIST = "6"     # 预约时段列表：[{s,e}]，单位分钟
DVID_NEXT_RESERVE = "9"     # 下次预约时间（分钟，自 00:00 起）
DVID_STATUS = "10"          # 状态位（开关机/火焰/水流/风机）
DVID_UV = "11"              # UV 杀菌状态（bit7，单一机型证据，待真机验证）
DVID_FAULT = "17"           # 故障代码（255=正常）
DVID_CURRENT_TEMP = "18"    # 当前水温
DVID_FA_TYPE = "19"         # FA 变升类型：0=10L / 4=13L / 8=12L
DVID_BOOST = "20"           # 增压档位（bit0~bit1，0~3）
DVID_MODE = "24"            # 工作模式
DVID_DRAIN = "25"           # 放水情况 / 在线用户（位域）
DVID_WATER_FLOW = "26"      # 当前水流量（L）
DVID_INLET_TEMP = "27"      # 进水温度
DVID_TARGET_TEMP = "28"     # 设定目标水温
DVID_TARGET_FLOW = "29"     # 设定水流量
DVID_TARGET_VOLUME = "30"   # 设定总水量（单位 10L）
DVID_USE_WATER_H = "32"     # 本次用水量 高字节
DVID_USE_WATER_L = "33"     # 本次用水量 低字节
DVID_USE_GAS_H = "34"       # 本次用气量 高字节
DVID_USE_GAS_L = "35"       # 本次用气量 低字节
DVID_TOTAL_GAS_3 = "36"     # 总用气量 最高字节
DVID_TOTAL_GAS_2 = "37"
DVID_TOTAL_GAS_1 = "38"
DVID_TOTAL_GAS_0 = "39"
DVID_CONTINUOUS_TIME = "46"  # 连续用水时间（分钟）
DVID_TOTAL_WATER_3 = "47"    # 总用水量 最高字节
DVID_TOTAL_WATER_2 = "48"
DVID_TOTAL_WATER_1 = "49"
DVID_TOTAL_WATER_0 = "50"
DVID_WATER_PRESSURE = "53"   # 水压/气压/电压/风压/水阀（字典随机型变）
DVID_KEEP_WARM = "54"        # 保温时长档位（×10 分钟）
DVID_DIFF_TEMP = "55"        # 回差温度
DVID_PUMP = "59"             # 水泵 / 预热 / 排气 / 防冻（位域）
DVID_ZERO_COLD = "61"        # 即热、预热功能开关（位域，零冷水核心状态位）
DVID_MOTION = "69"           # 水控 / 即热一次 / 自主学习 / 变升（位域）
DVID_HEAT_TIME_L = "74"      # 即热工作时间 低字节
DVID_HEAT_TIME_H = "75"      # 即热工作时间 高字节
DVID_CIRCULATION = "79"      # 即热循环时长档位
DVID_ALL_DAY = "82"          # 晨浴 / 全天（4=开启）
DVID_KITCHEN_TIMER = "87"    # 厨房洗定时档位（0=30 / 1=60 / 2=120 / 3=180 min）
DVID_SPARKLING = "95"        # 冷/热气状态（单一机型证据，取位方式未验证）
DVID_ONLINE = "1002"         # 设备在线状态（0=离线）
DVID_DRAIN_ALARM = "241"     # 放水报警 / 放水完成（真机常态 = 0）
DVID_CO_ALARM = "242"        # CO 报警（真机常态 = 7，取值语义未解码 → 只暴露原始值）
DVID_HEAT_ALARM = "243"      # 即热报警（真机常态 = 0）
DVID_FA_TYPE_ALT = "246"     # 变升类型（第二路，仅 2 个机型上报，取值含义未解码）
DVID_BLOCKAGE = "249"        # 堵塞条件（"连续6秒小于4L"，真机常态 = 1 → 是条件而非故障，只暴露原始值）
DVID_RESERVE_HEATING = "250" # 预约加热中（1=加热中）

# ======================================================================
# 命令类型（dvid1 取值）
# ======================================================================
OP_MODE_TEMP = 4        # 参数设置：模式 + 温度 + 水流 + 水量（最常用）
OP_RESET_TOTAL = 5      # 累计水量 / 气量清零
OP_UV = 6               # UV 杀菌
OP_BOOST = 7            # 增压
OP_KEEP_WARM = 10       # 保温时长设置
OP_DIFF_TEMP = 11       # 回差温度设置
OP_INSTANT_HEAT = 14    # 即热 / 零冷水开关
OP_RESERVE = 15         # 预约 / 定时开关
OP_MOTION = 17          # 点动（单次）
OP_SUR = 19             # SUR 速热
OP_CONTROL_RIGHT = 24   # 杀菌确认 / 抢占操作权
OP_CIRCULATION = 26     # 即热循环时长设置
OP_ALL_DAY = 27         # 全天循环
OP_KITCHEN = 29         # 厨房洗 / 冷气泡水 / 厨房定时

# ======================================================================
# 编码常量
# ======================================================================
NO_CHANGE = 0xFFFF      # 低 16 位哨兵：该参数不修改
TOGGLE_SUBTYPE = 2      # 开关型命令的 byte2 固定为 2
KITCHEN_SPARKLING_OFFSET = 11   # 冷气泡水在 0x0002FFFF 基础上 +11（语义未解码）

# 复位命令（OP_RESET_TOTAL）
RESET_VALUE_WATER = (1 << 24) + NO_CHANGE   # 16777216 + 65535
RESET_VALUE_GAS = (1 << 16) + NO_CHANGE     # 65536 + 65535

# ======================================================================
# 位域（0-based；设备协议文档用 1-based，换算：值 = 2^(位号-1)）
# ======================================================================
BIT_STATUS_WATER_FLOW = 0x02      # dvid10：有水流
BIT_STATUS_HEATING = 0x04         # dvid10：火焰（加热中）
BIT_ZC_DONE = 0x04                # dvid61：本次即热已完成
BIT_ZC_INSTANT_ON = 0x08          # dvid61：即热开关
BIT_ZC_RESERVE_ON = 0x10          # dvid61：定时/预约开关
BIT_PUMP_RUNNING = 0x02           # dvid59：水泵启动
BIT_BOOST_MASK = 0x03             # dvid20：增压档位 0~3
BIT_MOTION = 0x01                 # dvid69：水控开（用于点亮「点动」按钮）
BIT_MOTION_ONCE = 0x02            # dvid69：即热一次开
BIT_MOTION_RUNNING = 0x04         # dvid69：水控启动
BIT_SELF_LEARN_MODE = 0x08        # dvid69：自主学习模式
BIT_SELF_LEARN_DONE = 0x20        # dvid69：自主学习已学习成功
MASK_WATER_LEVEL = 0xC0           # dvid69：水量档位（0=100% / 1=90% / 2=80%）
SHIFT_WATER_LEVEL = 6
WATER_LEVEL_TEXT = {0: "100%", 1: "90%", 2: "80%"}
WATER_LEVEL_PERCENT = {0: 100, 1: 90, 2: 80}
MASK_DRAIN_STATUS = 0x07          # dvid25：放水情况 / 在线用户档位
BIT_BATH_FILL_DONE = 0x08         # dvid25：本次浴缸注水已完成
BIT_UV_MASK = 0x80                # dvid11：UV 状态（未验证）
MASK_ENV = 0x70                   # dvid53：环境修正值 bit4~bit6
SHIFT_ENV = 4
MASK_SPARKLING = 0xC0             # dvid95：冷/热气档位（1=冷 2=热；未验证）
SHIFT_SPARKLING = 6
SPARKLING_TEXT = {0: "关", 1: "冷气泡水", 2: "热气泡水"}

# ======================================================================
# 报警类点位 241 / 243 的非报警值
# ----------------------------------------------------------------------
# 设备仅声明了数值范围（241、243 均有 0~255 与 0~1 两种），未给出取值枚举，
# 也没有任何可判定为报警的条件分支。
# 实测 241 = 0、243 = 0 且长时间不变化，与本协议族「0 或 255 为无」的惯例一致。
# 因此 0 与 255 视为正常，其余值判为报警；原始值同时保留（*_raw）便于核对。
#
# 242 与 249 不适用此规则：实测常态值分别为 7 和 1，
# 取值语义不是布尔，无法据此判定，故只暴露原始值，不做布尔解读。
# ======================================================================
ALARM_NORMAL_VALUES: tuple = (0, 255)


def is_alarm(value: Optional[int]) -> bool:
    """按 ALARM_NORMAL_VALUES 判断是否处于报警态。"""
    return value is not None and value not in ALARM_NORMAL_VALUES

# ======================================================================
# 模式
# ======================================================================
MODE_COMFORT = 4
MODE_SMART = 5      # 随温感（智温感）
MODE_CHILD = 6      # 儿童浴（仅部分机型）
MODE_ELDER = 7      # 老人浴（仅部分机型）
MODE_ECO = 10
MODE_SUR = 11
MODE_KITCHEN = 14   # 厨房洗

MODE_NAMES: Dict[int, str] = {
    MODE_COMFORT: "舒适浴",
    MODE_SMART: "随温感",
    MODE_CHILD: "儿童浴",
    MODE_ELDER: "老人浴",
    MODE_ECO: "ECO",
    MODE_SUR: "SUR",
    MODE_KITCHEN: "厨房洗",
}

# ======================================================================
# 温度限值（设定温度点位 dvid28 的取值分支）
# ======================================================================
TEMP_MIN = 30
TEMP_MAX = 60
TEMP_MAX_ECO = 48
# 随温感：显示值 = 原始值 + 3 - 环境修正；原始值上限 = 环境修正 + 45
SMART_DISPLAY_OFFSET = 3
SMART_RAW_MAX_BASE = 45

# 保温时长档位（dvid1=10 的 byte3）→ 分钟数；0 表示「即热一次」
KEEP_WARM_OPTIONS: Dict[int, str] = {
    0: "即热一次",
    3: "30 分钟",
    6: "60 分钟",
    9: "90 分钟",
    12: "120 分钟",
    15: "150 分钟",
    18: "180 分钟",
    24: "240 分钟",
    30: "300 分钟",
    72: "720 分钟",
}

# 循环时长档位（dvid1=26 的 byte3）
CIRCULATION_SPECIAL: Dict[int, str] = {0: "全循环", 1: "自学习"}

# 厨房定时档位（dvid1=29 的 byte3）→ 分钟
KITCHEN_TIMER_OPTIONS: Dict[int, int] = {0: 30, 1: 60, 2: 120, 3: 180}

# 回差温度范围 3~15
DIFF_TEMP_MIN = 3
DIFF_TEMP_MAX = 15

# 浴缸注水量范围 6~50，步长 2，单位 10L
BATH_VOLUME_MIN = 6
BATH_VOLUME_MAX = 50
BATH_VOLUME_STEP = 2

# ======================================================================
# 故障码（dvid17；255 = 正常）
# ======================================================================
FAULT_NONE = 255
# 值 → 故障码文案（001 品类各机型一致）
FAULT_TEXT: Dict[int, str] = {
    0: "E0", 1: "E1", 2: "E2", 3: "E3", 4: "E4", 5: "E5", 6: "E6",
    7: "E7", 8: "E8", 9: "E9", 16: "En", 17: "F3", 18: "F4", 19: "F5",
    20: "F6", 21: "F7", 22: "F8", 23: "F9", 24: "EP", 25: "EL",
}
# 注：故障码背后的「标题 + 排查指引」与机型相关（同一编码在不同机型含义可能不同），
# 因此不放本模块，改由 faults_001.json 按 firm_product_model 存放，
# 经 capabilities.fault_info() 读取。本模块只负责「值 → 短码」这一层。


def fault_text(value: Any) -> Optional[str]:
    """故障值 → 短码文案；255（正常）返回 None。"""
    if value is None:
        return None
    code = int(value)
    if code == FAULT_NONE:
        return None
    return FAULT_TEXT.get(code, f"E{code}")

# ======================================================================
# F01 家族基线点位（该家族机型共同点位）
# 用途：作为「设备从未上报时」的实体创建依据，避免实体列表随设备在线状态波动
# ======================================================================
BASELINE_POINTS: frozenset = frozenset({
    "1", "2", "4", "6", "10", "17", "18", "24", "28", "29", "30", "54",
    "59", "61", "74", "75", "241", "242", "243", "247", "248", "250", "1002",
})

# 把非标准 JSON 里的裸键名（如 {s:360}）补上双引号
_JS_BARE_KEY_RE = re.compile(r"([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*:)")


# ======================================================================
# 编码函数
# ======================================================================
def pack_command(
    mode: Optional[int] = None,
    temp: Optional[int] = None,
    flow: Optional[int] = None,
    volume: Optional[int] = None,
) -> int:
    """按 byte 组装 32 位复合参数；None 表示该字节不修改（0xFF）。"""
    b3 = 0xFF if mode is None else mode
    b2 = 0xFF if temp is None else temp
    b1 = 0xFF if flow is None else flow
    b0 = 0xFF if volume is None else volume
    return (b3 << 24) | (b2 << 16) | (b1 << 8) | b0


def pack_toggle(byte3: int, subtype: int = TOGGLE_SUBTYPE) -> int:
    """开关型复合参数：byte3=开关值，byte2=子类型，低 16 位=不修改。

    实测：关 = 2*65536+65535 = 196607，开 = 16777216+2*65536+65535 = 16973823。
    """
    return (byte3 << 24) | (subtype << 16) | NO_CHANGE


def pack_kitchen_timer(slot: int) -> int:
    """厨房洗定时档位：slot<<24 | 2<<16 | 255<<8 | 2。"""
    return (slot << 24) | (TOGGLE_SUBTYPE << 16) | (0xFF << 8) | 0x02


def pack_level(byte3: int) -> int:
    """档位型命令（保温时长/循环时长/回差温度）：byte3=档位, byte2=2, 低 16 位=不修改。"""
    return pack_toggle(byte3)


def pack_mode_temp(mode: int, temp: int, flow: Optional[int], volume: Optional[int]) -> int:
    """模式+温度设置（含保持原有水流/水量）。"""
    return pack_command(mode=mode, temp=temp, flow=flow, volume=volume)


# ======================================================================
# 状态解析
# ======================================================================
def _to_int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def env_offset(state: Dict[str, Any]) -> int:
    """环境修正值 = (dvid53 & 0x70) / 16，缺失时按 0。"""
    raw = _to_int(state.get(DVID_WATER_PRESSURE))
    if raw is None:
        return 0
    return (raw & MASK_ENV) >> SHIFT_ENV


def smart_raw_max(offset: int) -> int:
    """随温感模式下原始温度上限 = 环境修正 + 45。"""
    return offset + SMART_RAW_MAX_BASE


def display_temp(raw: int, mode: Optional[int], offset: int) -> int:
    """原始设定温度 → 对外显示温度。

    仅随温感模式做 +3-环境修正，且上限 48℃。
    """
    if mode == MODE_SMART:
        return min(raw + SMART_DISPLAY_OFFSET - offset, TEMP_MAX_ECO)
    return raw


def raw_temp(display: int, mode: Optional[int], offset: int) -> int:
    """对外显示温度 → 下发的原始设定温度（display_temp 的逆运算）。"""
    if mode == MODE_SMART:
        return display - SMART_DISPLAY_OFFSET + offset
    return display


def temp_limits(mode: Optional[int], offset: int) -> Tuple[int, int]:
    """返回当前模式下的（显示温度下限, 显示温度上限）。"""
    if mode == MODE_SMART:
        raw_max = smart_raw_max(offset)
        low = display_temp(TEMP_MIN, mode, offset)
        high = display_temp(raw_max, mode, offset)
        return max(low, 0), high
    if mode == MODE_ECO:
        return TEMP_MIN, TEMP_MAX_ECO
    return TEMP_MIN, TEMP_MAX


def _parse_reserve_list(value: Any) -> Optional[List[Dict[str, int]]]:
    """解析 dvid6 预约时段列表，统一为 [{"s":分,"e":分}]。

    设备上报的不是合法 JSON：键名不带引号、字符串用单引号包裹，
    实测形如 [{'s':360,'e':540},{'s':1080,'e':1380}]。
    因此不能按严格 JSON 解析，需先做一次字面量归一化。
    """
    data = value
    if isinstance(data, str):
        text = data.strip()
        # 依次尝试：原样 → 裸键名加双引号 → 单引号替换为双引号
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
    result: List[Dict[str, int]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        start = _to_int(item.get("s"))
        end = _to_int(item.get("e"))
        if start is None or end is None:
            continue
        result.append({"s": start, "e": end})
    return result


def parse_state(state: Dict[str, Any]) -> Dict[str, Any]:
    """把合并后的 as 字典解析成语义字段。

    只输出「来源 dvid 存在」的键，使实体能区分「未知」与「值为 0」。
    """
    out: Dict[str, Any] = {}
    get = _to_int

    online = get(state.get(DVID_ONLINE))
    if online is not None:
        out["online"] = online != 0

    power = get(state.get(DVID_POWER))
    if power is not None:
        out["power_on"] = power == 1

    mode = get(state.get(DVID_MODE))
    current_temp = get(state.get(DVID_CURRENT_TEMP))
    target_raw = get(state.get(DVID_TARGET_TEMP))
    offset = env_offset(state)

    if mode is not None:
        out["mode"] = mode
        out["mode_name"] = MODE_NAMES.get(mode)
    if current_temp is not None:
        out["current_temp"] = current_temp
    if target_raw is not None:
        out["target_temp_raw"] = target_raw
        out["target_temp"] = display_temp(target_raw, mode, offset)

    flow_set = get(state.get(DVID_TARGET_FLOW))
    volume_set = get(state.get(DVID_TARGET_VOLUME))
    if flow_set is not None:
        out["target_flow"] = flow_set
    if volume_set is not None:
        out["target_volume"] = volume_set
    if DVID_WATER_PRESSURE in state:
        out["env_offset"] = offset

    water_flow = get(state.get(DVID_WATER_FLOW))
    if water_flow is not None:
        out["water_flow"] = water_flow
    inlet_temp = get(state.get(DVID_INLET_TEMP))
    if inlet_temp is not None:
        out["inlet_temp"] = inlet_temp

    fault = get(state.get(DVID_FAULT))
    if fault is not None:
        out["fault"] = fault
        out["fault_text"] = fault_text(fault)

    status = get(state.get(DVID_STATUS))
    if status is not None:
        out["status"] = status
        out["water_flowing"] = bool(status & BIT_STATUS_WATER_FLOW)
        out["heating"] = bool(status & BIT_STATUS_HEATING)

    zero_cold = get(state.get(DVID_ZERO_COLD))
    if zero_cold is not None:
        out["zero_cold_raw"] = zero_cold
        out["instant_heat_on"] = bool(zero_cold & BIT_ZC_INSTANT_ON)
        out["instant_heat_done"] = bool(zero_cold & BIT_ZC_DONE)
        out["reserve_on"] = bool(zero_cold & BIT_ZC_RESERVE_ON)

    pump = get(state.get(DVID_PUMP))
    if pump is not None:
        out["pump_running"] = bool(pump & BIT_PUMP_RUNNING)

    boost = get(state.get(DVID_BOOST))
    if boost is not None:
        out["boost_level"] = boost & BIT_BOOST_MASK
        out["boost_on"] = out["boost_level"] > 0

    motion = get(state.get(DVID_MOTION))
    if motion is not None:
        out["motion_raw"] = motion
        out["motion_on"] = bool(motion & BIT_MOTION)
        out["motion_once"] = bool(motion & BIT_MOTION_ONCE)
        out["motion_running"] = bool(motion & BIT_MOTION_RUNNING)
        out["self_learn_mode"] = bool(motion & BIT_SELF_LEARN_MODE)
        out["self_learn_done"] = bool(motion & BIT_SELF_LEARN_DONE)
        level = (motion & MASK_WATER_LEVEL) >> SHIFT_WATER_LEVEL
        if level in WATER_LEVEL_TEXT:
            out["water_level"] = WATER_LEVEL_TEXT[level]
            out["water_level_percent"] = WATER_LEVEL_PERCENT[level]

    drain = get(state.get(DVID_DRAIN))
    if drain is not None:
        out["drain_raw"] = drain
        out["drain_status"] = drain & MASK_DRAIN_STATUS
        out["bath_fill_done"] = bool(drain & BIT_BATH_FILL_DONE)

    uv = get(state.get(DVID_UV))
    if uv is not None:
        out["uv_on"] = bool(uv & BIT_UV_MASK)

    sterilize = get(state.get(DVID_STERILIZE))
    if sterilize is not None:
        out["sterilizing"] = sterilize == 1

    keep_warm = get(state.get(DVID_KEEP_WARM))
    if keep_warm is not None:
        out["keep_warm"] = keep_warm

    diff_temp = get(state.get(DVID_DIFF_TEMP))
    if diff_temp is not None:
        out["diff_temp"] = diff_temp

    circulation = get(state.get(DVID_CIRCULATION))
    if circulation is not None:
        out["circulation"] = circulation

    all_day = get(state.get(DVID_ALL_DAY))
    if all_day is not None:
        out["all_day_raw"] = all_day
        out["all_day"] = all_day == 4

    kitchen_timer = get(state.get(DVID_KITCHEN_TIMER))
    if kitchen_timer is not None:
        out["kitchen_timer"] = kitchen_timer

    sparkling = get(state.get(DVID_SPARKLING))
    if sparkling is not None:
        out["sparkling_raw"] = sparkling
        # 取值取 bit6~bit7 两位字段，未在真机验证，仅供只读参考。
        mode = (sparkling & MASK_SPARKLING) >> SHIFT_SPARKLING
        if mode in SPARKLING_TEXT:
            out["sparkling_mode"] = mode
            out["sparkling_text"] = SPARKLING_TEXT[mode]

    next_reserve = get(state.get(DVID_NEXT_RESERVE))
    if next_reserve is not None:
        out["next_reserve_time"] = next_reserve

    reserve_conflict = get(state.get(DVID_RESERVE_CONFLICT))
    if reserve_conflict is not None:
        out["reserve_conflict"] = reserve_conflict != 0

    fa_type_alt = get(state.get(DVID_FA_TYPE_ALT))
    if fa_type_alt is not None:
        out["fa_type_alt"] = fa_type_alt

    # 倒计时：剩余 = 保温档位*10 - (75*256 + 74)；档位为 0（即热一次）时不显示倒计时
    heat_low = get(state.get(DVID_HEAT_TIME_L))
    heat_high = get(state.get(DVID_HEAT_TIME_H))
    elapsed: Optional[int] = None
    if heat_low is not None and heat_high is not None:
        elapsed = heat_high * 256 + heat_low
    if elapsed is not None and out.get("instant_heat_on") and keep_warm:
        out["instant_heat_countdown"] = max(keep_warm * 10 - elapsed, 0)
    if elapsed is not None and out.get("sterilizing"):
        out["sterilize_countdown"] = max(30 - elapsed, 0)

    priority = get(state.get(DVID_PRIORITY))
    if priority is not None:
        out["priority"] = priority
        out["controllable"] = priority in (1, 2)

    fa_type = get(state.get(DVID_FA_TYPE))
    if fa_type is not None:
        out["fa_type"] = fa_type

    # 报警类点位 241/243：判据与局限见 ALARM_NORMAL_VALUES 的注释；原始值一并保留
    for key, flag, raw_key in (
        (DVID_DRAIN_ALARM, "drain_alarm", "drain_alarm_raw"),
        (DVID_HEAT_ALARM, "instant_heat_alarm", "instant_heat_alarm_raw"),
    ):
        value = get(state.get(key))
        if value is not None:
            out[raw_key] = value
            out[flag] = is_alarm(value)

    # 242（实测常态 7）与 249（实测常态 1）的取值语义未解码，不做布尔解读，只暴露原始值
    co_status = get(state.get(DVID_CO_ALARM))
    if co_status is not None:
        out["co_status_raw"] = co_status

    blockage = get(state.get(DVID_BLOCKAGE))
    if blockage is not None:
        out["blockage_raw"] = blockage

    reserve_heating = get(state.get(DVID_RESERVE_HEATING))
    if reserve_heating is not None:
        out["reserve_heating"] = reserve_heating == 1

    if DVID_RESERVE_LIST in state:
        out["reserve_list"] = _parse_reserve_list(state.get(DVID_RESERVE_LIST))

    continuous = get(state.get(DVID_CONTINUOUS_TIME))
    if continuous is not None:
        out["continuous_water_time"] = continuous

    # 用量统计（多字节拼接；本次用水量为 L，其余为 0.001 立方）
    use_water_h = get(state.get(DVID_USE_WATER_H))
    use_water_l = get(state.get(DVID_USE_WATER_L))
    if use_water_h is not None and use_water_l is not None:
        out["water_usage_session"] = use_water_h * 256 + use_water_l

    use_gas_h = get(state.get(DVID_USE_GAS_H))
    use_gas_l = get(state.get(DVID_USE_GAS_L))
    if use_gas_h is not None and use_gas_l is not None:
        out["gas_usage_session"] = round((use_gas_h * 256 + use_gas_l) / 1000, 3)

    gas_bytes = [get(state.get(d)) for d in (DVID_TOTAL_GAS_3, DVID_TOTAL_GAS_2, DVID_TOTAL_GAS_1, DVID_TOTAL_GAS_0)]
    if all(v is not None for v in gas_bytes):
        total = 0
        for v in gas_bytes:
            total = total * 256 + int(v)  # type: ignore[arg-type]
        out["gas_usage_total"] = round(total / 1000, 3)

    water_bytes = [get(state.get(d)) for d in (DVID_TOTAL_WATER_3, DVID_TOTAL_WATER_2, DVID_TOTAL_WATER_1, DVID_TOTAL_WATER_0)]
    if all(v is not None for v in water_bytes):
        total = 0
        for v in water_bytes:
            total = total * 256 + int(v)  # type: ignore[arg-type]
        out["water_usage_total"] = round(total / 1000, 3)

    return out


# ======================================================================
# 功能定义（能力探测与实体创建的依据）
#   platform        该功能落在哪个 HA 平台
#   probe_dvids     运行时探测依据：设备上报过其中任一 dvid 即视为支持
#   probe_required  模板声明后是否还需实机探测确认才能创建（模板可能过度声明）
#   requires_power  是否需要设备已开机才允许操作
#   fault_exempt    设备故障时是否仍允许操作（仅开关机与用量清零放行）
#   extra           不在能力表 flagOrder 中的补充功能
#
# probe_required 用于模板声明可能高于实机能力的功能；
# 没有可观测点位的功能（如用量清零）无法探测，只能信任模板声明。
# ======================================================================
FEATURES: Dict[str, Dict[str, Any]] = {
    "开关机":        {"platform": "water_heater", "probe_dvids": ("4",),    "requires_power": False, "fault_exempt": True},
    "模式切换":      {"platform": "water_heater", "probe_dvids": ("24",),   "requires_power": True},
    "预约":          {"platform": "switch",       "probe_dvids": ("61", "6"), "requires_power": False, "probe_required": True},
    "零冷水/即热":   {"platform": "switch",       "probe_dvids": ("61",),   "requires_power": True,  "probe_required": True},
    "保温时长":      {"platform": "select",       "probe_dvids": ("54",),   "requires_power": True,  "probe_required": True},
    "回差温度":      {"platform": "number",       "probe_dvids": ("55",),   "requires_power": True,  "probe_required": True},
    "循环时长":      {"platform": "select",       "probe_dvids": ("79",),   "requires_power": True,  "probe_required": True},
    "全天循环":      {"platform": "switch",       "probe_dvids": ("82",),   "requires_power": True,  "probe_required": True},
    "点动循环":      {"platform": "button",       "probe_dvids": ("69",),   "requires_power": True,  "probe_required": True},
    "增压":          {"platform": "switch",       "probe_dvids": ("20",),   "requires_power": True,  "probe_required": True},
    "UV杀菌":        {"platform": "switch",       "probe_dvids": ("11",),   "requires_power": True,  "probe_required": True},
    "厨房洗/定时":   {"platform": "select",       "probe_dvids": ("87",),   "requires_power": True,  "probe_required": True},
    "水量/气量清零": {"platform": "button",       "probe_dvids": (),       "requires_power": False, "fault_exempt": True},
    "优先权申请":    {"platform": "internal",     "probe_dvids": (),       "requires_power": False},
    "优先权247":     {"platform": "internal",     "probe_dvids": (),       "requires_power": False},
    "优先权248":     {"platform": "internal",     "probe_dvids": (),       "requires_power": False},
    "巡航杀菌":      {"platform": "switch",       "probe_dvids": ("251",),  "requires_power": True,  "probe_required": True},
    # 能力表未收录的补充功能：无模板证据，只能在运行时探测到点位或人工开启后才创建
    "冷气泡水":      {"platform": "switch",       "probe_dvids": ("95",),   "requires_power": True,  "extra": True},
    "浴缸注水量":    {"platform": "number",       "probe_dvids": ("30",),   "requires_power": True,  "extra": True},
}

"""平台实体的「规格」数据结构（核心层，零 homeassistant 依赖）。

平台模块只负责把 spec 渲染成 HA 实体；spec 表本体存在品类目录里，因此新增品类
只需加一个目录，平台模块一行不改（否则每个品类都要改同 7 个平台文件）。

`device_class` / `unit` 用字符串而不是 HA 枚举：品类目录（`categories/**`）不得
依赖 homeassistant，离线分析工具与单测都直接装载它们；HA 的平台模块在渲染时把
字符串转成对应枚举（HA 的枚举都是 `str` 子类，转换无损）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple


@dataclass(frozen=True)
class SwitchSpec:
    """一个开关功能到设备/角色能力的映射。"""

    feature: Optional[str]              # 能力名（决定是否创建实体）；None = 只按点位生成
    key: str                            # unique_id 短键
    name: str                           # 实体名
    icon: str
    method: str                         # 角色上的控制方法名（`set_xxx(*method_args, on)`）
    state_key: Optional[str] = None     # parsed 中的状态键；None 表示状态不可靠
    assumed: bool = False               # 状态位未经验证时置 True，UI 显示为可切换按钮
    device_level: bool = False          # True = 直接对设备渲染（组实体/温控器），不进角色
    require_points: Tuple[str, ...] = ()  # 点位路径（如 ("schedule_heat.enable",)）
    method_args: Tuple[Any, ...] = ()   # 调用方法时预绑定的参数（如 ("heat", 3)）


@dataclass(frozen=True)
class NumberSpec:
    feature: Optional[str]
    key: str
    name: str
    icon: str
    method: str                         # `set_xxx(*method_args, int)`
    state_key: str
    min_value: float
    max_value: float
    step: float
    unit: Optional[str] = None
    scale: int = 1                      # 设备值 × scale = 对外值（如浴缸注水量以 10L 为单位）
    device_level: bool = False
    require_points: Tuple[str, ...] = ()
    method_args: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class SelectSpec:
    """枚举型功能。

    `options` 与 `options_from` 二选一：`options` 是静态档位表
    （001 的保温时长 / 循环时长 / 厨房定时）；`options_from` 是角色上的方法名，
    运行时返回 `dict[int, str]`：008 的模式表是机型级的（同一按键在不同机型取值
    不同），只能用这种方式。
    """

    feature: Optional[str]
    key: str
    name: str
    icon: str
    method: str                         # `set_xxx(*method_args, int)`
    state_key: str
    options: Optional[Dict[int, str]] = None
    options_from: Optional[str] = None
    device_level: bool = False
    require_points: Tuple[str, ...] = ()
    method_args: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class ButtonSpec:
    """一次性动作（无可读状态）。"""

    key: str
    name: str
    icon: str
    method: str                         # `method(*method_args)`
    feature: Optional[str] = None       # 依赖的能力名；None 表示按点位判断
    require_point: Optional[str] = None
    device_level: bool = False
    require_points: Tuple[str, ...] = ()
    method_args: Tuple[Any, ...] = ()


@dataclass(frozen=True)
class ScheduleTimeSpec:
    """预约组起/止时间（HA `time` 平台，设备级）。

    002 的供暖/热水预约按组拆分：`kind` 决定语义与显示名前缀，
    `state_key` 指向 `parse_state` 输出的分钟值键。
    """

    kind: str        # "heat" / "dhw" / "wash"
    group: int       # 1~5
    field: str       # "start" / "end"
    dpid: str
    feature: Optional[str]     # "预约供暖" / "热水预热"；None = 只按点位生成
    state_key: str
    label: str = ""  # 可选显示名前缀；HQJ 的 dhw 组填「热水定时」



@dataclass(frozen=True)
class SensorSpec:
    key: str
    name: str
    dvid: str                           # 触发创建的点位
    value_key: str                      # parsed 中的取值键
    icon: str
    unit: Optional[str] = None
    device_class: Optional[str] = None
    precision: Optional[int] = None
    diagnostic: bool = False
    format: Optional[str] = None        # 需要格式化时的格式名（见 sensor.py 的 _FORMATTERS）


@dataclass(frozen=True)
class BinarySensorSpec:
    key: str
    name: str
    dvid: str
    value_key: str
    icon: str
    device_class: Optional[str] = None
    raw_key: Optional[str] = None       # parsed 中的原始值键，作为属性输出便于核对


# 平台名到 spec 类型的映射（供校验与文档；平台模块渲染时按名字取值）
SPEC_TYPES = {
    "switch": SwitchSpec,
    "number": NumberSpec,
    "select": SelectSpec,
    "button": ButtonSpec,
    "sensor": SensorSpec,
    "binary_sensor": BinarySensorSpec,
    "time": ScheduleTimeSpec,
}


def spec_keys(specs: List[object]) -> List[str]:
    """取出 spec 的短键列表（供测试断言 unique_id 拼接）。"""
    return [str(getattr(spec, "key", "")) for spec in specs]


def require_points_satisfied(spec: object, points: Mapping[str, Any]) -> bool:
    """`spec.require_points` 是否全部存在（点位路径用点号分隔）。

    用于「同一功能位下按点位存在性过滤」：例如 B3 的洗浴预约没有供暖 `total`，
    不能据此创建供暖预约主开关。路径不存在（None/空串/空容器）即视为不满足。
    """
    paths = getattr(spec, "require_points", ()) or ()
    for path in paths:
        node: Any = points
        for part in str(path).split("."):
            if not isinstance(node, Mapping) or part not in node:
                return False
            node = node[part]
        if node is None or node == "" or node == [] or node == {}:
            return False
    return True

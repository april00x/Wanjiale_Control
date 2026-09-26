"""平台实体的「规格」数据结构（核心层，零 homeassistant 依赖）。

平台模块只负责把 spec 渲染成 HA 实体；spec 表本体存在品类目录里，因此新增品类
只需加一个目录，平台模块一行不改（否则每个品类都要改同 7 个平台文件）。

`device_class` / `unit` 用字符串而不是 HA 枚举：品类目录（`categories/**`）不得
依赖 homeassistant，离线分析工具与单测都直接装载它们；HA 的平台模块在渲染时把
字符串转成对应枚举（HA 的枚举都是 `str` 子类，转换无损）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass(frozen=True)
class SwitchSpec:
    """一个开关功能到设备/角色能力的映射。"""

    feature: str                        # 能力名（决定是否创建实体）
    key: str                            # unique_id 短键
    name: str                           # 实体名
    icon: str
    method: str                         # 角色上的控制方法名（`set_xxx(on: bool)`）
    state_key: Optional[str] = None     # parsed 中的状态键；None 表示状态不可靠
    assumed: bool = False               # 状态位未经验证时置 True，UI 显示为可切换按钮


@dataclass(frozen=True)
class NumberSpec:
    feature: str
    key: str
    name: str
    icon: str
    method: str                         # `set_xxx(int)`
    state_key: str
    min_value: float
    max_value: float
    step: float
    unit: Optional[str] = None
    scale: int = 1                      # 设备值 × scale = 对外值（如浴缸注水量以 10L 为单位）


@dataclass(frozen=True)
class SelectSpec:
    """枚举型功能。

    `options` 与 `options_from` 二选一：`options` 是静态档位表
    （001 的保温时长 / 循环时长 / 厨房定时）；`options_from` 是角色上的方法名，
    运行时返回 `dict[int, str]`：008 的模式表是机型级的（同一按键在不同机型取值
    不同），只能用这种方式。
    """

    feature: str
    key: str
    name: str
    icon: str
    method: str                         # `set_xxx(int)`
    state_key: str
    options: Optional[Dict[int, str]] = None
    options_from: Optional[str] = None


@dataclass(frozen=True)
class ButtonSpec:
    """一次性动作（无可读状态）。"""

    key: str
    name: str
    icon: str
    method: str                         # `method()` 无参
    feature: Optional[str] = None       # 依赖的能力名；None 表示按点位判断
    require_point: Optional[str] = None


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
}


def spec_keys(specs: List[object]) -> List[str]:
    """取出 spec 的短键列表（供测试断言 unique_id 拼接）。"""
    return [str(getattr(spec, "key", "")) for spec in specs]

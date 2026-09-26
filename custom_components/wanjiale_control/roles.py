"""平台设备接口角色视图。

接口的实现单位是「角色（Role）」而不是「设备」：实体 = 设备 × 角色。
原因是 002 壁挂炉需在同一台设备上并存「卫浴目标温度(108)」与
「供暖目标温度(109)」，一个设备对象无法用同一个 `target_temperature`
同时服务两个实体；因此设备把语义按角色拆分，平台消费角色。
2026-09-26 先这么拆分，后续实现壁挂炉功能时根据实际情况调整 
rules：
- 平台层只认接口、不认品类，判定一律用 `isinstance(role, XxxProto)` 而不是
  `isinstance(dev, WanjialeGasWaterHeater)`，结构化判定使新增品类无需注册；
- `unique_id = f"{dev.unique_id()}-{spec.key}{role.suffix}"`；`role.suffix` 默认空串，
  既有 001 实体 ID 不变，002 的两个温度角色分别用 `-dhw` / `-heating`；
- 门控与错误语义留在设备/角色内部：平台只调方法，失败一律抛
  `WanjialeControlError`（由 `entity._control()` 转成 `HomeAssistantError`）；
- 只读品类不得实现任何 setter，否则会被 `switch` 等平台的 spec 命中而冒出控制实体；
- 通用只读设备（未注册品类）不得实现任何角色接口，因此天然不出控制实体，
  只由 `sensor` 平台的「原始状态」实体承载（见 `sensor.py`）。

状态读取用属性、动作调用用方法，与既有 001 设备类的真实形态一致
（`is_power_on` / `current_temperature` / `target_temperature` / `current_mode` 是
`@property`，而 `mode_names()` / `min_temp()` / `set_temperature()` 是方法）。
`@runtime_checkable` 的 `isinstance` 只检查属性是否存在、不检查是否可调用，
因此若把状态写成 `def is_power_on(self)`，判定会假通过而调用时炸掉。

由 `WanjialeDevice` 保证的基础接口（所有实体共用）：`did` / `name` / `model` /
`category_cn` / `ver` / `online`（用于 `device_info()`、`available`）、`unique_id()`
（实体唯一 ID 前缀）、`parsed`（只读实体的取值来源）、`caps` / `features` / `points`
（能力判定）、`has_feature(name)` / `has_point(dvid)`（实体创建条件）、`role_views()`
（返回该设备可实现的所有角色，默认 `[self]`，002 返回 `[dhw, heating]`）、
`role_suffix`（角色后缀，默认空串以保证既有实体 ID 不变）、
`apply_state(as_data, fids, raw)`（一次性接收状态与故障）。

`parsed` 的两个跨品类约定（核心层与平台层都依赖）：`fault`（故障值，与
`plugin.fault_none` 比较）与 `fault_text`（可选文案）。
"""
from __future__ import annotations

from typing import Dict, Optional, Protocol, runtime_checkable


@runtime_checkable
class WaterHeaterProto(Protocol):
    """`water_heater` 平台消费的角色（001 / 008 / 002 的卫浴角色）。"""

    role_suffix: str                      # "" / "-dhw"
    supports_mode_selection: bool
    """该角色是否对外暴露「模式选择」（`water_heater` 的 `OPERATION_MODE`）。

    未实现者按 `True` 处理（平台层用 `getattr(..., True)` 兜底），保证既有行为不变；
    001 由 `WanjialeGasWaterHeater` 覆盖为属性，按机型模板是否定义了模式表动态给出，
    模板模式表为空时不暴露模式选择。
    """

    # ---- 状态（属性） ----
    @property
    def is_power_on(self) -> Optional[bool]: ...
    @property
    def current_temperature(self) -> Optional[float]: ...
    @property
    def target_temperature(self) -> Optional[float]: ...
    @property
    def current_mode(self) -> Optional[int]: ...

    # ---- 元信息与动作（方法） ----
    def mode_names(self) -> Dict[int, str]: ...      # 机型级模式表 ∪ 实机上报
    def min_temp(self) -> float: ...
    def max_temp(self) -> float: ...
    def turn_on(self) -> None: ...
    def turn_off(self) -> None: ...
    def set_temperature(self, temperature: int) -> None: ...
    def set_mode(self, mode: int) -> None: ...


@runtime_checkable
class ClimateProto(Protocol):
    """`climate` 平台消费的角色（002 的供暖角色）。"""

    role_suffix: str                      # "-heating"

    # ---- 状态（属性） ----
    @property
    def is_power_on(self) -> Optional[bool]: ...
    @property
    def current_temperature(self) -> Optional[float]: ...
    @property
    def target_temperature(self) -> Optional[float]: ...
    @property
    def current_hvac_mode(self) -> Optional[int]: ...

    # ---- 元信息与动作（方法） ----
    def hvac_modes(self) -> Dict[int, str]: ...
    def min_temp(self) -> float: ...
    def max_temp(self) -> float: ...
    def turn_on(self) -> None: ...
    def turn_off(self) -> None: ...
    def set_temperature(self, temperature: int) -> None: ...
    def set_hvac_mode(self, mode: int) -> None: ...


@runtime_checkable
class FanProto(Protocol):
    """`fan` 平台消费的角色（006 / 028 / 073 / 099 油烟机与集成灶）。"""

    role_suffix: str

    # ---- 状态（属性） ----
    @property
    def is_on(self) -> Optional[bool]: ...
    @property
    def percentage(self) -> Optional[int]: ...
    @property
    def current_preset_mode(self) -> Optional[int]: ...

    # ---- 元信息与动作（方法） ----
    def preset_modes(self) -> Dict[int, str]: ...     # 风量档位 0~4
    def turn_on(self) -> None: ...
    def turn_off(self) -> None: ...
    def set_preset_mode(self, mode: int) -> None: ...

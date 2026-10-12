"""008 电热水器 · 各平台 spec 表（按**设备**动态生成）。

与 001 的静态表不同：008 的实体**名单本身**随机型变（不同机型开关不同、dpid 与
中文标签都不同），静态表装不下，故 `PLATFORM_SPECS` 的值是 `Callable[[device], Tuple[Spec, ...]]`
（由核心的 `registry._resolve_specs()` 解析，见实施方案 C3）。

主实体（`water_heater`）不走 spec，由 `WaterHeaterProto` 属性驱动（与 001 一致）。
故障明细走 `WanjialeFaultSensor`（由 C4 判定创建，非 spec 驱动）。

点位一律取 `device.caps.model_points`（= 能力表 `devices[*].points`），
不做任何跨机型推断。
"""
from __future__ import annotations

from typing import Any, Dict, Tuple

from ...specs import BinarySensorSpec, SensorSpec, SwitchSpec

# 辅助开关默认图标（按规范功能名；缺失则退回通用热水器图标）
_AUX_ICON: Dict[str, str] = {
    "预约": "mdi:calendar-check-outline",
    "无电洗": "mdi:power-plug-off-outline",
    "智能抑菌": "mdi:shield-sun-outline",
    "变频速热": "mdi:flash-outline",
    "冬季": "mdi:snowflake",
    "智能增容": "mdi:water-plus-outline",
    "峰谷夜电": "mdi:transmission-tower",
    "智能管家": "mdi:home-automation",
    "防冻": "mdi:snowflake-alert",
    "保养提醒": "mdi:wrench-clock",
    "增容": "mdi:water-plus",
    "净肤浴": "mdi:shower-head",
    "元气浴": "mdi:shower",
    "养肤浴": "mdi:hand-water",
    "瀑布洗": "mdi:waterfall",
    "富氧洗": "mdi:molecule",
    "恒温浴": "mdi:thermometer-water",
}


def _points(device: Any) -> Dict[str, Any]:
    caps = getattr(device, "caps", None)
    points = getattr(caps, "model_points", None)
    return points if isinstance(points, dict) else {}


def _aux(device: Any) -> Tuple[Dict[str, Any], ...]:
    aux = _points(device).get("aux")
    return tuple(item for item in (aux or ()) if isinstance(item, dict) and item.get("feature"))


def switch_specs(device: Any) -> Tuple[SwitchSpec, ...]:
    """按机型 aux 列表生成开关实体（dpid / 中文标签 / 键都随机型变）。"""
    specs = []
    for item in _aux(device):
        feature = str(item["feature"])
        key = str(item.get("key") or feature)
        specs.append(
            SwitchSpec(
                feature=feature,
                key=key,
                name=str(item.get("name") or feature),
                icon=str(item.get("icon") or _AUX_ICON.get(feature, "mdi:water-boiler")),
                method=f"set_{key}",
                # 状态位就来自该开关自己的 dpid（parse_state 输出 "<key>_on"）
                state_key=f"{key}_on",
            )
        )
    return tuple(specs)


def sensor_specs(device: Any) -> Tuple[SensorSpec, ...]:
    """当前热水量 / 当前水温，dpid 取机型实际点位。

    实体创建仍由 `dev.has_point(spec.dvid)` 门控：008 的 `base_points` 为空，
    点位集合只含**实测**点位，故设备首轮上报后实体才出现（实施方案 §4.5，已接受）。
    """
    pts = _points(device)
    specs = []
    hot = (pts.get("hotWater") or {}).get("dpid")
    if hot:
        # HA 无「热水量」对应 device_class，用单位 + 诊断类承载
        specs.append(
            SensorSpec(
                key="hot_water", name="当前热水量", dvid=str(hot), value_key="hot_water",
                icon="mdi:water-percent", unit="%", device_class=None,
                precision=None, diagnostic=True,
            )
        )
    cur = (pts.get("curTemp") or {}).get("dpid")
    if cur:
        specs.append(
            SensorSpec(
                key="current_temp", name="当前水温", dvid=str(cur), value_key="current_temp",
                icon="mdi:thermometer-water", unit="°C", device_class="temperature",
                precision=1,
            )
        )
    return tuple(specs)


def binary_sensor_specs(device: Any) -> Tuple[BinarySensorSpec, ...]:
    """加热中 / 保温中（状态点）。

    **本轮返回空表**：S0 核对确认 `per_model.csv` 无 H5 状态语义，状态点真源是各机型
    `js/index.js` 的 `statustip()`（随机型变、取值映射亦机型级），无法从数据资产机械派生
    → 按「无证据则留空」不产出（实施方案 §2.5-C、§九-10）。
    将来若逐机型确认了 `statustip()` 入参，把状态点写进能力表 `points.status` 并在此返回即可。
    """
    return ()


# 平台名 → 「按设备生成 spec 表」的函数（核心 `registry._resolve_specs()` 会传入 device）
PLATFORM_SPECS = {
    "switch": switch_specs,
    "sensor": sensor_specs,
    "binary_sensor": binary_sensor_specs,
}

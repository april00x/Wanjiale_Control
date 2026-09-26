"""001 燃气热水器 · 品类插件。

001 的语义与点位留在 `features_001.py`，本文件只把它按 `CategoryPlugin` 契约对外暴露。

未验证项：`DVID_UV`(11) / `DVID_SPARKLING`(95) / `DVID_FA_TYPE_ALT`(246) 只有单机型证据，
取位方式未经确认（详见 features_001 内注释）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ... import features_001 as f001
from ...api import WanjialeGasWaterHeater
from ...registry import CategoryPlugin, register_plugin
from . import specs as platform_specs


def _op_pair(op_type: int, value: int) -> Dict[str, str]:
    """命令类型 + 命令内容 的成对报文（001 的复合命令一律成对下发）。"""
    return {f001.DVID_OP_TYPE: str(op_type), f001.DVID_OP_VALUE: str(value)}


def parse_state(
    state: Dict[str, Any],
    fids: Any = None,
    profile: Any = None,
) -> Dict[str, Any]:
    """契约签名 `(state, fids, profile)`，001 只用 `state`。

    `fids`（云端故障列表）与 `profile`（机型档案）为 008/006 等品类预留；
    001 的故障取自设备点 `dvid17`，故此处忽略。
    """
    return f001.parse_state(state)


def encode_value(action: str, **params: Any) -> Dict[str, str]:
    """语义动作映射为 `{dvid: 绝对字符串}`（只做值计算，不含报文信封）。

    设备对象的控制方法委托到此处，避免同一套打包规则出现两份实现。
    动作有 power、mode_temp、toggle、level、kitchen_timer、sparkling、
    reset_total 与 control_right。
    """
    if action == "power":
        return {f001.DVID_POWER: "1" if params["on"] else "0"}

    if action == "mode_temp":
        value = f001.pack_mode_temp(
            int(params["mode"]),
            params["temp"],
            params.get("flow"),
            params.get("volume"),
        )
        return _op_pair(f001.OP_MODE_TEMP, value)

    if action == "toggle":
        return _op_pair(int(params["op"]), f001.pack_toggle(int(params["byte3"])))

    if action == "level":
        return _op_pair(int(params["op"]), f001.pack_level(int(params["level"])))

    if action == "kitchen_timer":
        return _op_pair(f001.OP_KITCHEN, f001.pack_kitchen_timer(int(params["slot"])))

    if action == "sparkling":
        # 冷气泡水在标准开关值上额外 +11（KITCHEN_SPARKLING_OFFSET，语义未解码）
        value = f001.pack_toggle(int(params["byte3"])) + f001.KITCHEN_SPARKLING_OFFSET
        return _op_pair(f001.OP_KITCHEN, value)

    if action == "reset_total":
        target = str(params["target"])
        if target not in ("water", "gas"):
            raise ValueError(f"reset_total 只支持 water/gas，收到 {target!r}")
        value = f001.RESET_VALUE_WATER if target == "water" else f001.RESET_VALUE_GAS
        return _op_pair(f001.OP_RESET_TOTAL, value)

    if action == "control_right":
        # 抢占多端操作权三帧中的第一帧（命令类型 24 + 开关值 1）；
        # 另两帧是跨品类的 247/248 写入，由核心层发出（见 api._before_control）
        return _op_pair(f001.OP_CONTROL_RIGHT, f001.pack_toggle(1))

    raise ValueError(f"未支持的语义动作：{action!r}")


def build_plugin() -> CategoryPlugin:
    """构造 001 的插件对象（由 `plugin = register_plugin(build_plugin())` 注册）。"""
    return CategoryPlugin(
        product=f001.PRODUCT,
        category_cn=f001.CATEGORY,
        device_class=WanjialeGasWaterHeater,
        platforms=frozenset(
            {
                "water_heater",
                "switch",
                "number",
                "select",
                "button",
                "sensor",
                "binary_sensor",
            }
        ),
        features=f001.FEATURES,
        # spec 表放品类目录，平台模块只做通用渲染
        specs=platform_specs.PLATFORM_SPECS,
        capability_file="capability_001.json",
        faults_file="faults_001.json",
        parse_state=parse_state,
        encode_value=encode_value,
        base_points=f001.BASELINE_POINTS,
        baseline_families=frozenset({"F01"}),
        always_on_features=frozenset({"开关机"}),
        # 只读机型：OYY(F45) 与 HPZ(F01) 的活动协议是 101+ 第二路体系
        # （js/index.js 只读写 key101-105/114；HPZ 的 config.xml 是遗留打包模板，
        #  被误聚类进 F01）。按标准 001 点位下发会误控，故不认领、只读可见。
        readonly_models=frozenset({"WJL_001_OYY", "WJL_001_HPZ"}),
        fault_points={"device": (f001.DVID_FAULT,)},
        fault_none=(f001.FAULT_NONE,),
        control_rights=True,
        encoding_note=(
            "复合命令成对下发（dvid1=命令类型, dvid2=32位复合参数）；"
            "复合参数 byte3/byte2/byte1/byte0 = 模式(或开关值)/温度/水流/水量，"
            "低 16 位 0xFFFF 表示该参数不修改。单键/多键由传输层按设备代际自动选择。"
        ),
    )


plugin: CategoryPlugin = register_plugin(build_plugin())

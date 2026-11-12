"""002 燃气壁挂炉 · 品类插件。

002 的语义与值编码留在 `features_002.py`，spec 表在 `categories/002/specs.py`，
本文件只把两者按 `CategoryPlugin` 契约对外暴露并自注册。

支持范围：46 个机型全部开放控制（A 类 16 / B 类 25，含 B1/B2a/B2b/B2c/B3/B4/B5 七个机型子组 / C 类 5）；
TRF 无机型数据，未收录（只读）。
B2c/B3/B4/B5 的特殊差异全部由机型点位表驱动，`control_disabled_models` 已清空。
"""
from __future__ import annotations

from ... import features_002 as f002
from ...api import WanjialeGasBoiler
from ...registry import CategoryPlugin, register_plugin
from . import specs as platform_specs

# 只读机型：当前为空（46 机型全部开放控制），保留空集以维持机制（后续真机白名单可临时回填）。
CONTROL_DISABLED_MODELS = frozenset()

# 只有这些 A 类机型的 write 集合真正包含 247/248，才需要抢操作权。
# 另有 12 个机型只在 read 集合残留 247/248，不能凭 `has_point(247)` 误发三帧。
CONTROL_RIGHTS_MODELS = frozenset({
    "WJL_002_CBC", "WJL_002_FFY", "WJL_002_WLK", "WJL_002_PGS", "WJL_002_YJW",
})


def build_plugin() -> CategoryPlugin:
    """构造 002 的插件对象（由 `plugin = register_plugin(build_plugin())` 注册）。"""
    return CategoryPlugin(
        product=f002.PRODUCT,
        category_cn=f002.CATEGORY,
        device_class=WanjialeGasBoiler,
        platforms=frozenset({
            "water_heater", "climate", "switch", "number", "select",
            "button", "sensor", "binary_sensor", "time",
        }),
        features=f002.FEATURES,
        # 值是 Callable[[device], Tuple[Spec, ...]]：002 的实体名单与 dpid 都随机型变
        specs=platform_specs.PLATFORM_SPECS,
        capability_file="capability_002.json",
        faults_file="faults_002.json",
        parse_state=f002.parse_state,
        encode_value=f002.encode_value,
        # 002 的点位全部随机型表下发，不做全局基线；由 baseline_from_model_points 收集
        base_points=frozenset(),
        baseline_families=frozenset(),
        baseline_from_model_points=True,
        always_on_features=frozenset({"开关机"}),
        # 故障点按协议变体不同（17/117/108/116/151）；实际创建以机型 points.fault.dpid 为准
        fault_points={"device": ("17", "117", "108", "116", "151")},
        # parse_state 已把各协议变体正常值归一化为 0
        fault_none=(0,),
        # 未收录机型不得被认领：002 的点位全在机型表里，没表就没法解析
        requires_model_entry=True,
        # 002 不用 readonly_models；只读限制走 control_disabled_models（仍认领、仍解析）
        readonly_models=frozenset(),
        control_disabled_models=CONTROL_DISABLED_MODELS,
        # A 类抢权白名单（仅 write 集合含 247/248 的 5 个机型）
        control_rights=True,
        control_rights_models=CONTROL_RIGHTS_MODELS,
        encoding_note=(
            "A 类命令字+复合包；B 类/C 类单点直写；点位全部来自 capability_002.json。"
            "同一 dpid 在不同协议变体/机型语义互斥，禁止跨机型复用点位表。"
        ),
    )


plugin: CategoryPlugin = register_plugin(build_plugin())

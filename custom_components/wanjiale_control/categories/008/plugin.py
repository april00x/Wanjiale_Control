"""008 电热水器 · 品类插件。

008 的语义与值编码留在 `features_008.py`，spec 表在 `categories/008/specs.py`，
本文件只把两者按 `CategoryPlugin` 契约对外暴露并自注册。

**未验证项（务必先读）**：008 的所有指令均**未在真实设备上验证**（现有 pcap/日志均为 001），
本轮以「只做开关 / 温度 / 模式 / 故障走 `fids`」控制风险；`points.status` 因无 CSV 证据
一律留空（不出加热/保温二元实体），详见《实施方案_接入008电热水器.md》§2.5/§八。
"""
from __future__ import annotations

from ... import features_008 as f008
from ...api import WanjialeElectricWaterHeater
from ...registry import CategoryPlugin, register_plugin
from . import specs as platform_specs

# 高危 / 存疑机型（§2.4）：已收录进机型表，但主动判为只读 —— 命中即不认领，
# 回落通用只读设备（只出 1 个「原始状态」实体、`_send()` 拒绝下发）。
# 与 C5（`requires_model_entry`）互补：后者拦「根本没进机型表」，
# 本集合拦「已进表但主动判高危」。
#   原生 6：D03/D04/D05/D07/D08/TRM（走 config.xml 的 <relation> 路径，需独立取证）
#   存疑 6：CKQ（新旧编号混用）、TOU/TTH（页面自相矛盾）、AVX/GOX（疑似空气能）、DXV（遥控器配件）
#   温度落 102 的 9 个：与模式点共用 dpid，静态无法判定设备侧语义
READONLY_MODELS = frozenset({
    "WJL_008_CKQ", "WJL_008_TOU", "WJL_008_TTH", "WJL_008_AVX", "WJL_008_GOX", "WJL_008_DXV",
    "WJL_008_D03", "WJL_008_D04", "WJL_008_D05", "WJL_008_D07", "WJL_008_D08", "WJL_008_TRM",
    "WJL_008_DJV", "WJL_008_GGE", "WJL_008_LKA", "WJL_008_MFU", "WJL_008_PZH",
    "WJL_008_RBD", "WJL_008_TKJ", "WJL_008_WTI", "WJL_008_ZWD",
})


def build_plugin() -> CategoryPlugin:
    """构造 008 的插件对象（由 `plugin = register_plugin(build_plugin())` 注册）。"""
    return CategoryPlugin(
        product=f008.PRODUCT,
        category_cn=f008.CATEGORY,
        device_class=WanjialeElectricWaterHeater,
        platforms=frozenset({"water_heater", "switch", "sensor", "binary_sensor"}),
        features=f008.FEATURES,
        # 值是 Callable[[device], Tuple[Spec, ...]]：008 的实体名单与 dpid 都随机型变
        specs=platform_specs.PLATFORM_SPECS,
        capability_file="capability_008.json",
        faults_file="faults_008.json",
        parse_state=f008.parse_state,
        encode_value=f008.encode_value,
        # 008 点位全在机型表里，不做全局基线（effective_points 只含实测点位）
        base_points=frozenset(),
        baseline_families=frozenset(),
        always_on_features=frozenset({"开关机"}),
        # 故障只声明云端来源（C4）：设备故障点由机型 points.fault 决定，
        # 不得用全局 device 元组（老编号 8/9 是热水量/水温，不是故障点）
        fault_points={"cloud": "fids"},
        # fids 里的元素就是故障表 id；0/255 是设备故障点的正常哨兵
        fault_none=(0, 255),
        # 未收录机型不得被认领（C5）：008 的点位全在机型表里，没表就没法控
        requires_model_entry=True,
        readonly_models=READONLY_MODELS,
        # 008 的操作权是老编号 14/15、新编号 120，与 001/002 的 247/248 不同构 →
        # 本轮不做操作权抢占，登记为后续项（§九-3）
        control_rights=False,
        encoding_note=(
            "单点直写（opt(dvid, 绝对值)），无成对复合包；"
            "开关/模式/温度/热水量/水温/故障与辅助开关点位全部随机型变，见 capability_008.json。"
        ),
    )


plugin: CategoryPlugin = register_plugin(build_plugin())

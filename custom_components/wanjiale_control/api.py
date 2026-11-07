"""万家乐设备抽象与控制 API 层。

分层：
  registry.py     品类注册表（product → CategoryPlugin）
  categories/     各品类插件（语义、点位、值编码、数据文件）
  protocol.py     传输层（TCP 帧 / AES / 长连接 / 局域网）
  features_001.py 001 品类协议字典（dvid 语义、命令编码、位域、故障码）
  capabilities.py 能力解析（按品类取表 + 运行时探测 + 选项覆盖）
  api.py          设备对象层（本文件）：把上面几者组装成可控制的设备

WanjialeDevice 基类负责设备元信息、状态缓存、能力门控与传输回退；品类特有的控制方法
放在子类里，并在方法首行调用 _ensure(feature) 做门控。设备分类只按 product 查品类
注册表，新增品类时本文件无需改动。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional, Set, Type

from . import features_001 as f001
from . import features_008 as f008
from . import registry
from .capabilities import (
    DeviceCapabilities,
    effective_points,
    ensure_plugins,
    feature_fault_exempt,
    feature_requires_power,
    observed_points,
    preload_faults,
    resolve,
    resolve_features,
)
from .protocol import LOCAL_PORT, LocalTransportUnsupported, WanjialeProtocol, normalize_fids
from .registry import CategoryPlugin

_LOGGER = logging.getLogger(__name__)


def _short_did(did: Any) -> str:
    """日志用的设备标识：只留末 4 位。

    日志常被用户整段贴出来，完整 did 属设备标识，不宜外传。
    """
    text = str(did or "")
    return f"***{text[-4:]}" if len(text) > 4 else "***"


# ======================================================================
# 跨品类系统级常量
# ----------------------------------------------------------------------
# 多端操作权（247/248）是 001 / 002 共用的跨品类机制，
# 必须由核心层实现、不能留在某个品类的子类里，因此点位于此处定义。
# 注：features_001 的字典里也有同名常量（值相同），那是 001 侧的命令编码字典。
# ======================================================================
DVID_CONTROL_RIGHT_SET = "247"  # 设置操作权（值为账号手机号；0=释放）
DVID_CONTROL_RIGHT_REQ = "248"  # 申请操作权（值为账号手机号；0=放弃）

# ======================================================================
# 异常
# ======================================================================
class WanjialeControlError(Exception):
    """控制被拒绝：机型不支持 / 设备待机 / 设备故障 / 下发失败。

    实体层捕获后转成 HomeAssistantError，把原因呈现给用户。
    """


# ======================================================================
# 品类归属与设备分类
# ======================================================================
def _resolve_plugin(raw: Dict[str, Any], caps: DeviceCapabilities) -> Optional[CategoryPlugin]:
    """决定设备归属哪个品类插件，返回 None 表示本集成不认领。

    判据依次为：设备记录的 `product` 命中注册表；否则若能力表已收录该设备
    （`caps.matched`）则用认领它的品类（`caps.product`）；若命中品类把该机型声明为
    只读（`plugin.readonly_models` 含 `caps.key`）则返回 `None`，这类机型确属本产品码，
    但活动协议是另一路体系（001 的 OYY/HPZ 属 101+ 第二路体系），按本品位下发会误控；
    若命中品类的 `requires_model_entry` 为 True 而设备未命中机型表
    （`caps.model_points` 为空）也返回 `None`，避免用空点位建出可下发的主实体；
    都不命中返回 `None`（未注册品类）。

    返回 `None` 时，`_resolve_device_class` 回落通用只读基类，平台侧只出
    1 个原始状态诊断实体，控制一律拒绝。
    """
    ensure_plugins()

    plugin = registry.by_product(str(raw.get("product") or "").strip())
    if plugin is None and caps.matched:
        plugin = registry.by_product(caps.product)
    if plugin is None:
        return None
    if caps.key and caps.key in plugin.readonly_models:
        _LOGGER.info(
            "机型 %s 属品类 %s 声明的只读机型，按通用只读设备处理（不认领）",
            caps.key, plugin.product,
        )
        return None
    if plugin.requires_model_entry and not caps.model_points:
        # 点位全在机型表里的品类（如 008）：没命中机型表就不认领，否则会用空点位
        # 建出「可开机」的主实体并允许下发。日志对齐 readonly_models 拦截路径，
        # 便于排查「设备在线却只读」。
        _LOGGER.info(
            "设备 firm=%s model=%s modelId=%s 未命中品类 %s 的机型表"
            "（requires_model_entry），按通用只读设备处理（不认领）",
            raw.get("firm"), raw.get("model"), raw.get("modelId"), plugin.product,
        )
        return None
    return plugin


def _resolve_device_class(raw: Dict[str, Any], caps: DeviceCapabilities) -> Type["WanjialeDevice"]:
    """设备类 = 归属品类的 `device_class`；未注册品类 → 通用只读 `WanjialeDevice`。

    不做任何猜测：不按设备名/型号关键字、也不按 AS 特征点位判定品类，
    否则会把非燃热设备误判为燃气热水器并据此下发 001 指令。
    """
    plugin = _resolve_plugin(raw, caps)
    return plugin.device_class if plugin is not None else WanjialeDevice


# ======================================================================
# 轮询耗时预算
# ----------------------------------------------------------------------
# 全账号共用一条云端长连接且顺序 query，若不压缩单台超时，设备数 ≥4 时
# 单轮耗时会超过 DEFAULT_SCAN_INTERVAL（10s），表现为"状态不更新"。
# 单台最坏情况是「首选通道超时 -> 回退另一通道再超时」两次，故一轮上限 ≈ 2×预算。
# ======================================================================
REFRESH_TOTAL_BUDGET_SECONDS = 4.0
MIN_DEVICE_QUERY_TIMEOUT = 0.5
MAX_DEVICE_QUERY_TIMEOUT = 3.0

# 局域网查询失败后的退避窗口（秒）。
# 查询改「局域网优先」后，若某台设备的局域网持续不可用（设备换 IP、AP 隔离、
# 设备离线），每轮都重试会把该轮预算全花在一次注定失败的连接上。窗口内直接走云端，
# 窗口结束自动恢复尝试，不需要用户干预，也不会因一次抖动永久放弃局域网。
LAN_RETRY_BACKOFF_SECONDS = 300.0


def device_query_timeout(device_count: int) -> float:
    """按在线设备数分配单台单次查询超时，保证一轮刷新不超出轮询间隔。

    响应通常不到 1s，单设备时用最大值 3.0s：1 台 → 3.0，2 台 → 2.0，4 台 → 1.0，
    ≥8 台 → 0.5；最坏一轮（每台都超时后回退另一通道）不超过 2×4=8s < 10s（≤8 台）。
    """
    if device_count <= 0:
        return MAX_DEVICE_QUERY_TIMEOUT
    per_device = REFRESH_TOTAL_BUDGET_SECONDS / device_count
    return max(MIN_DEVICE_QUERY_TIMEOUT, min(MAX_DEVICE_QUERY_TIMEOUT, per_device))


# ======================================================================
# 基类
# ======================================================================
class WanjialeDevice:
    """任意万家乐设备的基类。"""

    platform = "sensor"
    category_cn = "通用设备"

    # 控制后多少秒内不接受设备状态回写（避免回包滞后导致 UI 回跳）
    CONTROL_COOLDOWN = 2.0

    def __init__(self, protocol: WanjialeProtocol, raw_device: Dict[str, Any]) -> None:
        self._protocol = protocol
        self._raw = raw_device

        self.did: str = str(raw_device.get("did") or "")
        self.name: str = str(raw_device.get("name") or self.did)
        self.model: str = str(raw_device.get("model") or "")
        self.model_id: str = str(raw_device.get("modelId") or "")
        self.product: str = str(raw_device.get("product") or "")
        self.firm: str = str(raw_device.get("firm") or "")
        self.ver: str = str(raw_device.get("ver") or "")
        self.online: bool = bool(raw_device.get("online"))

        # 局域网控制参数
        self.local_host: Optional[str] = raw_device.get("lanIp")
        self.local_port: int = raw_device.get("lanPort", 0)
        self.lan_pin: str = str(raw_device.get("lanPin") or "")

        # 状态缓存：state 为合并后的 as 字典
        # （设备可能只增量上报部分点位，必须累加而不是覆盖）
        self.attributes: Dict[str, Any] = dict(raw_device)
        self.state: Dict[str, Any] = {}
        self.parsed: Dict[str, Any] = {}

        # 云端故障列表（传输层已归一化为字符串数组，见 protocol.normalize_fids）
        self.fids: List[str] = []
        # 最近一次完整业务响应（供插件读取 acts / ota 等非点位字段）
        self.last_response: Optional[Dict[str, Any]] = None

        # 能力
        self.caps: DeviceCapabilities = DeviceCapabilities()
        self.features: Dict[str, str] = {}
        self.points: Set[str] = set()

        # 品类归属（懒解析，见 plugin 属性）
        self._plugin: Optional[CategoryPlugin] = None
        self._plugin_ready: bool = False

        # 老设备在局域网下必须发单键报文。该标志来自连接层
        # （云端设备记录里没有），故这里默认 False，
        # 且不做探测：把鉴权失败当成老设备，会与 lanPin 错误混淆，
        # 误判会导致绕过局域网鉴权。将来由品类插件或选项页显式置位。
        self.is_old: bool = False

        # 多端操作权（默认关闭，由选项页开启）
        self.control_rights: bool = False
        self.login_name: str = ""

        self._last_seen_online: float = time.time() if self.online else 0.0
        self._last_control_time: float = 0.0
        self._optimistic: Dict[str, Any] = {}
        # 状态查询的“首次失败”标记：设备长期不可达时只在进入失败态时记录一次，
        # 恢复时清空（否则每轮轮询都会重复）。
        self._query_failed_logged: bool = False

    # ------------------------------------------------------------------
    # 品类归属
    # ------------------------------------------------------------------
    @property
    def plugin(self) -> Optional[CategoryPlugin]:
        """本设备所属的品类插件；未注册品类返回 None。

        首次访问时按 `product`（或能力表认领的品类）解析一次并缓存，
        因此设备对象自洽，无需外部注入；判据与 `_resolve_device_class` 同源。
        """
        if not self._plugin_ready:
            self._plugin_ready = True
            self._plugin = _resolve_plugin(self._raw, self.caps)
        return self._plugin

    @property
    def profile(self) -> Dict[str, Any]:
        """机型档案，随状态一起交给 `plugin.parse_state`。

        008 等品类的点位/模式表是机型级的，必须能按机型查表，
        故这里给出 firm_product_model 主键与机型标识。
        """
        return {
            "product": self.caps.product or self.product,
            "key": self.caps.key,
            "model_id": self.model_id,
            "model": self.model,
            "firm": self.firm,
            "family": self.caps.family,
            "form": self.caps.form,
            "points": self.caps.model_points,
        }

    # ------------------------------------------------------------------
    # 角色视图
    # ------------------------------------------------------------------
    role_suffix: str = ""
    """角色后缀，拼进实体 `unique_id` 以保证「设备 × 角色」各自唯一。

    默认空串 → 既有 001 实体 ID 完全不变；002 的两个温度角色分别用
    `-dhw` / `-heating`（见 roles.py）。
    """

    supports_mode_selection: bool = True
    """该角色是否对外暴露「模式选择」（`WaterHeaterProto` 的成员之一）。

    类级默认 `True` → 未实现该成员的角色行为不变（`isinstance` 判定不因契约新增成员
    而变严）；001 的燃气热水器在子类覆盖为属性，按「机型模板是否定义了模式表」动态给出
    （模板模式表为空 → 不暴露）。
    """

    supports_target_temperature: bool = True
    """该角色是否对外暴露「设定温度」（`WaterHeaterProto` 的成员之一）。

    类级默认 `True` → 未实现该成员的角色行为不变；008 覆盖为属性
    「该机型是否有点位 `points.temp`」。温控点随机型变，缺点位时不暴露温度控件，
    避免出现点了才报「该机型不支持设定温度」的假控件。
    """

    def role_views(self) -> List[Any]:
        """本设备可实现的所有角色；默认设备自身即唯一角色。

        平台模块据此遍历（`for role in dev.role_views()`），
        002 会重写为返回 `[卫浴角色, 供暖角色]`。
        """
        return [self]

    # ------------------------------------------------------------------
    # 能力
    # ------------------------------------------------------------------
    def bind_capabilities(self, caps: DeviceCapabilities) -> None:
        self.caps = caps

    def finalize_features(self, overrides: Optional[Dict[str, str]] = None) -> None:
        """在首次状态刷新后确定功能集合与已知点位。"""
        self.points = effective_points(self.caps, observed_points(self.state))
        self.features = resolve_features(self.caps, observed_points(self.state), overrides)
        _LOGGER.info(
            "设备 %s 支持功能：%s | 已知点位 %d 个 | %s",
            self.name,
            "、".join(self.features) or "无",
            len(self.points),
            self.caps.describe(),
        )

    def has_feature(self, feature: str) -> bool:
        return feature in self.features

    def has_point(self, dvid: str) -> bool:
        return str(dvid) in self.points

    @property
    def controls_enabled(self) -> bool:
        """该机型是否开放控制（只读限制由插件声明）。

        命中 `plugin.control_disabled_models` 的机型仍被认领、仍解析状态、
        只读实体照常创建，只是控制平台跳过该设备且 `_send()` 拒绝下发。
        未认领品类（plugin 为 None）同样视为不可控。
        """
        plugin = self.plugin
        if plugin is None:
            return False
        key = self.caps.key
        return not key or key not in plugin.control_disabled_models

    def unique_id(self) -> str:
        return f"wanjiale-{self.did}"

    def is_lan_available(self) -> bool:
        return (
            self.local_host is not None
            and self.local_port > 0
            and len(self.lan_pin) > 0
        )

    # ------------------------------------------------------------------
    # 状态
    # ------------------------------------------------------------------
    def _parse_state(self) -> None:
        """委托品类插件解析状态。

        插件缺失（未注册品类）或解析抛错时降级为空字典，不让异常穿透：
        单品类失败不拖垮全局，实体显示 unknown 而非整体不可用。
        """
        plugin = self.plugin
        if plugin is None:
            self.parsed = {}
            return
        try:
            # 插件不得修改传入的 state（契约约定：parse_state 只读）
            self.parsed = plugin.parse_state(self.state, self.fids, self.profile)
        except Exception:  # noqa: BLE001
            _LOGGER.warning("设备 %s 状态解析失败，本次降级为空", self.name, exc_info=True)
            self.parsed = {}

    def refresh(self) -> None:
        """合并最新上报并重新解析状态。"""
        self.attributes.update(self._raw)
        as_data = self.attributes.get("as", {})
        if isinstance(as_data, dict) and as_data:
            self.state.update({str(k): v for k, v in as_data.items()})

        if time.time() - self._last_control_time < self.CONTROL_COOLDOWN:
            self.state.update(self._optimistic)
        else:
            self._optimistic.clear()

        self._parse_state()

    def apply_state(
        self,
        as_data: Dict[str, Any],
        fids: Any = None,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        """一次性接收状态与故障。

        - `as_data`：本次上报的点位字典；
        - `fids`：云端故障列表（传输层已把老设备的逗号分隔字符串归一化为数组）；
        - `raw`：完整业务响应，存入 `last_response` 供插件读取 `acts` / `ota` 等字段。
          不会覆盖 `_raw`（那是设备档案，含名称/局域网参数等）。
        """
        if isinstance(raw, dict):
            self.last_response = raw
        if isinstance(as_data, dict) and as_data:
            self._raw["as"] = as_data
            self.state.update({str(k): v for k, v in as_data.items()})
            self._last_seen_online = time.time()
        self.fids = normalize_fids(fids)
        self.refresh()

    def optimistic(self, values: Dict[str, Any]) -> None:
        """控制成功后立即生效的预期值，冷却期内优先于设备回包。"""
        values = {str(k): v for k, v in values.items()}
        self.state.update(values)
        self._optimistic.update(values)
        self._last_control_time = time.time()
        self._last_seen_online = time.time()
        self._parse_state()

    # ------------------------------------------------------------------
    # 门控
    # ------------------------------------------------------------------
    def _require_plugin(self) -> CategoryPlugin:
        """取品类插件；未收录品类直接拒绝控制（宁可失败，不做误写）。"""
        plugin = self.plugin
        if plugin is None:
            raise WanjialeControlError(f"设备「{self.name}」所属品类未收录，暂不支持控制")
        return plugin

    def _ensure(self, feature: str) -> None:
        """功能可用性 + 设备可操作性检查。

        门控判据全部取自品类插件（`fault_none` 与功能元数据），
        核心层不写死任何品类的故障值或功能名。
        """
        plugin = self._require_plugin()
        product = plugin.product
        if feature and not self.has_feature(feature):
            raise WanjialeControlError(f"该机型不支持「{feature}」")
        fault = self.parsed.get("fault")
        if (
            fault is not None
            and fault not in plugin.fault_none
            and not feature_fault_exempt(feature, product)
        ):
            text = self.parsed.get("fault_text") or str(fault)
            raise WanjialeControlError(f"设备故障 {text}，暂不支持操作")
        if feature_requires_power(feature, product) and self.parsed.get("power_on") is False:
            raise WanjialeControlError("设备处于待机状态，请先开机")

    # ------------------------------------------------------------------
    # 传输（局域网优先，失败回退云端）
    # ------------------------------------------------------------------
    def _send(self, as_dict: Dict[str, Any]) -> None:
        """下发控制命令；失败抛 WanjialeControlError。

        首行校验品类已收录：未收录品类一律不下发。随后执行控制前置动作（如抢占操作权）。
        失败语义分两类：插件的契约违反（声明 `control_rights=True` 却未实现
        `encode_value("control_right")`）在 `_before_control` 内整体放弃抢权，
        不发半套帧，不影响本次控制；抢权帧的传输失败（`WanjialeControlError`）按原样向上
        抛出、中止本次控制：抢权与正式命令走同一条传输，传输坏了正式命令同样发不出去。
        """
        self._require_plugin()
        if not self.controls_enabled:
            raise WanjialeControlError("该机型未开放控制")
        try:
            self._before_control()
        except WanjialeControlError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.warning("控制前置动作异常（已忽略，继续下发本次控制）", exc_info=True)
        self._dispatch(as_dict)

    def _before_control(self) -> None:
        """控制前置动作：抢占多端操作权（247/248）。

        操作权是跨品类系统级机制（目前 001 / 002 在用），实现放基类，由
        `plugin.control_rights` 决定该品类是否使用它。

        仅当同时满足以下条件才发送（避免每次控制都多发 3 帧）：
          - 品类声明使用操作权，且选项页已开启；
          - 登录名已知；
          - 设备上报过 247 点位（说明该机型使用操作权机制）；
          - 机型在 `plugin.control_rights_models` 白名单内（白名单为空则不限制）；
          - 247 当前值不是本账号手机号（未持有）；
          - 优先权允许抢权：白名单命中时要求 `priority` 是整数且非 0；
            白名单为空（001/008）维持旧判据「`priority == 0` 才拦」。
        """
        plugin = self._require_plugin()
        if not plugin.control_rights or not self.control_rights or not self.login_name:
            return
        if not self.has_point(DVID_CONTROL_RIGHT_SET):
            return
        whitelist = plugin.control_rights_models
        if whitelist and self.caps.key not in whitelist:
            return
        if str(self.state.get(DVID_CONTROL_RIGHT_SET) or "") == self.login_name:
            return
        priority = self.parsed.get("priority")
        if whitelist:
            # 严格判据：None/非整数一律不抢，避免设备未上报优先权时误发三帧
            if not isinstance(priority, int) or priority == 0:
                return
        elif priority == 0:
            return
        # 第一帧由品类插件给出（001 是「命令类型 24 + 开关值 1」的成对命令）。
        # 声明 control_rights=True 即意味着必须实现该动作；未实现则整体放弃抢权
        # （宁可不抢，也不发半套帧）。points 透传让 002 可按方言决定是否支持。
        try:
            request_frame = plugin.encode_value(
                "control_right", points=self.caps.model_points or {}
            )
        except Exception:  # noqa: BLE001
            _LOGGER.warning(
                "品类 %s 声明使用操作权（control_rights=True）但未实现 "
                "encode_value('control_right')，本次不发送抢权帧",
                plugin.product, exc_info=True,
            )
            return
        self._dispatch(request_frame)
        self._dispatch({DVID_CONTROL_RIGHT_REQ: self.login_name})
        self._dispatch({DVID_CONTROL_RIGHT_SET: self.login_name})

    def _dispatch(self, as_dict: Dict[str, Any]) -> None:
        """真正把命令发出去：局域网优先，失败回退云端。"""
        as_str = {str(k): str(v) for k, v in as_dict.items()}
        if self.is_lan_available():
            self._send_lan(as_str)
            return
        self._send_cloud(as_str)

    def _send_opt(self, dvid: str, value: Any) -> None:
        self._send({dvid: value})

    def _send_opt_pair(self, op_type: int, value: int) -> None:
        self._send({f001.DVID_OP_TYPE: op_type, f001.DVID_OP_VALUE: value})

    def _send_cloud(self, as_str: Dict[str, str]) -> None:
        try:
            if not getattr(self._protocol, "_socket", None):
                self._protocol.connect_server()
        except Exception as err:  # noqa: BLE001
            raise WanjialeControlError(f"云端连接不可用：{err}") from err
        try:
            self._protocol.send_control_async(self.did, as_str)
        except Exception as err:  # noqa: BLE001
            raise WanjialeControlError(f"云端下发失败：{err}") from err

    def _send_lan(self, as_str: Dict[str, str]) -> None:
        """局域网下发；局域网不可用时回退云端。

        回退放在 `_try_send_lan` 之外：云端失败必须按原样抛出，不能被下面的
        异常分支当成「局域网失败」再发一次。
        """
        if not self._try_send_lan(as_str):
            self._send_cloud(as_str)

    def _try_send_lan(self, as_str: Dict[str, str]) -> bool:
        """尝试局域网下发；返回是否已发出（失败只记日志，由调用方回退云端）。"""
        try:
            # 连接按 did 维护：只检查/建立这台设备自己的链路。
            # 协议层还会断言「链路所属设备 == 目标设备」，不一致直接拒绝。
            if not self._protocol.is_local_connected(self.did):
                if not self._protocol.connect_local(
                    self.did, self.local_host, self.local_port, self.lan_pin
                ):
                    _LOGGER.warning("局域网认证失败，本次控制回退云端")
                    return False
            self._protocol.send_local_control(self.did, as_str, old_device=self.is_old)
            return True
        except LocalTransportUnsupported as err:
            # 老设备 + 局域网只支持单键；多键改走云端（云端不受该限制），
            # 而不是像客户端那样静默丢弃后续键。属协议限制，不是故障。
            _LOGGER.info("局域网不支持本次报文（%s），改用云端下发", err)
            return False
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("局域网控制失败，本次控制回退云端：%s", err, exc_info=True)
            self._protocol.close_local(self.did)  # 只关这台，不影响其它设备
            return False

    def turn_on(self) -> None:
        raise NotImplementedError

    def turn_off(self) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} did={self.did} name={self.name!r} online={self.online}>"


# ======================================================================
# 燃气热水器（001 产品线）
# ======================================================================
class WanjialeGasWaterHeater(WanjialeDevice):
    """万家乐燃气热水器（001 产品线）。

    控制命令一律走 features_001 提供的编码函数；状态一律走 features_001.parse_state，
    本类不出现裸 dvid 与魔法数字。
    """

    platform = "water_heater"
    category_cn = "燃气热水器"

    # ---- 状态读取（全部来自已解析的 parsed 字典） ----
    @property
    def is_power_on(self) -> Optional[bool]:
        return self.parsed.get("power_on")

    @property
    def current_temperature(self) -> Optional[int]:
        """当前水温（dvid18）。"""
        return self.parsed.get("current_temp")

    @property
    def target_temperature(self) -> Optional[int]:
        """设定温度（对外显示值：随温感模式下已做环境修正）。"""
        return self.parsed.get("target_temp")

    @property
    def target_temperature_raw(self) -> Optional[int]:
        return self.parsed.get("target_temp_raw")

    @property
    def current_mode(self) -> Optional[int]:
        return self.parsed.get("mode")

    @property
    def fault_code(self) -> Optional[int]:
        return self.parsed.get("fault")

    @property
    def is_heating(self) -> Optional[bool]:
        return self.parsed.get("heating")

    @property
    def env_offset(self) -> int:
        return int(self.parsed.get("env_offset") or 0)

    def min_temp(self, mode: Optional[int] = None) -> int:
        m = self.current_mode if mode is None else mode
        return f001.temp_limits(m, self.env_offset)[0]

    def max_temp(self, mode: Optional[int] = None) -> int:
        m = self.current_mode if mode is None else mode
        return f001.temp_limits(m, self.env_offset)[1]

    def mode_names(self) -> Dict[int, str]:
        """模式号 → 名称：模板模式表 ∪ 运行时实际上报过的模式。"""
        modes = dict(self.caps.template_modes)
        current = self.current_mode
        if current is not None and current not in modes:
            modes[current] = f001.MODE_NAMES.get(current, f"模式 {current}")
        return modes

    @property
    def supports_mode_selection(self) -> bool:
        """是否对外暴露「模式选择」：仅当机型模板确实定义了模式表。

        模板模式表为空时，`mode_names()` 最多只能给出实机当前那 1 项，下拉框是
        「看起来能选、实际没得选」的假控件，故不暴露 `OPERATION_MODE`。
        温度控制仍由「模式切换」功能门控，不受本属性影响。
        """
        return bool(self.caps.template_modes)

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def _current_flow_volume(self) -> tuple[Optional[int], Optional[int]]:
        """沿用设备当前的设定水流 / 水量。

        未知时返回 None，由编码器写成 0xFFFF（不修改），避免误改设备参数。
        """
        return self.parsed.get("target_flow"), self.parsed.get("target_volume")

    def set_power(self, on: bool) -> None:
        self._ensure("开关机")
        self._send_opt(f001.DVID_POWER, 1 if on else 0)
        self.optimistic({f001.DVID_POWER: 1 if on else 0})

    def turn_on(self) -> None:
        self.set_power(True)

    def turn_off(self) -> None:
        self.set_power(False)

    def set_temperature(self, temperature: int) -> None:
        """设置温度（入参为对外显示温度，内部按模式反算成原始值下发）。"""
        self._ensure("模式切换")
        mode = self.current_mode if self.current_mode is not None else f001.MODE_COMFORT
        low, high = f001.temp_limits(mode, self.env_offset)
        display = max(low, min(high, int(temperature)))
        raw = f001.raw_temp(display, mode, self.env_offset)
        flow, volume = self._current_flow_volume()
        value = f001.pack_mode_temp(mode, raw, flow, volume)
        self._send_opt_pair(f001.OP_MODE_TEMP, value)
        self.optimistic({f001.DVID_TARGET_TEMP: raw, f001.DVID_MODE: mode})

    def set_mode(self, mode: int) -> None:
        """切换模式。

        只改模式，温度沿用设备当前值，不做钳位。
        """
        self._ensure("模式切换")
        raw = self.target_temperature_raw
        flow, volume = self._current_flow_volume()
        value = f001.pack_mode_temp(mode, raw, flow, volume)
        self._send_opt_pair(f001.OP_MODE_TEMP, value)
        self.optimistic({f001.DVID_MODE: mode})

    def set_instant_heat(self, on: bool) -> None:
        """零冷水 / 即热开关（dvid1=14，byte3 为 1/0，低 16 位不修改）。"""
        self._ensure("零冷水/即热")
        self._send_opt_pair(f001.OP_INSTANT_HEAT, f001.pack_toggle(1 if on else 0))

    def set_reserve(self, on: bool) -> None:
        """预约 / 定时总开关（dvid1=15）。"""
        self._ensure("预约")
        self._send_opt_pair(f001.OP_RESERVE, f001.pack_toggle(1 if on else 0))

    def set_boost(self, on: bool) -> None:
        """增压（dvid1=7）。"""
        self._ensure("增压")
        self._send_opt_pair(f001.OP_BOOST, f001.pack_toggle(1 if on else 0))
        self.optimistic({f001.DVID_BOOST: 1 if on else 0})

    def set_all_day(self, on: bool) -> None:
        """全天循环（dvid1=27，byte3=4 为开）。"""
        self._ensure("全天循环")
        self._send_opt_pair(f001.OP_ALL_DAY, f001.pack_toggle(4 if on else 0))
        self.optimistic({f001.DVID_ALL_DAY: 4 if on else 0})

    def set_uv(self, on: bool) -> None:
        """UV 杀菌（dvid1=6）。"""
        self._ensure("UV杀菌")
        self._send_opt_pair(f001.OP_UV, f001.pack_toggle(1 if on else 0))

    def set_motion(self, on: bool) -> None:
        """水控/点动开关（dvid1=17，byte3 为 1/0）。

        设备把该命令当作「水控使能」的开关，而不是一次性动作：开启后只要有水流
        就会自动触发一次即热。因此必须同时提供关闭路径（byte3=0），否则一旦打开
        就只能靠官方客户端或设备复位才能退出。
        """
        self._ensure("点动循环")
        self._send_opt_pair(f001.OP_MOTION, f001.pack_toggle(1 if on else 0))
        # 同其它开关：先乐观更新 dvid69 的水控位，冷却期内不被滞后回包拉回
        current = int(self.state.get(f001.DVID_MOTION) or 0)
        self.optimistic({
            f001.DVID_MOTION:
                (current | f001.BIT_MOTION) if on else (current & ~f001.BIT_MOTION)
        })

    def set_sterilize(self, on: bool) -> None:
        """巡航杀菌（dvid251 直接写值）。"""
        self._ensure("巡航杀菌")
        self._send_opt(f001.DVID_STERILIZE, 1 if on else 0)
        self.optimistic({f001.DVID_STERILIZE: 1 if on else 0})

    def set_keep_warm(self, level: int) -> None:
        """保温时长档位（dvid1=10，byte3 为档位）。"""
        self._ensure("保温时长")
        self._send_opt_pair(f001.OP_KEEP_WARM, f001.pack_level(int(level)))
        self.optimistic({f001.DVID_KEEP_WARM: int(level)})

    def set_diff_temp(self, value: int) -> None:
        """回差温度（dvid1=11，byte3 为温度值）。"""
        self._ensure("回差温度")
        self._send_opt_pair(f001.OP_DIFF_TEMP, f001.pack_level(int(value)))
        self.optimistic({f001.DVID_DIFF_TEMP: int(value)})

    def set_circulation(self, level: int) -> None:
        """即热循环时长档位（dvid1=26，byte3 为档位）。"""
        self._ensure("循环时长")
        self._send_opt_pair(f001.OP_CIRCULATION, f001.pack_level(int(level)))
        self.optimistic({f001.DVID_CIRCULATION: int(level)})

    def set_kitchen_timer(self, slot: int) -> None:
        """厨房洗定时档位（dvid1=29）。"""
        self._ensure("厨房洗/定时")
        self._send_opt_pair(f001.OP_KITCHEN, f001.pack_kitchen_timer(int(slot)))
        self.optimistic({f001.DVID_KITCHEN_TIMER: int(slot)})

    def set_sparkling(self, on: bool) -> None:
        """冷气泡水（dvid1=29，byte3 为 1/0）。

        设备在标准开关值上额外 +11（KITCHEN_SPARKLING_OFFSET），
        该偏移语义未解码，按固定值下发，不做推导。
        """
        self._ensure("冷气泡水")
        value = f001.pack_toggle(1 if on else 0) + f001.KITCHEN_SPARKLING_OFFSET
        self._send_opt_pair(f001.OP_KITCHEN, value)

    def set_bath_volume(self, volume: int) -> None:
        """浴缸注水量（dvid1=4 的 byte0，单位 10L）。"""
        self._ensure("浴缸注水量")
        mode = self.current_mode if self.current_mode is not None else f001.MODE_COMFORT
        raw = self.target_temperature_raw
        if raw is None:
            raw = f001.raw_temp(
                f001.temp_limits(mode, self.env_offset)[0], mode, self.env_offset
            )
        flow, _ = self._current_flow_volume()
        value = f001.pack_mode_temp(mode, raw, flow, int(volume))
        self._send_opt_pair(f001.OP_MODE_TEMP, value)
        self.optimistic({f001.DVID_TARGET_VOLUME: int(volume)})

    def reset_water_total(self) -> None:
        """累计用水量清零（dvid1=5）。"""
        self._ensure("水量/气量清零")
        self._send_opt_pair(f001.OP_RESET_TOTAL, f001.RESET_VALUE_WATER)

    def reset_gas_total(self) -> None:
        """累计用气量清零（dvid1=5）。"""
        self._ensure("水量/气量清零")
        self._send_opt_pair(f001.OP_RESET_TOTAL, f001.RESET_VALUE_GAS)

    def clear_reserve_conflict(self) -> None:
        """清除预约时间重叠错误码（dvid245=0）。"""
        self._send_opt(f001.DVID_RESERVE_CONFLICT, 0)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_status(self) -> Dict[str, Any]:
        return self._protocol.query_device(self.did)


# ======================================================================
# 电热水器（008 产品线）
# ======================================================================
class WanjialeElectricWaterHeater(WanjialeDevice):
    """万家乐电热水器（008 产品线）。

    点位全部取自 `self.caps.model_points`（机型级，由 `capability_008.json` 的
    `devices[*].points` 透传），本类不出现任何裸 dvid：
    控制一律经 `plugin.encode_value()`，状态一律来自 `features_008.parse_state()`。

    与 001 的差异：008 无成对复合包（单点直写绝对值）；模式表直接取机型能力表；
    温度点独立于模式点，故 `set_temperature` 按「机型是否有点位」判定而非「模式切换」。
    """

    platform = "water_heater"
    category_cn = "电热水器"

    # ------------------------------------------------------------------
    # 机型点位视图
    # ------------------------------------------------------------------
    def _points(self) -> Dict[str, Any]:
        return self.caps.model_points or {}

    def _point(self, name: str) -> Dict[str, Any]:
        item = self._points().get(name)
        return item if isinstance(item, dict) else {}

    def _aux_item(self, feature: str) -> Optional[Dict[str, Any]]:
        for item in self._points().get("aux") or []:
            if isinstance(item, dict) and item.get("feature") == feature:
                return item
        return None

    # ------------------------------------------------------------------
    # 状态（属性，`WaterHeaterProto` 契约）
    # ------------------------------------------------------------------
    @property
    def is_power_on(self) -> Optional[bool]:
        return self.parsed.get("power_on")

    @property
    def current_temperature(self) -> Optional[int]:
        """当前水温（机型级 `points.curTemp`）。"""
        return self.parsed.get("current_temp")

    @property
    def target_temperature(self) -> Optional[int]:
        """设定温度（008 的下发值即绝对值 ℃，无需反算）。"""
        return self.parsed.get("target_temp")

    @property
    def current_mode(self) -> Optional[int]:
        return self.parsed.get("mode")

    @property
    def hot_water(self) -> Optional[int]:
        """当前热水量（%）。"""
        return self.parsed.get("hot_water")

    # ------------------------------------------------------------------
    # 元信息
    # ------------------------------------------------------------------
    def mode_names(self) -> Dict[int, str]:
        """模式号 → 名称：直接取机型能力表（无需 001 的「模板 ∪ 实机上报」合并）。"""
        return dict(self.caps.template_modes)

    @property
    def supports_mode_selection(self) -> bool:
        """无模式表则不暴露假下拉框（与 001 一致）。"""
        return bool(self.caps.template_modes)

    @property
    def supports_target_temperature(self) -> bool:
        """该机型是否可设温，由机型点位决定（008 没有独立的「温度」能力位）。

        温控点随机型变，缺点位时不暴露温度控件，避免出现点了才报
        「该机型不支持设定温度」的假控件。
        """
        return bool(self._point("temp"))

    def min_temp(self) -> float:
        return float(self._point("temp").get("min", 30))

    def max_temp(self) -> float:
        return float(self._point("temp").get("max", 75))

    @property
    def fault_code(self) -> Optional[int]:
        return self.parsed.get("fault")

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    def _frame(self, action: str, **params: Any) -> Dict[str, str]:
        """委托品类插件编码（值计算的唯一实现），并补上本机型点位。"""
        plugin = self._require_plugin()
        params.setdefault("points", self._points())
        return plugin.encode_value(action, **params)

    def set_power(self, on: bool) -> None:
        self._ensure("开关机")
        frame = self._frame("power", on=bool(on))
        self._send(frame)
        self.optimistic(frame)

    def turn_on(self) -> None:
        self.set_power(True)

    def turn_off(self) -> None:
        self.set_power(False)

    def set_temperature(self, temperature: int) -> None:
        """下发绝对值温度（按机型 `points.temp.min/max` 钳位）。

        门控用 `_ensure("")`：只校验品类已收录和设备无故障，不做功能门控：
        008 没有独立的「温度」能力位，而 `模式切换` 只表示模式点存在；
        是否可以设温由「该机型是否有点位」决定，避免把两个概念绑在一起。
        """
        if not self._point("temp"):
            raise WanjialeControlError("该机型不支持设定温度")
        self._ensure("")
        frame = self._frame("temperature", value=int(temperature))
        self._send(frame)
        self.optimistic(frame)

    def set_mode(self, mode: int) -> None:
        self._ensure("模式切换")
        frame = self._frame("mode", mode=int(mode))
        self._send(frame)
        self.optimistic(frame)

    def set_aux(self, feature: str, on: bool) -> None:
        """辅助开关：按机型 aux 列表派发（`set_<key>` 方法由下方动态生成）。"""
        item = self._aux_item(feature)
        if item is None:
            raise WanjialeControlError(f"该机型不支持「{feature}」")
        self._ensure(feature)
        value = int(item.get("on", 1) if on else item.get("off", 0))
        frame = self._frame("toggle", dpid=item.get("dpid"), value=value)
        self._send(frame)
        self.optimistic(frame)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_status(self) -> Dict[str, Any]:
        return self._protocol.query_device(self.did)


def _make_aux_setter(feature: str, key: str):
    """生成 `set_<key>(on)`（switch spec 的 `method` 指向它）。"""

    def setter(self: WanjialeElectricWaterHeater, on: bool) -> None:
        self.set_aux(feature, bool(on))

    setter.__name__ = f"set_{key}"
    setter.__qualname__ = f"WanjialeElectricWaterHeater.set_{key}"
    setter.__doc__ = f"辅助开关「{feature}」（机型级 dpid，见能力表 `points.aux`）。"
    return setter


# 按 features_008 的「键 → 规范功能名」表逐个绑定，使 `hasattr(role, "set_<key>")`
# 在 switch 平台判定时成立（平台只调方法、不认品类）。
for _aux_key, _aux_feature in f008.AUX_KEY_FEATURE.items():
    if not hasattr(WanjialeElectricWaterHeater, f"set_{_aux_key}"):
        setattr(WanjialeElectricWaterHeater, f"set_{_aux_key}",
                _make_aux_setter(_aux_feature, _aux_key))


# ======================================================================
# 燃气壁挂炉（002 产品线）
# ======================================================================
class _BoilerDhwRole:
    """卫浴角色：water_heater 主实体 + 卫浴侧辅助开关/数值。

    显式实现方法，不使用 `__getattr__`：平台按 `hasattr(role, method)` 判定实体，
    动态兜底会让所有方法看起来都存在而误建实体。
    """

    role_suffix = "-dhw"
    supports_mode_selection = False
    supports_target_temperature = True

    def __init__(self, dev: "WanjialeGasBoiler") -> None:
        self._dev = dev

    # ---- WaterHeaterProto ----
    @property
    def is_power_on(self) -> Optional[bool]:
        return self._dev.parsed.get("power_on")

    @property
    def current_temperature(self) -> Optional[int]:
        return self._dev.parsed.get("dhw_current_temp")

    @property
    def target_temperature(self) -> Optional[int]:
        return self._dev.parsed.get("dhw_target_temp")

    @property
    def current_mode(self) -> Optional[int]:
        return None

    def mode_names(self) -> Dict[int, str]:
        return {}

    def min_temp(self) -> float:
        return self._dev.dhw_min_temp()

    def max_temp(self) -> float:
        return self._dev.dhw_max_temp()

    def turn_on(self) -> None:
        self._dev.set_power(True)

    def turn_off(self) -> None:
        self._dev.set_power(False)

    def set_temperature(self, temperature: int) -> None:
        self._dev.set_dhw_temperature(temperature)

    def set_mode(self, mode: int) -> None:
        """B5（FFS）的 107 是设备模式（0 舒适/2 周计划/3 防冻）；其余机型卫浴不暴露模式。"""
        pts = self._dev.caps.model_points or {}
        if (pts.get("mode") or {}).get("device_mode"):
            self._dev.set_device_mode(mode)
            return
        raise WanjialeControlError("壁挂炉卫浴不支持模式选择")

    # ---- 卫浴辅助（switch / number / select 平台按方法名命中） ----
    def set_instant(self, on: bool) -> None:
        self._dev.set_instant(on)

    def set_preheat(self, on: bool) -> None:
        self._dev.set_preheat_master(on)

    def set_heat_schedule(self, on: bool) -> None:
        self._dev.set_heat_schedule_master(on)

    def set_preheat_schedule(self, on: bool) -> None:
        self._dev.set_preheat_master(on)

    def set_boost(self, on: bool) -> None:
        self._dev.set_boost(on)

    def set_ai(self, on: bool) -> None:
        self._dev.set_ai(on)

    def set_sterilize(self, on: bool) -> None:
        self._dev.set_sterilize(on)

    def set_keep_warm(self, minutes: int) -> None:
        self._dev.set_keep_warm(minutes)

    def set_diff_temp(self, value: int) -> None:
        self._dev.set_diff_temp(value)

    def set_heat_diff(self, value: int) -> None:
        self._dev.set_heat_diff(value)

    def set_child_lock(self, on: bool) -> None:
        self._dev.set_child_lock(on)

    def set_morning_night(self, mode: int) -> None:
        self._dev.set_morning_night(mode)

    def reset_device(self) -> None:
        self._dev.reset_device()

    def clear_reserve_conflict(self) -> None:
        self._dev.clear_reserve_conflict()

    # ---- 档位表（select 的 options_from） ----
    def keep_warm_options(self) -> Dict[int, str]:
        return self._dev.keep_warm_options()

    def device_mode_names(self) -> Dict[int, str]:
        values = (self._dev.caps.model_points or {}).get("mode", {}).get("values") or {}
        return {int(key): str(name) for key, name in values.items()}


class _BoilerHeatingRole:
    """供暖角色：climate 实体（供暖开关 / 设定温度 / 供暖模式）。"""

    role_suffix = "-heating"
    supports_target_temperature = True

    def __init__(self, dev: "WanjialeGasBoiler") -> None:
        self._dev = dev

    @property
    def is_power_on(self) -> Optional[bool]:
        return self._dev.parsed.get("heating_on")

    @property
    def current_temperature(self) -> Optional[int]:
        return self._dev.parsed.get("heat_current_temp")

    @property
    def target_temperature(self) -> Optional[int]:
        return self._dev.parsed.get("heat_target_temp")

    @property
    def current_hvac_mode(self) -> Optional[int]:
        value = self._dev.parsed.get("heating_on")
        return None if value is None else (1 if value else 0)

    def hvac_modes(self) -> Dict[int, str]:
        return {0: "off", 1: "heat"}

    def heating_mode_names(self) -> Dict[int, str]:
        return dict(self._dev.caps.template_modes)

    def min_temp(self) -> float:
        return self._dev.heat_min_temp()

    def max_temp(self) -> float:
        return self._dev.heat_max_temp()

    def turn_on(self) -> None:
        self._dev.set_heating(True)

    def turn_off(self) -> None:
        self._dev.set_heating(False)

    def set_temperature(self, temperature: int) -> None:
        self._dev.set_heating_temperature(temperature)

    def set_hvac_mode(self, mode: int) -> None:
        self._dev.set_heating(bool(mode))

    def set_heating(self, on: bool) -> None:
        self._dev.set_heating(on)

    def set_heating_mode(self, mode: int) -> None:
        self._dev.set_heating_mode(mode)


class WanjialeGasBoiler(WanjialeDevice):
    """万家乐燃气壁挂炉（002 产品线）。

    点位全部来自 `capability_002.json` 的 `devices[*].points`，取值一律经
    `features_002.parse_state()`，控制一律经 `plugin.encode_value()`，本类不出现裸 dpid。

    一台设备拆成两个角色：卫浴（`-dhw`）与供暖（`-heating`），设备对象本身不实现
    `WaterHeaterProto`/`ClimateProto`，避免同一个 `target_temperature` 服务两个实体。
    组实体（预约开关/温度/时间）由 `device_level=True` 的 spec 直接对设备渲染。

    A 线（dialect=A）走命令字加 32 位复合包，B/C 系为单键直写，均由 `encode_value`
    按机型点位分支处理。
    """

    platform = "water_heater"
    category_cn = "燃气壁挂炉"

    def __init__(self, protocol: WanjialeProtocol, raw_device: Dict[str, Any]) -> None:
        super().__init__(protocol, raw_device)
        # 不能在 __init__ 读 caps：api.load_devices() 是「先构造设备、后 bind_capabilities()」
        self._roles: Optional[List[Any]] = None

    def bind_capabilities(self, caps: DeviceCapabilities) -> None:
        super().bind_capabilities(caps)
        self._roles = None
        # 只有 A 线老设备在局域网下必须发单键报文（两键会 LocalTransportUnsupported
        # 而回退云端）；B/C 误置会把局域网多键全部误判为老设备并回退云端。
        self.is_old = (caps.model_points or {}).get("dialect") == "A"

    def role_views(self) -> List[Any]:
        if self._roles is None:
            pts = self.caps.model_points or {}
            roles: List[Any] = [_BoilerDhwRole(self)]
            # B5（FFS）没有供暖点：只返回卫浴角色，避免 climate 被误建
            if pts.get("heat_set") or pts.get("heating"):
                roles.append(_BoilerHeatingRole(self))
            self._roles = roles
        return list(self._roles)

    # ------------------------------------------------------------------
    # 机型点位视图
    # ------------------------------------------------------------------
    def _points(self) -> Dict[str, Any]:
        return self.caps.model_points or {}

    def _point(self, name: str) -> Dict[str, Any]:
        item = self._points().get(name)
        return item if isinstance(item, dict) else {}

    def _thermostats(self) -> List[Dict[str, Any]]:
        """B4 八路温控器是列表点位，不能走 `_point()`（只接受字典点位组）。"""
        items = self._points().get("thermostats")
        return [item for item in (items or []) if isinstance(item, dict)]

    def _require(self, *names: str) -> Dict[str, Any]:
        """按点位组存在性做最后一道保险（功能位可被选项强制打开，但不能凭空造点位）。"""
        for name in names:
            item = self._point(name)
            if item:
                return item
        raise WanjialeControlError("该机型未配置对应点位，拒绝下发")

    def _frame(self, action: str, **params: Any) -> Dict[str, str]:
        plugin = self._require_plugin()
        params.setdefault("points", self._points())
        try:
            return plugin.encode_value(action, **params)
        except ValueError as err:
            # 平台层只捕获 WanjialeControlError，这里统一转换，避免裸 ValueError
            raise WanjialeControlError(str(err)) from err

    def _commit(self, frame: Dict[str, str], optimistic: bool = True) -> None:
        """下发并更新状态缓存。

        A 线 `{1, 2}` 是命令通道、不是状态点，写进 state 既不会更新目标温度，
        又会在冷却期污染状态缓存；只有 `{4: 0/1}` 这类真实状态点才做乐观更新。
        """
        self._send(frame)
        if self._points().get("dialect") == "A":
            if optimistic and set(frame) == {"4"}:
                self.optimistic(frame)
            else:
                self._mark_control()
            return
        if optimistic:
            self.optimistic(frame)
        else:
            self._mark_control()

    def _mark_control(self) -> None:
        """只更新时间戳，让 CONTROL_COOLDOWN 生效，不把命令帧写进 state。"""
        self._last_control_time = time.time()

    # ------------------------------------------------------------------
    # 温度量程（数据驱动，不写死 30~80/25~60）
    # ------------------------------------------------------------------
    def dhw_min_temp(self) -> float:
        return float(self._point("dhw_set").get("min", 35))

    def dhw_max_temp(self) -> float:
        return float(self._point("dhw_set").get("max", 60))

    def _heat_range(self) -> tuple:
        spec = self._point("heat_set")
        low_floor = float(spec.get("min_floor", spec.get("min", 25)))
        high_floor = float(spec.get("max_floor", spec.get("max", 60)))
        low_radiator = float(spec.get("min_radiator", low_floor))
        high_radiator = float(spec.get("max_radiator", high_floor))

        emitter = self.parsed.get("emitter")
        emitter_pt = self._point("emitter")
        if emitter_pt:
            if emitter is not None and emitter == emitter_pt.get("radiator"):
                return low_radiator, high_radiator
            if emitter is not None and emitter == emitter_pt.get("floor"):
                return low_floor, high_floor
        elif emitter is not None and emitter == self._point("heating").get("range_radiator_value"):
            # 1=散热片，值来自能力表
            return low_radiator, high_radiator
        # 末端未知时取地暖范围（更保守，避免下发超出设备允许的上限）
        return low_floor, high_floor

    def heat_min_temp(self) -> float:
        return float(self._heat_range()[0])

    def heat_max_temp(self) -> float:
        return float(self._heat_range()[1])

    def keep_warm_options(self) -> Dict[int, str]:
        """保温档位表：键为分钟数（select 用分钟作为选项值）。"""
        spec = self._point("keep_warm")
        levels = spec.get("levels") or []
        mapping = spec.get("map") if isinstance(spec.get("map"), dict) else None
        unit = int(spec.get("unit_min") or 1)
        zero_meaning = str(spec.get("zero_meaning") or "unknown")
        zero_label = {"instant_once": "即热一次", "cycle_once": "循环一次"}.get(
            zero_meaning, "0（未知）"
        )
        options: Dict[int, str] = {}
        for level in levels:
            level = int(level)
            if mapping is not None:
                minutes = mapping.get(str(level))
                if minutes is None:
                    continue
                minutes = int(minutes)
            else:
                minutes = level * unit
            options[minutes] = zero_label if minutes == 0 else f"{minutes} 分钟"
        return options

    # ------------------------------------------------------------------
    # 主实体控制
    # ------------------------------------------------------------------
    def set_power(self, on: bool) -> None:
        self._ensure("开关机")
        self._require("power")
        self._commit(self._frame("power", on=bool(on)))

    def turn_on(self) -> None:
        self.set_power(True)

    def turn_off(self) -> None:
        self.set_power(False)

    def set_dhw_temperature(self, temperature: int) -> None:
        self._ensure("卫浴温度")
        spec = self._require("dhw_set")
        low = int(spec.get("min", 35))
        high = int(spec.get("max", 60))
        value = max(low, min(high, int(round(float(temperature)))))
        params: Dict[str, Any] = {}
        if self._points().get("dialect") == "A":
            # A 线 cmd4 的复合包需要沿用当前卫浴模式 / 水流 / 水量（未知时 0xFF 不修改）
            params = {
                "mode": self.parsed.get("dhw_mode"),
                "flow": self.parsed.get("target_flow"),
                "volume": self.parsed.get("target_volume"),
            }
        self._commit(self._frame("dhw_temp", value=value, **params))

    def set_heating_temperature(self, temperature: int) -> None:
        self._ensure("供暖温度")
        self._require("heat_set")
        low, high = self._heat_range()
        value = max(int(low), min(int(high), int(round(float(temperature)))))
        self._commit(self._frame("heat_temp", value=value))

    def set_heating(self, on: bool) -> None:
        if on and self.parsed.get("power_on") is False:
            raise WanjialeControlError("请先开机")
        self._ensure("供暖开关")
        if not (self._point("heating") or self._point("season")):
            raise WanjialeControlError("该机型未配置供暖开关点位，拒绝下发")
        self._commit(self._frame("heating", on=bool(on)))

    def set_heating_mode(self, mode: int) -> None:
        self._ensure("供暖模式")
        self._require("mode")
        self._commit(self._frame("mode", mode=int(mode)))

    def set_device_mode(self, mode: int) -> None:
        """B5（FFS）设备模式（0 舒适/2 周计划/3 防冻）。"""
        spec = self._point("mode")
        if not spec.get("device_mode"):
            raise WanjialeControlError("该机型不支持设备模式")
        self._commit(self._frame("mode", mode=int(mode)))

    # ------------------------------------------------------------------
    # 辅助开关
    # ------------------------------------------------------------------
    def set_instant(self, on: bool) -> None:
        self._ensure("即热")
        self._require("instant_mode", "instant")
        params: Dict[str, Any] = {}
        if self._points().get("dialect") == "A":
            # 部分 A 机型 cmd14 采用「on/off + 温度」形态，温度未知时写 0xFF 不修改
            params["temp"] = self.parsed.get("dhw_target_temp")
        self._commit(self._frame("instant", on=bool(on), **params))

    def set_preheat_master(self, on: bool) -> None:
        self._ensure("热水预热")
        spec = self._point("schedule_dhw")
        if spec.get("total") is None and not spec.get("master_cmd"):
            raise WanjialeControlError("该机型未配置热水预热总开关点位")
        params: Dict[str, Any] = {}
        if self._points().get("dialect") == "A":
            params["temp"] = self.parsed.get("dhw_target_temp")
        self._commit(self._frame("preheat_master", on=bool(on), **params))

    def set_preheat(self, on: bool) -> None:
        self.set_preheat_master(on)

    def set_heat_schedule_master(self, on: bool) -> None:
        self._ensure("预约供暖")
        spec = self._point("schedule_heat")
        if spec.get("total") is None and not spec.get("master_cmd"):
            raise WanjialeControlError("该机型未配置预约供暖总开关点位")
        params: Dict[str, Any] = {}
        if self._points().get("dialect") == "A":
            params["temp"] = self.parsed.get("dhw_target_temp")
        self._commit(self._frame("heat_schedule_master", on=bool(on), **params))

    def set_boost(self, on: bool) -> None:
        self._ensure("增压")
        self._require("boost")
        self._commit(self._frame("boost", on=bool(on)))

    def set_ai(self, on: bool) -> None:
        self._ensure("AI")
        self._require("ai")
        self._commit(self._frame("ai", on=bool(on)))

    def set_sterilize(self, on: bool) -> None:
        self._ensure("杀菌")
        self._require("sterilize")
        self._commit(self._frame("sterilize", on=bool(on)))

    def set_child_lock(self, on: bool) -> None:
        self._ensure("童锁")
        self._require("child_lock")
        self._commit(self._frame("child_lock", on=bool(on)))

    # ------------------------------------------------------------------
    # 补充方案：末端 / ECO / 外出 / 即热时长 / 洗浴预约总开关 / 点动
    # ------------------------------------------------------------------
    def _require_power_on(self) -> None:
        """显式待机门控。

        `_ensure("")` 只做品类/故障门控，`feature_requires_power("")` 恒为 False，
        不会拦待机；补充方案的 setter 统一在 `_ensure("")` 后追加本检查。
        """
        if self.parsed.get("power_on") is False:
            raise WanjialeControlError("设备处于待机状态，请先开机")

    def set_emitter(self, value: int) -> None:
        """采暖末端：A 线 cmd43 复合包 / B3 直写 120。"""
        self._ensure("")
        self._require_power_on()
        self._require("heating", "emitter")
        params: Dict[str, Any] = {}
        emitter_cmd = self._point("heating").get("emitter_cmd") or {}
        if emitter_cmd.get("with_temp"):
            # 仅带温度/回差形态需要当前 94/97；YJW 只发末端，不要求这两个值
            params["heat_temp"] = self.parsed.get("heat_target_temp")
            params["heat_diff"] = self.parsed.get("diff_heat")
        self._commit(self._frame("emitter", value=int(value), **params), optimistic=False)

    def set_eco(self, on: bool) -> None:
        """节能/睡眠（仅 A 线 cmd38）。"""
        self._ensure("")
        self._require_power_on()
        self._require("heating")
        self._commit(self._frame("eco", on=bool(on)), optimistic=False)

    def set_away(self, on: bool) -> None:
        """外出（FFY/WLK cmd44，无读点）。"""
        self._ensure("")
        self._require_power_on()
        self._require("away_cmd")
        self._commit(self._frame("away", on=bool(on)), optimistic=False)

    def set_circulation(self, value: int) -> None:
        """即热时长：A 线 cmd26 / B3 直写 118。"""
        self._ensure("")
        self._require_power_on()
        self._require("circulation")
        self._commit(self._frame("circulation", value=int(value)), optimistic=False)

    def set_wash_master(self, on: bool) -> None:
        """洗浴预约总开关（XQZ 116）。"""
        self._ensure("")
        self._require_power_on()
        sched = self._require("schedule_wash")
        if (sched.get("master") or {}).get("dpid") in (None, ""):
            raise WanjialeControlError("该机型未配置洗浴预约总开关")
        self._commit(self._frame("wash_master", on=bool(on)), optimistic=False)

    def set_jog(self, on: bool) -> None:
        """点动：仅 B3 的 jog(117, writable)；A 线 68 只读，拒绝下发。"""
        self._ensure("")
        self._require_power_on()
        if self._point("jog").get("writable") is not True:
            raise WanjialeControlError("该机型点动状态只读，拒绝下发")
        self._commit(self._frame("jog", on=bool(on)), optimistic=False)

    # ------------------------------------------------------------------
    # 数值
    # ------------------------------------------------------------------
    def set_keep_warm(self, minutes: int) -> None:
        self._ensure("保温时长")
        self._require("keep_warm")
        self._commit(self._frame("keep_warm", minutes=int(minutes)))

    def set_diff_temp(self, value: int) -> None:
        self._ensure("回差温度")
        self._require("diff_temp", "diff_dhw")
        self._commit(self._frame("diff", kind="dhw", value=int(value)))

    def set_heat_diff(self, value: int) -> None:
        self._ensure("回差温度")
        self._require("diff_heat")
        self._commit(self._frame("diff", kind="heat", value=int(value)))

    # ------------------------------------------------------------------
    # 预约组编辑（设备级；spec 通过 method_args 传 kind/group 或 n）
    # ------------------------------------------------------------------
    _SCHEDULE_FEATURE = {"heat": "预约供暖", "dhw": "热水预热", "wash": None}

    def _schedule_item(self, kind: str) -> Dict[str, Any]:
        item = self._point(f"schedule_{kind}")
        if not item:
            raise WanjialeControlError(f"该机型未配置预约组点位：{kind}")
        return item

    def set_schedule_time(self, kind: str, group: int, field: str, minutes: int) -> None:
        feature = self._SCHEDULE_FEATURE.get(kind)
        if feature:
            self._ensure(feature)
        sched = self._schedule_item(kind)
        params: Dict[str, Any] = {
            "kind": kind, "group": int(group), "field": str(field), "minutes": int(minutes),
        }
        # A 线与 B3 洗浴预约都是「同组起止整包」，必须先从回读值取另一端，拿不到就报错
        packed_group = kind == "wash" and bool(sched.get("packed"))
        if self._points().get("dialect") == "A" or packed_group:
            other_field = "end" if str(field) == "start" else "start"
            other = self.parsed.get(f"{kind}_schedule_{int(group)}_{other_field}")
            if other is None:
                raise WanjialeControlError(
                    "未回读到该组另一端时间，已取消写入（避免只写半个预约包）"
                )
            params["other_minutes"] = int(other)
        frame = self._frame("schedule_time", **params)
        # 组时间可能是只写点位（JJY/LLF 的 129~132），不做乐观更新
        self._commit(frame, optimistic=False)

    def set_schedule_enable(self, kind: str, group: int, on: bool) -> None:
        feature = self._SCHEDULE_FEATURE.get(kind)
        if feature:
            self._ensure(feature)
        self._schedule_item(kind)
        frame = self._frame("schedule_enable", kind=kind, group=int(group), on=bool(on))
        self._commit(frame, optimistic=False)

    def set_schedule_temp(self, kind: str, group: int, value: int) -> None:
        feature = self._SCHEDULE_FEATURE.get(kind)
        if feature:
            self._ensure(feature)
        self._schedule_item(kind)
        frame = self._frame("schedule_temp", kind=kind, group=int(group), value=int(value))
        self._commit(frame)

    def set_schedule_delete(
        self, kind: str, group: int, packed_value: Optional[int] = None
    ) -> None:
        feature = self._SCHEDULE_FEATURE.get(kind)
        if feature:
            self._ensure(feature)
        if self._points().get("dialect") == "A" and packed_value is None:
            # A 线删除同样需要整组起止；从 list 回读值合成，不写半包
            start = self.parsed.get(f"{kind}_schedule_{int(group)}_start")
            end = self.parsed.get(f"{kind}_schedule_{int(group)}_end")
            if start is None or end is None:
                raise WanjialeControlError("未回读到该组的起止时间，已取消删除")
            packed_value = (int(start) << 16) | int(end)
        frame = self._frame(
            "schedule_delete", kind=kind, group=int(group), packed_value=packed_value
        )
        self._commit(frame, optimistic=False)

    def delete_wash_schedule(self) -> None:
        """RTF 洗浴预约删除：从回读的起止分钟重算 packed 值下发到 104。

        删除值按「与写入相同的 packed 值」下发；
        回读不到当前起止时不写，避免误删别的组。
        """
        sched = self._point("schedule_wash")
        if not sched.get("delete"):
            raise WanjialeControlError("该机型不支持洗浴预约删除")
        start = self.parsed.get("wash_schedule_1_start")
        end = self.parsed.get("wash_schedule_1_end")
        if start is None or end is None:
            raise WanjialeControlError("未回读到洗浴预约时间，已取消删除")
        start_h, start_m = divmod(int(start), 60)
        end_h, end_m = divmod(int(end), 60)
        packed = ((start_h + start_m * 256) & 0xFFFF) | ((end_h + end_m * 256) << 16)
        self.set_schedule_delete("wash", 1, packed)

    def set_thermostat(self, n: int, on: bool) -> None:
        """B4 八路温控器开关（设定温度只读、配对/编号管理不做）。"""
        if not self._thermostats():
            raise WanjialeControlError("该机型不支持温控器控制")
        self._commit(self._frame("thermostat", n=int(n), on=bool(on)), optimistic=False)

    # ------------------------------------------------------------------
    # A 线专属动作（编码已在 features_002 落地）
    # ------------------------------------------------------------------
    def set_morning_night(self, mode: int) -> None:
        self._ensure("晨浴夜浴")
        self._require("morning_night")
        self._commit(self._frame("morning_night", mode=int(mode)), optimistic=False)

    def reset_device(self) -> None:
        self._ensure("复位")
        self._require("reset")
        self._commit(self._frame("reset"), optimistic=False)

    def clear_reserve_conflict(self) -> None:
        self._ensure("预约冲突清除")
        self._require("conflict")
        self._commit(self._frame("conflict"), optimistic=False)

    # ------------------------------------------------------------------
    # 状态视图（只读；实体取值走 parsed）
    # ------------------------------------------------------------------
    @property
    def fault_code(self) -> Optional[int]:
        return self.parsed.get("fault")

    def query_status(self) -> Dict[str, Any]:
        return self._protocol.query_device(self.did)


# ======================================================================
# 顶层 API
# ======================================================================
class WanjialeApi:
    """对 HA 集成暴露的顶层接口。"""

    def __init__(
        self,
        protocol: WanjialeProtocol,
        feature_overrides: Optional[Dict[str, str]] = None,
        enable_control_rights: bool = False,
        login_name: str = "",
    ) -> None:
        self._protocol = protocol
        self._devices: List[WanjialeDevice] = []
        self._feature_overrides = feature_overrides or {}
        self._enable_control_rights = enable_control_rights
        self._login_name = login_name
        self._last_device_list_refresh: float = 0.0
        self._bg_device_list_interval: float = 300.0
        # 局域网查询失败退避：did → 允许再次尝试局域网的时间戳（见 _arm_lan_backoff）
        self._lan_retry_after: Dict[str, float] = {}

    @property
    def devices(self) -> List[WanjialeDevice]:
        return list(self._devices)

    @property
    def protocol(self) -> WanjialeProtocol:
        return self._protocol

    @property
    def control_rights_enabled(self) -> bool:
        return self._enable_control_rights

    @property
    def login_name(self) -> str:
        return self._login_name

    def login(self) -> Dict[str, Any]:
        return self._protocol.login()

    # ------------------------------------------------------------------
    # 设备加载
    # ------------------------------------------------------------------
    def load_devices(self) -> List[WanjialeDevice]:
        raw_list = self._protocol.get_devices()
        self._devices = []
        for raw in raw_list:
            caps = resolve(
                firm=str(raw.get("firm") or ""),
                product=str(raw.get("product") or ""),
                model=str(raw.get("model") or ""),
                model_id=str(raw.get("modelId") or ""),
            )
            cls = _resolve_device_class(raw, caps)
            dev = cls(self._protocol, raw)
            dev.bind_capabilities(caps)
            dev.login_name = self._login_name
            dev.control_rights = self._enable_control_rights
            _LOGGER.info(
                "设备分类: did=%s name=%s product=%s modelId=%s → %s%s | %s",
                _short_did(raw.get("did")), raw.get("name"), raw.get("product"), raw.get("modelId"),
                cls.__name__,
                "（品类未收录，当前为只读）" if cls is WanjialeDevice else "",
                caps.describe(),
            )
            self._devices.append(dev)

        self._discover_lan()
        return self._devices

    def finalize_features(self) -> None:
        """首次状态刷新后确定各设备的功能集合（实体创建的依据）。

        本方法在 executor 里执行（`async_setup_entry` 用 `async_add_executor_job` 调用），
        因此在末尾顺带预热故障文案表，否则实体首次写状态时会在事件循环里
        同步读 `faults_*.json`，被 HA 记为阻塞调用（见 `capabilities.preload_faults`）。
        """
        for dev in self._devices:
            try:
                dev.finalize_features(self._feature_overrides)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("解析设备 %s 能力失败", _short_did(dev.did), exc_info=True)
        preload_faults(dev.caps.product for dev in self._devices)

    def _discover_lan(self) -> None:
        """UDP 广播发现局域网设备，按 did 精确回填 local_host / local_port。

        多设备下不能把单个应答的 IP 套给所有设备：一台设备只认自己的那个应答。
        """
        if not self._devices:
            return
        missing = [dev for dev in self._devices if not dev.local_host]
        if not missing:
            return
        try:
            found = self._protocol.discover_devices(timeout=2.0)
        except Exception:  # noqa: BLE001
            # 协议层内部已兜住广播异常，这里只是防御
            _LOGGER.debug("UDP 广播发现失败", exc_info=True)
            return
        if not found:
            return

        by_did = {item.did: item for item in found}
        for dev in missing:
            item = by_did.get(dev.did)
            if item is None:
                _LOGGER.debug("广播未发现设备 %s，保持无局域网地址", dev.name)
                continue
            dev.local_host = item.ip
            dev.local_port = LOCAL_PORT
            _LOGGER.info(
                "LAN 发现: %s(%s) → %s:%d",
                dev.name, _short_did(dev.did), item.ip, LOCAL_PORT,
            )

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        """刷新所有设备状态；任何异常不得穿透（coordinator 成功后实体仍可用）。"""
        try:
            self._refresh_all_impl()
        except Exception:  # noqa: BLE001
            # 常规失败已在内部逐项处理，走到这里说明刷新逻辑有缺陷
            _LOGGER.error("刷新设备状态时发生未预期异常", exc_info=True)

    def _refresh_all_impl(self) -> None:
        if not self._devices:
            return
        if not any(dev.local_host for dev in self._devices):
            self._discover_lan()

        threading.Thread(target=self._try_refresh_device_list, daemon=True).start()

        online = [dev for dev in self._devices if dev.online]
        # 按在线设备数分配单台超时，避免设备数一多单轮就超出轮询间隔
        timeout = device_query_timeout(len(online))

        for dev in online:
            result = self._query_with_fallback(dev, timeout)
            if not isinstance(result, dict) or result.get("error"):
                self._report_query_failure(
                    dev, result.get("error") if isinstance(result, dict) else result
                )
                continue
            if dev._query_failed_logged:
                dev._query_failed_logged = False
                _LOGGER.info("设备 %s 状态查询已恢复", dev.name)
            as_data = result.get("as")
            if not (isinstance(as_data, dict) and as_data):
                continue
            # 状态与故障一次性交给设备（fids 供 008/006 等品类判故障）
            dev.apply_state(as_data, result.get("fids"), result)
            _LOGGER.debug(
                "设备状态更新: %s power=%s current=%s target=%s mode=%s",
                dev.name, dev.parsed.get("power_on"),
                dev.parsed.get("current_temp"), dev.parsed.get("target_temp"),
                dev.parsed.get("mode"),
            )

    def _query_with_fallback(self, dev: WanjialeDevice, timeout: float) -> Dict[str, Any]:
        """局域网优先，失败回退云端。

        主因：局域网上设备会约 1 帧/秒主动推送增量（`cmd=post`），而
        `query_local_device` 的读循环会跳过非匹配帧，因此每轮查询顺带把积压读掉；
        若查询走云端，这条链路长期无人读，接收缓冲会无界增长。
        次因：状态不再依赖云端长连接，云端故障只影响回退路径。
        单轮耗时与走云端相差不大：设备是在自己的推送节拍上应答查询，
        所以收益主要不在速度。

        局域网不可用（未发现地址 / AP 隔离 / 退避窗口内）时回退云端，行为同前。
        """
        result: Dict[str, Any] = {"error": "无可用通道"}
        try:
            result = self._query_device_lan(dev, timeout)
        except Exception as err:  # noqa: BLE001
            # 该函数内部已兜住常规失败，走到这里属意料之外；有退避窗口，不会每轮触发
            _LOGGER.error("局域网查询 %s 异常：%s", dev.name, err, exc_info=True)
        if isinstance(result, dict) and result.get("error"):
            try:
                result = self._query_device_cloud(dev, timeout)
            except Exception as err:  # noqa: BLE001
                # 云端是最后一条通道，它异常即本次查询失败（与轮询循环共用失败态去重）
                self._report_query_failure(dev, err)
        return result

    def _query_device_cloud(self, dev: WanjialeDevice, timeout: float) -> Dict[str, Any]:
        if not getattr(self._protocol, "_socket", None):
            return {"error": "云端未建立长连接"}
        return self._protocol.query_device(dev.did, timeout=timeout)

    def _query_device_lan(self, dev: WanjialeDevice, timeout: float) -> Dict[str, Any]:
        """局域网查询；连接与认证都限制在本次轮询预算内。

        失败时记录退避（`_arm_lan_backoff`），避免"局域网持续不可用"被每轮重试放大。
        """
        if not dev.is_lan_available():
            return {"error": "未发现局域网地址"}
        if time.time() < self._lan_retry_after.get(dev.did, 0.0):
            return {"error": "局域网退避中"}
        try:
            # 连接按 did 维护：只检查/建立这台设备自己的链路
            if not self._protocol.is_local_connected(dev.did):
                if not self._protocol.connect_local(
                    dev.did, dev.local_host, dev.local_port, dev.lan_pin, timeout=timeout
                ):
                    self._arm_lan_backoff(dev)
                    return {"error": "局域网认证失败"}
            result = self._protocol.query_local_device(dev.did, timeout=timeout)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("局域网查询 %s 异常：%s", dev.name, err, exc_info=True)
            self._protocol.close_local(dev.did)  # 只关这台，不影响其它设备
            self._arm_lan_backoff(dev)
            return {"error": "局域网查询失败"}
        if isinstance(result, dict) and result.get("error"):
            self._arm_lan_backoff(dev)
        else:
            self._lan_retry_after.pop(dev.did, None)
        return result

    def _arm_lan_backoff(self, dev: WanjialeDevice) -> None:
        """记录局域网失败：`LAN_RETRY_BACKOFF_SECONDS` 内该设备直接走云端。"""
        self._lan_retry_after[dev.did] = time.time() + LAN_RETRY_BACKOFF_SECONDS

    def _report_query_failure(self, dev: WanjialeDevice, reason: Any) -> None:
        """记录「两条通道都没取到数据」。

        设备不可达会一直持续到恢复为止，因此只在进入失败态时记录一次，
        避免每轮轮询重复（恢复时由调用方清除标记）。
        """
        if dev._query_failed_logged:
            return
        dev._query_failed_logged = True
        _LOGGER.error("设备 %s 状态查询失败（局域网与云端均不可用）：%s", dev.name, reason)

    async def async_refresh_all(self) -> None:
        """异步刷新（HA coordinator 调用）。"""
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.refresh_all)
        except Exception:  # noqa: BLE001
            _LOGGER.error("async_refresh_all 失败", exc_info=True)

    # ------------------------------------------------------------------
    # 后台刷新设备列表（更新在线状态 / 局域网参数）
    # ------------------------------------------------------------------
    def _try_refresh_device_list(self) -> None:
        now = time.time()
        if now - self._last_device_list_refresh < self._bg_device_list_interval:
            return
        self._last_device_list_refresh = now
        try:
            raw_list = self._protocol.get_devices()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("后台刷新设备列表失败：%s", err, exc_info=True)
            return
        try:
            self._apply_device_list(raw_list)
        except Exception:  # noqa: BLE001
            _LOGGER.error("应用设备列表时异常", exc_info=True)

    def _apply_device_list(self, raw_list: List[Dict[str, Any]]) -> None:
        did_to_device = {dev.did: dev for dev in self._devices}
        now = time.time()
        for raw in raw_list:
            did = str(raw.get("did") or "")
            dev = did_to_device.get(did)
            if dev is None:
                _LOGGER.info("发现新设备: %s（需重载集成后生效）", _short_did(did))
                continue
            dev._raw = raw
            dev.name = str(raw.get("name") or dev.name)
            dev.model_id = str(raw.get("modelId") or dev.model_id)
            dev.ver = str(raw.get("ver") or dev.ver)
            if not dev.lan_pin and raw.get("lanPin"):
                dev.lan_pin = str(raw["lanPin"])

            cloud_online = bool(raw.get("online"))
            if cloud_online:
                dev.online = True
            elif dev.is_lan_available():
                # 云端报离线但设备有局域网参数时，以「最近一次成功交互」为判据，
                # 避免云端状态推送延迟导致设备被误判为离线。
                if dev._last_seen_online and now - dev._last_seen_online >= 120:
                    dev.online = False
                    _LOGGER.info("设备 %s 超过 120s 未确认在线，标记离线", dev.name)
                else:
                    dev.online = True
            else:
                dev.online = False
            dev.refresh()

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------
    def connect_server(self) -> bool:
        return self._protocol.connect_server()

    def close_server(self) -> None:
        self._protocol.close_server()

    def close_local(self, did: Optional[str] = None) -> None:
        """关闭局域网连接：指定 `did` 只关该设备，不指定则关闭全部。"""
        self._protocol.close_local(did)

    def close(self) -> None:
        _LOGGER.debug("关闭云端长连接与局域网连接")
        self.close_server()
        self.close_local()

    def reconnect(self) -> bool:
        _LOGGER.debug("重连长连接")
        self.close_server()
        return self.connect_server()

    def get_device_by_did(self, did: str) -> Optional[WanjialeDevice]:
        for dev in self._devices:
            if dev.did == did:
                return dev
        return None

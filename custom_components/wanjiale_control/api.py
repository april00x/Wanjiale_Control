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
    """判定「谁认领这台设备」—— 品类归属的唯一来源。

    判据依次为：设备记录的 `product` 命中注册表；否则若能力表已收录该设备
    （`caps.matched`）则用认领它的品类（`caps.product`）；若命中品类把该机型声明为
    只读（`plugin.readonly_models` 含 `caps.key`）则返回 `None` —— 这类机型确属本产品码，
    但活动协议是另一路体系（001 的 OYY/HPZ 属 101+ 第二路体系），按本品位下发会误控；
    若命中品类的 `requires_model_entry` 为 True 而设备未命中机型表
    （`caps.model_points` 为空）也返回 `None` —— 防「用空点位建出可下发的主实体」；
    都不命中返回 `None`（未注册品类）。

    返回 `None` 时，`_resolve_device_class` 回落通用只读基类，平台侧表现为
    「只出 1 个原始状态诊断实体 + 拒绝下发」。
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
# 窗口结束自动恢复尝试 —— 不需要用户干预，也不会因一次抖动永久放弃局域网。
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

        # 老设备（客户端 `isOld()`）在局域网下必须发单键报文。该标志来自连接层
        # （客户端由 Connection 携带，云端设备记录里没有），故这里默认 False，
        # 且不做探测 —— 把「鉴权失败」当成老设备会与「lanPin 错误」混淆，
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
    「该机型是否有点位 `points.temp`」——温控点随机型变，缺点位时不暴露温度控件，
    避免出现「点了才报『该机型不支持设定温度』」的假控件。
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

        插件缺失（未注册品类）或解析抛错时降级为空字典，不让异常穿透 ——
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
        抛出、中止本次控制 —— 抢权与正式命令走同一条传输，传输坏了正式命令同样发不出去。
        """
        self._require_plugin()
        try:
            self._before_control()
        except WanjialeControlError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.warning("控制前置动作异常（已忽略，继续下发本次控制）", exc_info=True)
        self._dispatch(as_dict)

    def _before_control(self) -> None:
        """控制前置动作：抢占多端操作权（247/248）。

        操作权是跨品类系统级机制（001 / 002 / 008 共用），实现放基类，由
        `plugin.control_rights` 决定该品类是否使用它。

        仅当同时满足以下条件才发送（避免每次控制都多发 3 帧）：
          - 品类声明使用操作权，且选项页已开启；
          - 登录名已知；
          - 设备上报过 247 点位（说明该机型使用操作权机制）；
          - 247 当前值不是本账号手机号（未持有）；
          - 优先权不为 0（0 = 无优先不可控，发了也不会生效）。
        """
        plugin = self._require_plugin()
        if not plugin.control_rights or not self.control_rights or not self.login_name:
            return
        if not self.has_point(DVID_CONTROL_RIGHT_SET):
            return
        if str(self.state.get(DVID_CONTROL_RIGHT_SET) or "") == self.login_name:
            return
        if self.parsed.get("priority") == 0:
            return
        # 第一帧由品类插件给出（001 是「命令类型 24 + 开关值 1」的成对命令）。
        # 声明 control_rights=True 即意味着必须实现该动作；未实现则整体放弃抢权
        # （宁可不抢，也不发半套帧）。
        try:
            request_frame = plugin.encode_value("control_right")
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

    def set_motion(self) -> None:
        """点动（dvid1=17），单次触发。"""
        self._ensure("点动循环")
        self._send_opt_pair(f001.OP_MOTION, f001.pack_toggle(1))

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

    点位**全部取自 `self.caps.model_points`**（机型级，由 `capability_008.json` 的
    `devices[*].points` 经 C1/C2 透传），本类不出现任何裸 dvid：
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
        """该机型是否可设温 —— 由机型点位决定（008 没有独立的「温度」能力位）。

        温控点随机型变，缺点位时不暴露温度控件，避免出现「点了才报
        『该机型不支持设定温度』」的假控件（见实施方案 §九-12）。
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
        """下发**绝对值**温度（按机型 `points.temp.min/max` 钳位）。

        门控用 `_ensure("")`：只校验「品类已收录 + 设备无故障」，不做功能门控 ——
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
        因此在末尾顺带预热故障文案表 —— 否则实体首次写状态时会在事件循环里
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

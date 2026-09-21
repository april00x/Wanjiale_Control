"""万家乐设备抽象与控制 API 层。

分层：
  protocol.py     传输层（TCP 帧 / AES / 长连接 / 局域网）
  features_001.py 品类协议字典（dvid 语义、命令编码、位域、故障码）
  capabilities.py 能力解析（内置表 + 运行时探测 + 选项覆盖）
  api.py          设备对象层（本文件）：把上面三者组装成可控制的设备

架构约定：
  - WanjialeDevice 基类负责设备元信息、状态缓存、能力门控与传输回退；
  - 每个品类一个子类（当前只有 WanjialeGasWaterHeater / 001 燃气热水器），
    品类特有的控制方法放在子类里，并在方法首行调用 _ensure(feature) 做门控；
  - 新增品类：加 features_0XX.py 与一个子类，并在 _resolve_device_class 注册。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Type

from . import features_001 as f001
from .capabilities import (
    DeviceCapabilities,
    effective_points,
    feature_fault_exempt,
    feature_requires_power,
    observed_points,
    resolve,
    resolve_features,
)
from .protocol import LOCAL_PORT, WanjialeProtocol

_LOGGER = logging.getLogger(__name__)

# ======================================================================
# 异常
# ======================================================================
class WanjialeControlError(Exception):
    """控制被拒绝：机型不支持 / 设备待机 / 设备故障 / 下发失败。

    实体层捕获后转成 HomeAssistantError，把原因呈现给用户。
    """


# ======================================================================
# 设备类型注册与分派
# ======================================================================
_DEVICE_TYPE_REGISTRY: Dict[str, Type["WanjialeDevice"]] = {}


def register_device_type(device_type: str) -> Callable[[Type["WanjialeDevice"]], Type["WanjialeDevice"]]:
    """装饰器：注册设备类型到注册表。"""

    def _decorator(cls: Type["WanjialeDevice"]) -> Type["WanjialeDevice"]:
        _DEVICE_TYPE_REGISTRY[device_type] = cls
        return cls

    return _decorator


def _resolve_device_class(raw: Dict[str, Any], caps: DeviceCapabilities) -> Type["WanjialeDevice"]:
    """根据设备记录 + 能力解析结果，挑选合适的设备类。"""
    # 命中 001 能力表 ⇒ 一定是燃气热水器（该表只收 001 品类）
    if caps.matched:
        return WanjialeGasWaterHeater
    if str(raw.get("product") or "").strip() == f001.PRODUCT:
        return WanjialeGasWaterHeater

    t = str(raw.get("type") or "").strip().lower()
    model = str(raw.get("model") or "").strip().lower()
    product = str(raw.get("product") or "").strip().lower()
    for key in (t, model, product):
        if key and key in _DEVICE_TYPE_REGISTRY:
            return _DEVICE_TYPE_REGISTRY[key]

    name = str(raw.get("name") or "").strip().lower()
    for token in ("热水器", "water", "heater", "燃热", "燃气"):
        if token in name or token in model:
            return WanjialeGasWaterHeater

    # 按设备 AS 属性检测热水器特征
    as_data = raw.get("as", {})
    if isinstance(as_data, dict) and {"4", "28", "24"} & set(as_data.keys()):
        return WanjialeGasWaterHeater

    return WanjialeDevice


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

        # 能力
        self.caps: DeviceCapabilities = DeviceCapabilities()
        self.features: Dict[str, str] = {}
        self.points: Set[str] = set()

        # 多端操作权（默认关闭，由选项页开启）
        self.control_rights: bool = False
        self.login_name: str = ""

        self._last_seen_online: float = time.time() if self.online else 0.0
        self._last_control_time: float = 0.0
        self._optimistic: Dict[str, Any] = {}

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

        self.parsed = f001.parse_state(self.state)

    def optimistic(self, values: Dict[str, Any]) -> None:
        """控制成功后立即生效的预期值，冷却期内优先于设备回包。"""
        values = {str(k): v for k, v in values.items()}
        self.state.update(values)
        self._optimistic.update(values)
        self._last_control_time = time.time()
        self._last_seen_online = time.time()
        self.parsed = f001.parse_state(self.state)

    # ------------------------------------------------------------------
    # 门控
    # ------------------------------------------------------------------
    def _ensure(self, feature: str) -> None:
        """功能可用性 + 设备可操作性检查。"""
        if feature and not self.has_feature(feature):
            raise WanjialeControlError(f"该机型不支持「{feature}」")
        fault = self.parsed.get("fault")
        if (
            fault is not None
            and fault != f001.FAULT_NONE
            and not feature_fault_exempt(feature)
        ):
            text = self.parsed.get("fault_text") or str(fault)
            raise WanjialeControlError(f"设备故障 {text}，暂不支持操作")
        if feature_requires_power(feature) and self.parsed.get("power_on") is False:
            raise WanjialeControlError("设备处于待机状态，请先开机")

    # ------------------------------------------------------------------
    # 传输（局域网优先，失败回退云端）
    # ------------------------------------------------------------------
    def _send(self, as_dict: Dict[str, Any]) -> None:
        """下发控制命令；失败抛 WanjialeControlError。

        下发前先执行控制前置动作（如抢占操作权），前置动作失败不阻断本次控制。
        """
        try:
            self._before_control()
        except WanjialeControlError:
            raise
        except Exception:  # noqa: BLE001
            _LOGGER.debug("控制前置动作失败（忽略）", exc_info=True)
        self._dispatch(as_dict)

    def _before_control(self) -> None:
        """控制前置动作钩子；子类可覆盖（例如申请多端操作权）。"""

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
        """局域网下发；任一步失败即回退云端。"""
        try:
            if not getattr(self._protocol, "_local_socket", None):
                if not self._protocol.connect_local(self.local_host, self.local_port, self.lan_pin):
                    _LOGGER.warning("局域网认证失败，回退云端控制")
                    self._send_cloud(as_str)
                    return
            self._protocol.send_local_control(self.did, as_str)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("局域网控制失败(%s)，回退云端控制", err)
            self._protocol.close_local()
            self._send_cloud(as_str)

    def turn_on(self) -> None:
        raise NotImplementedError

    def turn_off(self) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} did={self.did} name={self.name!r} online={self.online}>"


# ======================================================================
# 燃气热水器（001 产品线）
# ======================================================================
@register_device_type("water_heater")
@register_device_type("热水器")
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
    # 操作权（默认关闭，由选项页开启）
    # ------------------------------------------------------------------
    def _before_control(self) -> None:
        """抢占设备操作权。

        仅当同时满足以下条件才发送（避免每次控制都多发 3 帧）：
          - 选项页已开启，且登录名已知；
          - 设备上报过 247 点位（说明该机型使用操作权机制）；
          - 247 当前值不是本账号手机号（未持有）；
          - 优先权不为 0（0 = 无优先不可控，发了也不会生效）。
        """
        if not self.control_rights or not self.login_name:
            return
        if not self.has_point(f001.DVID_CONTROL_RIGHT_SET):
            return
        if str(self.state.get(f001.DVID_CONTROL_RIGHT_SET) or "") == self.login_name:
            return
        if self.parsed.get("priority") == 0:
            return
        self._dispatch({
            f001.DVID_OP_TYPE: f001.OP_CONTROL_RIGHT,
            f001.DVID_OP_VALUE: f001.pack_toggle(1),
        })
        self._dispatch({f001.DVID_CONTROL_RIGHT_REQ: self.login_name})
        self._dispatch({f001.DVID_CONTROL_RIGHT_SET: self.login_name})

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------
    def query_status(self) -> Dict[str, Any]:
        return self._protocol.query_device(self.did)


# ======================================================================
# 预留：其他品类设备
# ======================================================================
@register_device_type("range_hood")
@register_device_type("油烟机")
class WanjialeRangeHood(WanjialeDevice):
    platform = "fan"
    category_cn = "油烟机"


@register_device_type("stove")
@register_device_type("灶具")
class WanjialeStove(WanjialeDevice):
    platform = "switch"
    category_cn = "灶具"


@register_device_type("disinfect")
@register_device_type("消毒柜")
class WanjialeDisinfect(WanjialeDevice):
    platform = "switch"
    category_cn = "消毒柜"


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
                "设备分类: did=%s name=%s modelId=%s → %s | %s",
                raw.get("did"), raw.get("name"), raw.get("modelId"),
                cls.__name__, caps.describe(),
            )
            self._devices.append(dev)

        self._discover_lan()
        return self._devices

    def finalize_features(self) -> None:
        """首次状态刷新后确定各设备的功能集合（实体创建的依据）。"""
        for dev in self._devices:
            try:
                dev.finalize_features(self._feature_overrides)
            except Exception:  # noqa: BLE001
                _LOGGER.warning("解析设备 %s 能力失败", dev.did, exc_info=True)

    def _discover_lan(self) -> None:
        """UDP 广播发现局域网 IP，自动填充 local_host / local_port。"""
        if not self._devices:
            return
        if all(dev.local_host for dev in self._devices):
            return
        try:
            ip = self._protocol.discover_device(timeout=2.0)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("UDP 广播发现失败", exc_info=True)
            return
        if not ip:
            return
        for dev in self._devices:
            if not dev.local_host:
                dev.local_host = ip
                dev.local_port = LOCAL_PORT
                _LOGGER.info("LAN 发现: %s → %s:%d", dev.name, dev.local_host, dev.local_port)

    # ------------------------------------------------------------------
    # 状态刷新
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        """刷新所有设备状态；任何异常不得穿透（coordinator 成功后实体仍可用）。"""
        try:
            self._refresh_all_impl()
        except Exception:  # noqa: BLE001
            _LOGGER.debug("refresh_all 异常", exc_info=True)

    def _refresh_all_impl(self) -> None:
        if not self._devices:
            return
        if not any(dev.local_host for dev in self._devices):
            self._discover_lan()

        threading.Thread(target=self._try_refresh_device_list, daemon=True).start()

        for dev in self._devices:
            if not dev.online:
                continue
            result = self._query_with_fallback(dev)
            if not isinstance(result, dict) or result.get("error"):
                continue
            as_data = result.get("as")
            if isinstance(as_data, dict) and as_data:
                dev._raw["as"] = as_data
                dev._last_seen_online = time.time()
                dev.refresh()
                _LOGGER.debug(
                    "设备状态更新: %s power=%s current=%s target=%s mode=%s",
                    dev.name, dev.parsed.get("power_on"),
                    dev.parsed.get("current_temp"), dev.parsed.get("target_temp"),
                    dev.parsed.get("mode"),
                )

    def _query_with_fallback(self, dev: WanjialeDevice) -> Dict[str, Any]:
        """云端长连接优先，失败回退局域网。"""
        result: Dict[str, Any] = {"error": "no transport"}
        try:
            result = self._query_device_cloud(dev)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("云端查询 %s 失败", dev.did)
        if isinstance(result, dict) and result.get("error") and dev.is_lan_available():
            try:
                result = self._query_device_lan(dev)
            except Exception:  # noqa: BLE001
                _LOGGER.debug("局域网查询 %s 失败", dev.did)
        return result

    def _query_device_cloud(self, dev: WanjialeDevice) -> Dict[str, Any]:
        if not getattr(self._protocol, "_socket", None):
            return {"error": "no cloud socket"}
        return self._protocol.query_device(dev.did, timeout=3)

    def _query_device_lan(self, dev: WanjialeDevice) -> Dict[str, Any]:
        if not dev.is_lan_available():
            return {"error": "no LAN"}
        try:
            if not getattr(self._protocol, "_local_socket", None):
                if not self._protocol.connect_local(dev.local_host, dev.local_port, dev.lan_pin):
                    return {"error": "lan auth failed"}
            return self._protocol.query_local_device(dev.did, timeout=3)
        except Exception:  # noqa: BLE001
            self._protocol.close_local()
            return {"error": "lan query failed"}

    async def async_refresh_all(self) -> None:
        """异步刷新（HA coordinator 调用）。"""
        import asyncio

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.refresh_all)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("async_refresh_all 失败")

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
            _LOGGER.debug("HTTP 刷新设备列表失败: %s", err)
            return
        try:
            self._apply_device_list(raw_list)
        except Exception:  # noqa: BLE001
            _LOGGER.debug("_apply_device_list 异常", exc_info=True)

    def _apply_device_list(self, raw_list: List[Dict[str, Any]]) -> None:
        did_to_device = {dev.did: dev for dev in self._devices}
        now = time.time()
        for raw in raw_list:
            did = str(raw.get("did") or "")
            dev = did_to_device.get(did)
            if dev is None:
                _LOGGER.info("发现新设备: %s（需重载集成后生效）", did)
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

    def close_local(self) -> None:
        self._protocol.close_local()

    def close(self) -> None:
        self.close_server()
        self.close_local()

    def reconnect(self) -> bool:
        self.close_server()
        return self.connect_server()

    def get_device_by_did(self, did: str) -> Optional[WanjialeDevice]:
        for dev in self._devices:
            if dev.did == did:
                return dev
        return None

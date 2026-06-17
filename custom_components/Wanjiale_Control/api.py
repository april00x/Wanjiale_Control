"""万家乐设备抽象与控制 API 层。

该层将 protocol.py 提供的基础 TCP/HTTP 能力包装成"设备对象"：
  - WanjialeDevice：基类，描述任意设备；
  - WanjialeWaterHeater：热水器子类，封装开关机/设置温度/模式；

控制命令格式（基于前端 index.js 分析）：
  client.opt(deviceId, dvid, value)
  JSON: {"to":"did","cmd":"opt","mid":"xxx","as":{"dvid":"value"}}

dvid 含义：
  "1"  - 操作类型标识
  "2"  - 操作值（32位整数，编码：mode*16777216 + temp*65536 + other）
  "4"  - 开关机状态（0=关机，1=开机）
  "24" - 模式（4=舒适浴，5=随温感，10=ECO，11=SUR，14=厨房洗）
  "28" - 目标温度
  "251" - 杀菌状态

值编码方式：
  value = mode * 16777216 + temp * 65536 + byte2 * 256 + byte1
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable, Dict, List, Optional, Type

from .protocol import LOCAL_PORT, WanjialeProtocol

_LOGGER = logging.getLogger(__name__)

# ======================================================================
# 设备类型注册表
# ======================================================================
_DEVICE_TYPE_REGISTRY: Dict[str, Type["WanjialeDevice"]] = {}


def register_device_type(device_type: str) -> Callable[[Type["WanjialeDevice"]], Type["WanjialeDevice"]]:
    """装饰器：注册设备类型到注册表。"""

    def _decorator(cls: Type["WanjialeDevice"]) -> Type["WanjialeDevice"]:
        _DEVICE_TYPE_REGISTRY[device_type] = cls
        return cls

    return _decorator


def _resolve_device_class(raw_device: Dict[str, Any]) -> Type["WanjialeDevice"]:
    """根据 machtapi 返回的原始设备字典，挑选合适的设备类。"""
    t = str(raw_device.get("type") or "").strip().lower()
    model = str(raw_device.get("model") or "").strip().lower()
    name = str(raw_device.get("name") or "").strip().lower()
    product = str(raw_device.get("product") or "").strip().lower()

    for key in (t, model, product):
        if key and key in _DEVICE_TYPE_REGISTRY:
            return _DEVICE_TYPE_REGISTRY[key]
    for token in ("热水器", "water", "heater", "燃热", "燃气"):
        if token in name or token in model:
            return WanjialeWaterHeater

    # 按设备 AS 属性检测热水器特征
    as_data = raw_device.get("as", {})
    if isinstance(as_data, dict):
        water_heater_dvids = {"4", "28", "24", "17"}
        if water_heater_dvids & set(as_data.keys()):
            return WanjialeWaterHeater

    return WanjialeDevice


# ======================================================================
# 基类：WanjialeDevice
# ======================================================================
class WanjialeDevice:
    """任意万家乐设备的基类。"""

    platform = "sensor"
    category_cn = "通用设备"

    def __init__(
        self,
        protocol: WanjialeProtocol,
        raw_device: Dict[str, Any],
    ) -> None:
        self._protocol = protocol
        self._raw = raw_device

        self.did: str = str(raw_device.get("did") or "")
        self.name: str = str(raw_device.get("name") or self.did)
        self.model: str = str(raw_device.get("model") or "")
        self.online: bool = bool(raw_device.get("online"))
        self.product: str = str(raw_device.get("product") or "")
        self.firm: str = str(raw_device.get("firm") or "")

        # 局域网控制参数
        self.local_host: Optional[str] = raw_device.get("lanIp")
        self.local_port: int = raw_device.get("lanPort", 0)
        self.lan_pin: str = raw_device.get("lanPin", "")

        # 状态缓存
        self.attributes: Dict[str, Any] = dict(raw_device)

    def refresh(self) -> None:
        """刷新设备状态。"""
        self.online = bool(self._raw.get("online"))
        self.attributes.update(self._raw)

    def unique_id(self) -> str:
        return f"wanjiale-{self.did}"

    def is_lan_available(self) -> bool:
        return (
            self.local_host is not None
            and self.local_port > 0
            and len(self.lan_pin) > 0
        )

    # ------------------------------------------------------------------
    # 控制命令
    # ------------------------------------------------------------------
    def _send_opt(self, dvid: str, value: int) -> Dict[str, Any]:
        as_dict = {dvid: str(value)}
        if self.is_lan_available():
            return self._send_lan_control(as_dict)
        return self._send_cloud_control(as_dict)

    def _send_opt_pair(self, op_type: int, value: int) -> Dict[str, Any]:
        as_dict = {"1": str(op_type), "2": str(value)}
        if self.is_lan_available():
            return self._send_lan_control(as_dict)
        return self._send_cloud_control(as_dict)

    def _send_cloud_control(self, as_dict: Dict[str, Any]) -> Dict[str, Any]:
        if not getattr(self._protocol, "_socket", None):
            try:
                self._protocol.connect_server()
            except Exception:
                _LOGGER.debug("建立长连接失败, 云控制不可用")
                return {"error": "cloud unavailable"}
        try:
            return self._protocol.send_control(self.did, as_dict)
        except Exception:
            _LOGGER.debug("send_control 失败")
            return {"error": "send failed"}

    def _send_lan_control(self, as_dict: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if not getattr(self._protocol, "_local_socket", None):
                success = self._protocol.connect_local(
                    self.local_host, self.local_port, self.lan_pin,
                )
                if not success:
                    _LOGGER.warning("局域网认证返回失败, 回退到云端控制")
                    return self._send_cloud_control(as_dict)
            self._protocol.send_local_control(self.did, as_dict)
            # LAN fire-and-forget 后立即云端 query 获取真实状态
            # 设备收到 LAN 命令后会通过云端推送 post（~0.7s 到达）
            if getattr(self._protocol, "_socket", None):
                try:
                    result = self._protocol.query_device(self.did, timeout=2.5, accept_post=True)
                    if isinstance(result, dict) and isinstance(result.get("as"), dict):
                        current_as = self.attributes.get("as", {})
                        if isinstance(current_as, dict):
                            current_as.update(result["as"])
                        else:
                            self.attributes["as"] = dict(result["as"])
                        self.refresh()
                        return result
                except Exception:
                    pass
            return {"status": "sent"}
        except Exception as exc:
            _LOGGER.warning("局域网控制失败 (%s), 回退到云端控制", exc)
            self._protocol.close_local()
            try:
                return self._send_cloud_control(as_dict)
            except Exception:
                _LOGGER.debug("云控制回退也失败")
                return {"error": str(exc)}

    def turn_on(self) -> None:
        raise NotImplementedError

    def turn_off(self) -> None:
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} did={self.did} name={self.name!r} online={self.online}>"


# ======================================================================
# 热水器设备
# ======================================================================
@register_device_type("water_heater")
@register_device_type("热水器")
class WanjialeWaterHeater(WanjialeDevice):
    """万家乐热水器。"""

    platform = "water_heater"
    category_cn = "热水器"

    DVID_OP_TYPE = "1"
    DVID_OP_VALUE = "2"
    DVID_POWER = "4"
    DVID_MODE = "24"
    DVID_TEMP = "28"
    DVID_FAULT = "17"
    DVID_STATUS = "10"
    DVID_ZERO_WATER = "61"
    DVID_STERILIZE = "251"

    OP_MODE = 4
    OP_INSTANT_HEAT = 14
    OP_UV = 6
    OP_BOOST = 7
    OP_MOTION = 17
    OP_ALL_DAY = 27
    OP_RESERVE = 15
    OP_KEEP_WARM = 10
    OP_DIFF_TEMP = 11
    OP_KITCHEN = 29

    MODE_COMFORT = 4
    MODE_SMART = 5
    MODE_ECO = 10
    MODE_SUR = 11
    MODE_KITCHEN = 14

    STATUS_HEATING = 4
    STATUS_WATER_FLOW = 2

    target_temperature: Optional[int] = None
    current_temperature: Optional[int] = None
    is_power_on: Optional[bool] = None
    current_mode: Optional[int] = None
    is_heating: Optional[bool] = None
    fault_code: Optional[int] = None
    is_sterilizing: Optional[bool] = None
    is_boost: Optional[bool] = None
    is_instant_heat: Optional[bool] = None

    _last_control_time: float = 0.0
    CONTROL_COOLDOWN = 5.0

    MIN_TEMP = 30
    MAX_TEMP = 60

    def refresh(self) -> None:
        super().refresh()

        as_data = self.attributes.get("as", {})
        if not isinstance(as_data, dict):
            return

        in_cooldown = time.time() - self._last_control_time < self.CONTROL_COOLDOWN

        if self.DVID_POWER in as_data:
            if not in_cooldown:
                self.is_power_on = str(as_data[self.DVID_POWER]) == "1"

        if self.DVID_MODE in as_data:
            if not in_cooldown:
                self.current_mode = int(as_data[self.DVID_MODE])

        if self.DVID_TEMP in as_data:
            new_temp = int(as_data[self.DVID_TEMP])
            if not in_cooldown:
                self.target_temperature = new_temp
            self.current_temperature = new_temp

        if self.DVID_FAULT in as_data:
            self.fault_code = int(as_data[self.DVID_FAULT])
            if self.fault_code != 255:
                _LOGGER.warning("热水器故障: %s", self.fault_code)

        if self.DVID_STATUS in as_data:
            status = int(as_data[self.DVID_STATUS])
            self.is_heating = bool(status & self.STATUS_HEATING)

        if self.DVID_STERILIZE in as_data:
            self.is_sterilizing = str(as_data[self.DVID_STERILIZE]) == "1"

        if "20" in as_data:
            self.is_boost = bool(int(as_data["20"]) & 3)

        if self.DVID_ZERO_WATER in as_data:
            zw_status = int(as_data[self.DVID_ZERO_WATER])
            self.is_instant_heat = bool(zw_status & 8)

    # ------------------------------------------------------------------
    # 控制方法
    # ------------------------------------------------------------------
    def set_power(self, on: bool) -> Dict[str, Any]:
        value = 1 if on else 0
        result = self._send_opt(self.DVID_POWER, value)
        if isinstance(result, dict) and not result.get("error"):
            self.is_power_on = on
            self._last_control_time = time.time()
        return result

    def set_temperature(self, temperature: int) -> Dict[str, Any]:
        temp = max(self.MIN_TEMP, min(self.MAX_TEMP, temperature))
        mode = self.current_mode or self.MODE_COMFORT
        as_data = self.attributes.get("as", {}) or {}
        byte2 = int(as_data.get("29", 0) or 0)
        byte1 = int(as_data.get("30", 0) or 0)
        value = mode * 16777216 + temp * 65536 + byte2 * 256 + byte1
        result = self._send_opt_pair(self.OP_MODE, value)
        if isinstance(result, dict) and not result.get("error"):
            self.target_temperature = temp
            self._last_control_time = time.time()
        return result

    def set_mode(self, mode: int) -> Dict[str, Any]:
        temp = self.target_temperature or 40
        as_data = self.attributes.get("as", {}) or {}
        byte2 = int(as_data.get("29", 0) or 0)
        byte1 = int(as_data.get("30", 0) or 0)
        value = mode * 16777216 + temp * 65536 + byte2 * 256 + byte1
        result = self._send_opt_pair(self.OP_MODE, value)
        if isinstance(result, dict) and not result.get("error"):
            self.current_mode = mode
            self._last_control_time = time.time()
        return result

    def set_instant_heat(self, on: bool, duration: int = 0) -> Dict[str, Any]:
        if on:
            value = duration * 16777216 + 2 * 65536 + 65535
        else:
            value = 2 * 65536 + 65535
        return self._send_opt_pair(self.OP_INSTANT_HEAT, value)

    def set_boost(self, on: bool) -> Dict[str, Any]:
        value = 16777216 + 2 * 65536 + 65535 if on else 2 * 65536 + 65535
        result = self._send_opt_pair(self.OP_BOOST, value)
        if isinstance(result, dict) and not result.get("error"):
            self.is_boost = on
            self._last_control_time = time.time()
        return result

    def set_sterilize(self, on: bool) -> Dict[str, Any]:
        return self._send_opt(self.DVID_STERILIZE, 1 if on else 0)

    def set_all_day(self, on: bool) -> Dict[str, Any]:
        value = 4 * 16777216 + 2 * 65536 + 65535 if on else 2 * 65536 + 65535
        return self._send_opt_pair(self.OP_ALL_DAY, value)

    def set_motion(self, on: bool) -> Dict[str, Any]:
        value = 16777216 + 2 * 65536 + 65535 if on else 2 * 65536 + 65535
        return self._send_opt_pair(self.OP_MOTION, value)

    def set_kitchen_timer(self, timer_index: int) -> Dict[str, Any]:
        value = timer_index * 16777216 + 2 * 65536 + 255 * 256 + 2
        return self._send_opt_pair(self.OP_KITCHEN, value)

    def turn_on(self) -> Dict[str, Any]:
        return self.set_power(True)

    def turn_off(self) -> Dict[str, Any]:
        return self.set_power(False)

    def query_status(self) -> Dict[str, Any]:
        return self._protocol.query_device(self.did)

    def get_mode_name(self, mode: Optional[int] = None) -> str:
        m = mode or self.current_mode
        return {
            self.MODE_COMFORT: "舒适浴",
            self.MODE_SMART: "随温感",
            self.MODE_ECO: "ECO",
            self.MODE_SUR: "SUR",
            self.MODE_KITCHEN: "厨房洗",
        }.get(m, "未知")


# ======================================================================
# 预留：其他设备类型
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
# 顶层 API：WanjialeApi
# ======================================================================
class WanjialeApi:
    """对 HA 集成暴露的顶层接口。"""

    def __init__(self, protocol: WanjialeProtocol) -> None:
        self._protocol = protocol
        self._devices: List[WanjialeDevice] = []

    @property
    def devices(self) -> List[WanjialeDevice]:
        return list(self._devices)

    @property
    def protocol(self) -> WanjialeProtocol:
        return self._protocol

    def login(self) -> Dict[str, Any]:
        return self._protocol.login()

    def load_devices(self) -> List[WanjialeDevice]:
        raw_list = self._protocol.get_devices()
        self._devices = []
        for raw in raw_list:
            cls = _resolve_device_class(raw)
            _LOGGER.info(
                "设备分类: did=%s name=%s model=%s → %s",
                raw.get("did"), raw.get("name"), raw.get("model"), cls.__name__,
            )
            self._devices.append(cls(self._protocol, raw))

        # 尝试 UDP 广播发现局域网 IP
        self._discover_lan()

        return self._devices

    def _discover_lan(self) -> None:
        """UDP 广播发现局域网 IP，自动填充 local_host / local_port。"""
        if not self._devices:
            return
        try:
            ip = self._protocol.discover_device(timeout=2.0)
        except Exception:
            _LOGGER.debug("UDP 广播发现失败")
            return
        if not ip:
            return
        for dev in self._devices:
            if not dev.local_host:
                dev.local_host = ip
                dev.local_port = LOCAL_PORT
                _LOGGER.info(
                    "LAN 发现: %s → %s:%d",
                    dev.name, dev.local_host, dev.local_port,
                )

    # ------------------------------------------------------------------
    # 核心：通过 TCP 长连接查询设备状态
    # ------------------------------------------------------------------
    def refresh_all(self) -> None:
        """刷新所有设备状态。

        LAN 用于控制 + 查询回退。云端长连接优先查询。
        """
        if not self._devices:
            return

        if not any(dev.local_host for dev in self._devices):
            self._discover_lan()

        try:
            raw_list = self._protocol.get_devices()
            self._apply_device_list(raw_list)
        except Exception:
            _LOGGER.debug("HTTP 刷新设备列表失败")

        for dev in self._devices:
            if not dev.online:
                continue
            try:
                result = self._query_device_cloud(dev)
                if isinstance(result, dict) and result.get("error") and dev.is_lan_available():
                    result = self._query_device_lan(dev)
            except Exception:
                if dev.is_lan_available():
                    try:
                        result = self._query_device_lan(dev)
                    except Exception:
                        _LOGGER.debug("查询设备 %s 失败", dev.did)
                        continue
                else:
                    _LOGGER.debug("查询设备 %s 失败", dev.did)
                    continue

            if not isinstance(result, dict) or result.get("error"):
                continue

            as_data = result.get("as", {})
            if isinstance(as_data, dict) and as_data:
                dev._raw["as"] = as_data
                dev.attributes["as"] = as_data
                dev.refresh()
                _LOGGER.info(
                    "设备状态更新: %s power=%s temp=%s mode=%s",
                    dev.name, getattr(dev, "is_power_on", None),
                    getattr(dev, "current_temperature", None),
                    getattr(dev, "current_mode", None),
                )

    def _query_device_lan(self, dev: WanjialeDevice) -> Dict[str, Any]:
        """通过 LAN 查询设备状态（云连接不可用时的回退方案）。"""
        if not dev.is_lan_available():
            return {"error": "no LAN"}
        try:
            if not getattr(self._protocol, "_local_socket", None):
                success = self._protocol.connect_local(
                    dev.local_host, dev.local_port, dev.lan_pin,
                )
                if not success:
                    return {"error": "lan auth failed"}
            return self._protocol.query_local_device(dev.did, timeout=3)
        except Exception:
            self._protocol.close_local()
            return {"error": "lan query failed"}

    def _query_device_cloud(self, dev: WanjialeDevice) -> Dict[str, Any]:
        """通过云端长连接查询设备状态。"""
        if not getattr(self._protocol, "_socket", None):
            return {"error": "no cloud socket"}
        return self._protocol.query_device(dev.did, timeout=5)

    async def async_refresh_all(self) -> None:
        """异步刷新（HA coordinator 调用）。"""
        import asyncio
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.refresh_all)
        except Exception:
            _LOGGER.exception("async_refresh_all 失败")

    def _apply_device_list(self, raw_list: List[Dict[str, Any]]) -> None:
        did_to_device = {dev.did: dev for dev in self._devices}
        for raw in raw_list:
            did = str(raw.get("did") or "")
            dev = did_to_device.get(did)
            if dev is not None:
                dev._raw = raw
                dev.name = str(raw.get("name") or dev.name)
                dev.online = bool(raw.get("online"))
                dev.model = str(raw.get("model") or dev.model)
                dev.refresh()
            else:
                _LOGGER.info("发现新设备: %s", did)

    def connect_server(self) -> bool:
        return self._protocol.connect_server()

    def close_server(self) -> None:
        self._protocol.close_server()

    def reconnect(self) -> bool:
        """断线重连。"""
        self.close_server()
        return self.connect_server()

    def get_device_by_did(self, did: str) -> Optional[WanjialeDevice]:
        for dev in self._devices:
            if dev.did == did:
                return dev
        return None

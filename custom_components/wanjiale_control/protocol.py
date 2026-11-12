"""万家乐私有 TCP 协议实现。

数据包格式：
    AA BB | msgType | bArr2_len | checksum | enc_len_high | enc_len_low | bArr2(明文) | encrypted_body

加密体结构（加密前）：
    serial_high | serial_low | bArr

加密方式：
    AES/CBC/PKCS5Padding
    key = password_md5[0:16]
    iv = password_md5[16:32]

消息类型：
    9  - 登录
    10 - 连接长连接服务器
    8  - 局域网认证
    17 - 业务消息/控制命令
    11 - 心跳响应
    2  - 登录响应
    3  - 连接响应

局域网控制：
    - 加密密钥 = lanPin + lanPin
    - 先发送认证消息，认证通过后发送业务消息（JSON 控制命令）

JSON 控制命令格式：
    {"to":"did","cmd":"opt","mid":"xxx","as":{"dvid":"value"}}
"""
from __future__ import annotations

import hashlib
import json
import logging
import random
import socket
import struct
import threading
import time
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

try:
    from Crypto.Cipher import AES
    from Crypto.Util.Padding import pad, unpad
except ImportError:  # pragma: no cover - 部分 Windows 环境包目录为小写
    try:
        import sys
        import importlib
        # 在包查找失败时临时注册兼容名
        sys.modules.setdefault("Crypto", importlib.import_module("crypto"))
        from Crypto.Cipher import AES
        from Crypto.Util.Padding import pad, unpad
    except Exception:
        AES = None  # type: ignore
        pad = None  # type: ignore
        unpad = None  # type: ignore

_LOGGER = logging.getLogger(__name__)


def _short_did(did: Any) -> str:
    """日志用的设备标识：只留末 4 位（日志常被用户整段贴出来）。"""
    text = str(did or "")
    return f"***{text[-4:]}" if len(text) > 4 else "***"


# ---------- 服务器配置 ----------
SERVER_HOST = "unls2.machtalk.net"
SERVER_PORT = 26779
HTTP_BASE_URL = "http://newapi.machtalk.net/v2.0"

PLATFORM_ID = 8
APP_ID = 8

# 消息类型
MSG_TYPE_LOGIN = 9
MSG_TYPE_CONNECT = 10
MSG_TYPE_LOCAL_LOGIN = 8
MSG_TYPE_BUSINESS = 17
MSG_TYPE_HEART = 11  # login/connect 等服务器响应类型
MSG_TYPE_HEARTBEAT = 1  # AA BB 01 心跳类型
MSG_TYPE_BROADCAST = 6  # UDP 局域网设备发现
BROADCAST_PORT = 7680
LOCAL_PORT = 7681

USER_TYPE_NORMAL = 1

# 数据包魔数
PACKET_MAGIC = bytes([0xAA, 0xBB])


# ======================================================================
# 工具函数
# ======================================================================
def md5_hash(s: str) -> str:
    """计算字符串 MD5（32位小写 hex）。"""
    return hashlib.md5(s.encode("utf-8")).hexdigest()


def _aes_cbc_encrypt(plain: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 加密，PKCS5/PKCS7 填充。"""
    if AES is None:
        raise RuntimeError("pycryptodome is not installed")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    padded = pad(plain, AES.block_size, style="pkcs7")
    return cipher.encrypt(padded)


def _aes_cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    """AES-CBC 解密，移除 PKCS5/PKCS7 填充。"""
    if AES is None:
        raise RuntimeError("pycryptodome is not installed")
    cipher = AES.new(key, AES.MODE_CBC, iv)
    decrypted = cipher.decrypt(ciphertext)
    return unpad(decrypted, AES.block_size, style="pkcs7")


def _get_short(b: bytes, offset: int) -> int:
    """从字节数组读取大端 16 位整数。"""
    return ((b[offset] & 0xFF) << 8) | (b[offset + 1] & 0xFF)


# ======================================================================
# 数据包构造（还原 Util.makeData）
# ======================================================================
def make_data(
    msg_type: int,
    serial: int,
    bArr: bytes,  # 要加密的数据体
    bArr2: bytes,  # 明文头部数据
    password_md5: str,
) -> bytes:
    """构造完整数据包。

    结构：
        header[7]: AA BB | msgType | bArr2_len | checksum | enc_len_high | enc_len_low
        bArr2: 明文数据
        encrypted: AES 加密后的 (serial + bArr)
    """
    bArr2_len = len(bArr2) if bArr2 else 0
    bArr_len = len(bArr) if bArr else 0

    # 构造加密体：serial(2字节) + bArr
    enc_payload = bytearray()
    enc_payload.append((serial >> 8) & 0xFF)
    enc_payload.append(serial & 0xFF)
    if bArr:
        enc_payload.extend(bArr)
    enc_payload = bytes(enc_payload)

    # AES 加密（key/iv 为 password_md5 hex 字符串的前/后 16 字符）
    key = password_md5[:16].encode("utf-8")
    iv = password_md5[16:32].encode("utf-8")
    encrypted = _aes_cbc_encrypt(enc_payload, key, iv)

    # 构造头部
    header = bytearray(7)
    header[0] = 0xAA  # -86
    header[1] = 0xBB  # -69
    header[2] = msg_type & 0xFF
    header[3] = bArr2_len & 0xFF
    enc_len = len(encrypted)
    header[5] = (enc_len >> 8) & 0xFF
    header[6] = enc_len & 0xFF

    # checksum = enc_len_high + enc_len_low + sum(bArr2) + sum(enc_payload_raw)
    checksum = (header[5] & 0xFF) + (header[6] & 0xFF)
    if bArr2:
        for b in bArr2:
            checksum += b & 0xFF
    for b in enc_payload:
        checksum += b & 0xFF
    header[4] = checksum & 0xFF

    # 组装完整数据包
    result = bytearray()
    result.extend(header)
    if bArr2:
        result.extend(bArr2)
    result.extend(encrypted)
    return bytes(result)


def make_data_for_local(msg_type: int, bArr: bytes) -> bytes:
    """构造局域网数据包（不加密）。

    结构：
        AA BB | msgType | bArr_len | checksum | 0 | 0 | bArr
    """
    bArr_len = len(bArr) if bArr else 0

    header = bytearray(7)
    header[0] = 0xAA
    header[1] = 0xBB
    header[2] = msg_type & 0xFF
    header[3] = bArr_len & 0xFF
    header[5] = 0
    header[6] = 0

    # 校验和只累计 bArr 字节，不含头部字段
    checksum = 0
    if bArr:
        for b in bArr:
            checksum += b & 0xFF
    header[4] = checksum & 0xFF

    result = bytearray()
    result.extend(header)
    if bArr:
        result.extend(bArr)
    return bytes(result)


# ======================================================================
# 登录消息构造
# ======================================================================
def build_login_bArr2(username: str, user_type: int = USER_TYPE_NORMAL) -> bytes:
    """构造登录消息的明文部分 bArr2。

    结构：userType | platformId_high | platformId_low | loginName_bytes
    """
    name_bytes = username.encode("utf-8")
    arr = bytearray()
    arr.append(user_type & 0xFF)
    arr.append((PLATFORM_ID >> 8) & 0xFF)
    arr.append(PLATFORM_ID & 0xFF)
    arr.extend(name_bytes)
    return bytes(arr)


def build_login_bArr(username: str, imei: str = "") -> bytes:
    """构造登录消息的加密体部分 bArr（不含 serial）。

    结构：
        loginName_len | loginName_bytes | 01 00 00 00 01 01 | imei_len | imei_bytes | 00 00 00 | appId | 01
    """
    name_bytes = username.encode("utf-8")
    imei_bytes = imei.encode("utf-8") if imei else b""
    name_len = len(name_bytes)
    imei_len = len(imei_bytes)

    arr = bytearray()
    arr.append(name_len & 0xFF)
    arr.extend(name_bytes)
    arr.extend(bytes([0x01, 0x00, 0x00, 0x00, 0x01, 0x01]))
    arr.append(imei_len & 0xFF)
    if imei_bytes:
        arr.extend(imei_bytes)
    arr.extend([0x00, 0x00, 0x00])
    arr.append(APP_ID & 0xFF)
    arr.append(0x01)
    return bytes(arr)


# ======================================================================
# 局域网认证消息构造
# ======================================================================
def build_local_login_packet(serial: int, lan_pin: str) -> bytes:
    """构造局域网认证数据包。

    随机挑战值取 2 字节：
        明文头   = {challenge_high, challenge_low}
        加密体   = {challenge_high, challenge_low, 0, 10}

    msgType = 8，加密密钥 = lanPin + lanPin
    """
    random_val = random.randint(0, 65535)
    bArr = bytes([(random_val >> 8) & 0xFF, random_val & 0xFF, 0x00, 0x0A])  # 加密体
    bArr2 = bytes([(random_val >> 8) & 0xFF, random_val & 0xFF])  # 明文头

    full_lan_pin = lan_pin + lan_pin  # 32位密钥
    return make_data(MSG_TYPE_LOCAL_LOGIN, serial, bArr, bArr2, full_lan_pin)


# ======================================================================
# 业务消息构造
# ======================================================================
def build_business_packet(serial: int, json_str: str, encrypt_key: str) -> bytes:
    """构造业务消息数据包。

    msgType = 17
    bArr = JSON字符串bytes
    bArr2 = null
    """
    json_bytes = json_str.encode("utf-8")
    return make_data(MSG_TYPE_BUSINESS, serial, json_bytes, None, encrypt_key)


# ======================================================================
# 「写数据点」报文构造：单键 / 多键的唯一判定点
# ======================================================================
class LocalTransportUnsupported(RuntimeError):
    """该场景下局域网不支持本报文，调用方应改用云端。

    目前只有一种情形：老设备 + 多键（见 build_opt_message）。
    """


# `acts` 消息族用于菜谱 / 烹饪类设备，与具名动作命令族互斥。
# 本集成暂无菜谱类品类，这里只预留枚举值。
MSG_CMD_ACTS = ("acts", "actsStart", "actsStop", "actsPause", "actsStep", "actsOpt")


def build_opt_message(
    did: str,
    values: Dict[str, Any],
    mid: str,
    *,
    old_device: bool = False,
) -> str:
    """构造「写数据点（cmd=opt）」的 JSON 报文。

    单键还是多键只在这里判定：
      - 老设备走局域网：只支持单键，报文为 {"to","cmd","mid","dvid","value"}
      - 其它情况（含老设备走云端）：多键，报文为 {"to","cmd","mid","as":{…}}

    Raises:
        ValueError: 点位值为空。
        LocalTransportUnsupported: 老设备 + 多键。设备只认第 1 个键，与其静默丢键，
            不如让调用方改用云端。
    """
    if not values:
        raise ValueError("build_opt_message: 点位值为空，拒绝下发")

    if old_device:
        if len(values) > 1:
            raise LocalTransportUnsupported(
                "老设备 + 局域网只支持单键报文，当前 "
                f"{len(values)} 个键（{', '.join(str(k) for k in values)}）"
            )
        dvid, value = next(iter(values.items()))
        payload: Dict[str, Any] = {
            "to": did, "cmd": "opt", "mid": mid, "dvid": dvid, "value": value,
        }
    else:
        payload = {"to": did, "cmd": "opt", "mid": mid, "as": values}

    return json.dumps(payload, separators=(",", ":"))


# ======================================================================
# 登录响应解析
# ======================================================================
class CredentialError(RuntimeError):
    """登录响应表明凭据无效（密码错误 / 账号异常）。

    判定按抛出位置与异常类型，不依赖服务端文案：
      - 加密体长度为 0（服务器返回空加密数据）；
      - AES 解密抛 `ValueError`（padding 校验失败，密码派生 key/iv 不匹配）；
      - 解密后 uid 为空。
    继承 RuntimeError，对既有 `except RuntimeError` / `except Exception` 调用方零影响。
    """


def parse_login_response(
    raw: bytes,
    password_md5: str,
) -> Dict[str, Any]:
    """解析登录响应（偏移均相对解密后数据）。

        [0:2]     本机发出的序号
        [2:4]     序号回显
        [4:36]    session_key (32 bytes)
        [36:68]   uid (32 bytes)
        [68:100]  api_key (32 bytes)
        [100:104] server_ip (4 bytes)
        [104:106] server_port (大端)
    """
    if len(raw) < 7:
        _LOGGER.error("响应头长度不足: 只有 %d 字节", len(raw))
        raise ValueError(f"响应头长度不足: {len(raw)} < 7")

    msg_type = raw[2] & 0xFF
    bArr2_len = raw[3] & 0xFF
    enc_len = _get_short(raw, 5)

    _LOGGER.debug(
        "响应头: msg_type=%d, bArr2_len=%d, enc_len=%d, header_hex=%s",
        msg_type, bArr2_len, enc_len, raw[:7].hex(),
    )

    enc_start = 7 + bArr2_len
    if len(raw) < enc_start + enc_len:
        _LOGGER.error(
            "响应体长度不足: raw=%d expected=%d enc_len=%d",
            len(raw), enc_start + enc_len, enc_len,
        )
        raise ValueError(f"响应体长度不足: {len(raw)} < {enc_start + enc_len}")

    encrypted = raw[enc_start : enc_start + enc_len]

    if len(encrypted) == 0:
        _LOGGER.error(
            "加密体为空！enc_len=%d bArr2_len=%d",
            enc_len, bArr2_len,
        )
        raise CredentialError("服务器返回空加密数据，请检查账号密码是否正确")

    # 解密（key/iv 为 password_md5 hex 字符串的前/后 16 字符）
    key = password_md5[:16].encode("utf-8")
    iv = password_md5[16:32].encode("utf-8")
    try:
        decrypted = _aes_cbc_decrypt(encrypted, key, iv)
    except ValueError as err:
        # 密码派生 key/iv 不匹配时 padding 校验失败——这是「密码错误」最常见的形态，
        # 不能当瞬时故障重试，否则每次都会白试一轮。
        _LOGGER.debug("登录响应解密失败（padding 错误），按凭据错误处理")
        raise CredentialError("登录响应解密失败，请检查账号密码是否正确") from err

    _LOGGER.debug("登录响应解密成功: %d 字节", len(decrypted))

    if len(decrypted) < 106:
        _LOGGER.error(
            "解密后数据长度不足: %d < 106",
            len(decrypted),
        )
        raise ValueError(f"解密后数据长度不足: {len(decrypted)} < 106")

    result: Dict[str, Any] = {}
    result["serial"] = _get_short(decrypted, 0)

    # random_prefix: [2:4] (2 bytes, skip)
    # session_key: [4:36]
    session_key_bytes = decrypted[4:36]
    result["session_key"] = session_key_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")

    # uid: [36:68]
    uid_bytes = decrypted[36:68]
    result["uid"] = uid_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")

    # api_key: [68:100]
    api_key_bytes = decrypted[68:100]
    result["api_key"] = api_key_bytes.split(b"\x00", 1)[0].decode("utf-8", errors="ignore")

    # server_ip: [100:104]
    ip_bytes = decrypted[100:104]
    result["server_ip"] = f"{ip_bytes[0]&0xFF}.{ip_bytes[1]&0xFF}.{ip_bytes[2]&0xFF}.{ip_bytes[3]&0xFF}"

    # server_port: [104:106]
    result["server_port"] = _get_short(decrypted, 104)

    if not result["uid"]:
        raise CredentialError("未能从响应中解析出 uid，可能密码错误")

    return result


# ======================================================================
# 局域网认证响应解析
# ======================================================================
def parse_local_login_response(raw: bytes, full_lan_pin: str) -> bool:
    """解析局域网认证响应；result_code == 0 即认证成功。

    响应帧为完整数据包格式（7 字节头 + 明文段 + 加密段），两种形态：
      - enc_len == 0：result_code 取明文段前 2 字节，即 raw[7:9]；
      - enc_len != 0：解密后取 decrypted[2:4]（前 2 字节视为序号回显）。
        该分支尚无实测样本，遇到带加密体的设备时需先核对偏移。
    """
    if raw is None or len(raw) < 9:
        return False
    if raw[0] != 0xAA or raw[1] != 0xBB:
        return False

    plaintext_len = raw[3] & 0xFF
    enc_len = _get_short(raw, 5)

    if len(raw) < 7 + plaintext_len + enc_len:
        return False

    _LOGGER.debug(
        "局域网认证响应解析: msg_type=%d 明文=%d 加密=%d 密钥长度=%d",
        raw[2] & 0xFF, plaintext_len, enc_len, len(full_lan_pin),
    )

    # enc_len == 0：无加密体，result_code 直接从明文读取
    if enc_len == 0:
        if plaintext_len < 2:
            return False
        result_code = _get_short(raw, 7)  # 跳过 7 字节 header
        return result_code == 0

    # 有加密体：解密后读取
    encrypted = raw[7 + plaintext_len : 7 + plaintext_len + enc_len]
    _LOGGER.debug("局域网认证加密体: %d 字节", len(encrypted))

    key = full_lan_pin[:16].encode("utf-8")
    iv = full_lan_pin[16:32].encode("utf-8")

    try:
        decrypted = _aes_cbc_decrypt(encrypted, key, iv)
        _LOGGER.debug("局域网认证解密成功: %d 字节", len(decrypted))
    except Exception:
        _LOGGER.debug("局域网认证解密失败", exc_info=True)
        return False

    if len(decrypted) < 4:
        return False

    result_code = _get_short(decrypted, 2)  # 跳过 2 字节 serial
    return result_code == 0


# ======================================================================
# HTTP 设备列表 / 签名
# ======================================================================
# 服务端「会话失效，请重新登录」类错误码（来源：error_codes.properties）
# 10144=鉴权失败；10182 / 20015 / 00120009=会话已过期。
# 10141 / 10142 / 10143 / 10148（缺或错 APIKey、timestamp、key）属请求构造问题，
# 不在本集合内：重登只会掩盖实现缺陷，仍按普通错误抛出。
AUTH_FAILED_CODES = frozenset({"10144", "10182", "20015", "00120009"})

# 心跳应答 ACK 读取预算。抓包实测应答 55~65ms（capture_xintiao.pcap
# 帧3109→3113 等），2s 余量充足；也是云端探测段三持锁读的最坏增量。
CLOUD_HEARTBEAT_ACK_TIMEOUT = 2.0
# 发心跳前排空积压的预算：现网实测空闲期 Recv-Q 可达 3125B。
# 帧数上限按最坏构成取值：3125B 若全是 11B 的心跳 ACK ≈ 284 帧，
# 200 帧会漏排 → 首轮「伪活」。提到 400 覆盖之；真正的上界是 0.5s 墙钟
# （读已缓冲数据是毫秒级，不增加锁占用）；积压仍超出时留待下一轮。
CLOUD_RX_DRAIN_TIMEOUT = 0.2
CLOUD_RX_DRAIN_MAX_FRAMES = 400
CLOUD_RX_DRAIN_MAX_SECONDS = 0.5


class SessionExpiredError(RuntimeError):
    """服务端判定 uid/api_key 已失效，需要重新登录。

    继承 RuntimeError，对现有 `except RuntimeError` / `except Exception` 调用方向后兼容。
    """

    def __init__(self, code: str, payload: Any = None) -> None:
        super().__init__(f"会话已失效（code={code}）")
        self.code = code
        self.payload = payload


def _raise_if_auth_failed(data: Any) -> None:
    """响应中带鉴权类错误码时抛 `SessionExpiredError`（兼容 int / str 两种 code）。"""
    if not isinstance(data, dict) or data.get("code") is None:
        return
    code = str(data["code"])
    if code in AUTH_FAILED_CODES:
        _LOGGER.debug("设备列表鉴权失败: code=%s", code)
        raise SessionExpiredError(code, data)


def build_auth_headers(uid: str, api_key: str) -> Dict[str, str]:
    """构建 HTTP 请求头（ts/uid/key 签名）。

    不要显式设置 Host：部分网络环境下显式指定 Host 会导致 /app/* 返回 502，
    交给 requests/aiohttp 自动补全即可。
    """
    ts = str(int(time.time() * 1000))
    key = md5_hash(api_key + ts)
    return {
        "ts": ts,
        "uid": uid,
        "key": key,
        "User-Agent": "okhttp/3.8.1",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip",
    }


# ======================================================================
# 业务响应形态归一化
# ======================================================================
def normalize_fids(value: Any) -> list[str]:
    """把 `fids`（云端故障列表）归一化为字符串数组。

    老设备下发逗号分隔字符串（如 "1,2,3"），新设备下发数组。
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def normalize_business_payload(data: Any) -> Any:
    """把业务响应统一成「`as` 字典 + `fids` 数组」形态。

    老设备用 `values: [{"dvid","value"}]` 代替 `as`，取值统一转成字符串。

    按形态特征而非设备代际判断：新设备不会「缺 as 又带 values」，
    因此这一判据与设备代际等价，传输层不必追踪设备类型。
    """
    if not isinstance(data, dict):
        return data

    if "as" not in data:
        values = data.get("values")
        if isinstance(values, list):
            as_dict: Dict[str, str] = {}
            for item in values:
                if isinstance(item, dict) and item.get("dvid") is not None:
                    as_dict[str(item["dvid"])] = str(item.get("value", ""))
            if as_dict:
                data.pop("values", None)
                data["as"] = as_dict

    if "fids" in data:
        data["fids"] = normalize_fids(data.get("fids"))

    return data


# ======================================================================
# 局域网链路（按设备）
# ======================================================================
class DiscoveredDevice(NamedTuple):
    """UDP 广播发现到的一台设备。"""

    did: str
    ip: str
    model: str
    lan_supported: bool


class _LocalLink:
    """一台设备的局域网链路：socket + 认证 lanPin + 心跳线程。

    字段都只服务这台设备，不从其它设备复用。
    """

    __slots__ = ("did", "lan_pin", "socket", "lock", "stop_event", "heartbeat_thread")

    def __init__(self, did: str, lan_pin: str) -> None:
        self.did = did
        self.lan_pin = lan_pin
        self.socket: Optional[socket.socket] = None
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.heartbeat_thread: Optional[threading.Thread] = None


def parse_broadcast_reply(data: bytes, ip: str) -> Optional[DiscoveredDevice]:
    """解析 UDP 广播应答，取出设备标识（did）与型号。

    设备有两种应答形式：
    1. 裸 JSON（不带本地帧头），形如 {"cmd":"lanDevInfo","did":…}，
       出现即说明该设备可走局域网。
    2. AA BB 本地帧（7 字节头 + payload），payload 结构：
        payload[0]             = didLen（0 = 设备尚未授权）
        payload[1 : 1+didLen]  = did
        payload[1+didLen]      = modelLen
        payload[2+didLen : …]  = model
        其后                    = 数据格式(0=JSON/1=二进制)、是否支持局域网控制、配网状态等
    """
    if not data:
        return None

    # 形式 1：裸 JSON（先判，因为它不以 AA BB 开头）
    if data[:1] == b"{":
        try:
            obj = json.loads(data.decode("utf-8", errors="ignore"))
        except Exception:  # noqa: BLE001
            return None
        did = str(obj.get("did") or "")
        if not did:
            return None
        return DiscoveredDevice(did=did, ip=ip, model="", lan_supported=True)

    # 形式 2：AA BB 本地帧
    if len(data) < 3 or data[0] != 0xAA or data[1] != 0xBB:
        return None
    payload = data[7:]  # 跳过本地帧头
    if not payload:
        return None

    if len(payload) < 10:
        _LOGGER.debug("广播应答过短（%d 字节），忽略", len(payload))
        return None
    did_len = payload[0] & 0xFF
    if did_len == 0 or len(payload) < did_len + 1:
        _LOGGER.debug("广播应答未携带 did（设备尚未授权），忽略")
        return None

    did = payload[1 : 1 + did_len].decode("utf-8", errors="ignore")
    pos = 1 + did_len
    model = ""
    model_len = payload[pos] & 0xFF
    pos += 1
    model = payload[pos : pos + model_len].decode("utf-8", errors="ignore")
    pos += model_len

    # payload[pos] = 数据格式，payload[pos+1] = 是否支持局域网控制
    lan_supported = pos + 1 < len(payload) and (payload[pos + 1] & 0xFF) == 1
    return DiscoveredDevice(did=did, ip=ip, model=model, lan_supported=lan_supported)


# ======================================================================
# 高层封装：WanjialeProtocol
# ======================================================================
class WanjialeProtocol:
    """万家乐协议客户端。

    支持两种控制方式：
    1. 云端控制：通过长连接服务器转发
    2. 局域网控制：直连设备本地端口

    使用方式：
        proto = WanjialeProtocol(username, password)
        proto.login()                 # TCP 登录，获取 uid/api_key
        devices = proto.get_devices() # HTTP 拉设备列表

        # 云端控制
        proto.connect_server()        # 连接长连接服务器
        proto.send_control(did, {"2": "71045130"})  # 发送控制命令

        # 局域网控制（需要设备的 lanPin；连接按 did 各自维护，互不干扰）
        proto.connect_local(did, device_ip, device_port, lan_pin)  # 局域网认证
        proto.send_local_control(did, {"2": "71045130"})           # 局域网控制
    """

    def __init__(
        self,
        username: str,
        password: str,
        imei: str = "",
        host: str = SERVER_HOST,
        port: int = SERVER_PORT,
        timeout: float = 10.0,
        http_base_url: str = HTTP_BASE_URL,
        http_timeout: float = 3.0,
    ) -> None:
        self.username = username
        self.password = password
        self._password_md5 = md5_hash(password)
        self.imei = imei
        self.host = host
        self.port = port
        self.timeout = timeout
        self.http_base_url = http_base_url
        self.http_timeout = http_timeout

        # 登录结果
        self.uid: Optional[str] = None
        self.api_key: Optional[str] = None
        self.session_key: Optional[str] = None
        self.server_ip: Optional[str] = None
        self.server_port: Optional[int] = None

        # 连接状态
        self._socket: Optional[socket.socket] = None
        self._serial = random.randint(0, 9999)
        self._last_heartbeat = 0.0
        # 最近一次收到完整帧（握手应答/心跳应答/业务帧）的时刻：僵死 socket 检测依据。
        # send 基准的 _last_heartbeat 检测不到「服务端半关连接/静默丢链」——
        # sendall 仍能成功写入内核缓冲，只有 recv 侧才能证明链路活着。
        self._last_rx_at: float = 0.0
        self._heartbeat_interval = 40
        self._lock = threading.RLock()
        # 云端通道不可用的“首次”标记：网络长期不通时只在进入失败态时提示一次
        self._cloud_down: bool = False

        # 会话恢复：relogin 节流时间戳；_closed 为卸载标记，必须在 _lock 内检查，
        # 否则"检查后卸载"仍会留下无人关闭的云端长连接。
        self._last_relogin: float = 0.0
        self._closed: bool = False

        # 局域网连接按 did 独立维护：多设备共用一份 socket / lanPin 会误发。
        self._local_links: Dict[str, "_LocalLink"] = {}
        self._local_links_lock = threading.RLock()

        # 局域网心跳间隔。设备端空闲会关闭链路，40s 间隔保不住连接，
        # 这里与客户端本地连接的 setHeartbeatTime(10000) 保持一致。
        self._lan_heartbeat_interval: float = 10.0

    def _next_serial(self) -> int:
        """获取下一个序列号。"""
        self._serial = (self._serial + 1) % 65536
        return self._serial

    # ---- TCP 登录 ----
    def login(self, quiet: bool = False) -> Dict[str, Any]:
        """登录并获取 uid/api_key。

        Args:
            quiet: 为 True 时把「登录成功」从 INFO 降为 DEBUG（自动重登用，
                避免最坏路径额外产生 3 条 INFO 污染日志）；默认行为不变。
        """
        bArr2 = build_login_bArr2(self.username, USER_TYPE_NORMAL)  # 明文部分
        bArr = build_login_bArr(self.username, self.imei)  # 加密体部分
        serial = self._next_serial()
        frame = make_data(MSG_TYPE_LOGIN, serial, bArr, bArr2, self._password_md5)

        _LOGGER.debug(
            "登录帧: serial=%d 加密体=%d 明文=%d password_md5=***",
            serial, len(bArr), len(bArr2),
        )
        _LOGGER.debug("登录帧构造完成: %d 字节", len(frame))

        _LOGGER.debug("正在连接 %s:%d", self.host, self.port)
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(frame)
            _LOGGER.debug("登录包已发送: %d 字节，等待响应...", len(frame))
            resp = s.recv(4096)

        _LOGGER.debug("收到登录响应: %d 字节", len(resp))

        if not resp:
            raise RuntimeError("服务器返回空响应")

        result = parse_login_response(resp, self._password_md5)

        self.uid = result.get("uid")
        self.api_key = result.get("api_key")
        self.session_key = result.get("session_key")
        self.server_ip = result.get("server_ip")
        self.server_port = result.get("server_port")

        # uid 属账号标识，不打进日志（诊断导出里同样是脱敏的）
        if quiet:
            _LOGGER.debug("登录成功: 服务器=%s:%s", self.server_ip, self.server_port)
        else:
            _LOGGER.info("登录成功: 服务器=%s:%s", self.server_ip, self.server_port)
        return result

    # ---- HTTP 拉设备列表 ----
    def get_devices(self) -> list[Dict[str, Any]]:
        """从 HTTP API 获取设备列表。"""
        if not self.uid or not self.api_key:
            raise RuntimeError("请先调用 login()")

        # 凭据快照：重登时 login() 逐字段赋值，直接读实例属性可能拿到
        # "新 uid + 旧 api_key" 的半更新组合，导致假 10144。
        uid, api_key = self.uid, self.api_key

        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("requests 未安装") from e

        url = f"{self.http_base_url}/app/devices"
        resp = None
        try:
            resp = requests.get(url, headers=build_auth_headers(uid, api_key), timeout=self.http_timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            _LOGGER.debug(
                "HTTP 获取设备列表失败: status=%s body=%s",
                getattr(resp, "status_code", None) if resp is not None else "N/A",
                (getattr(resp, "text", "")[:200]) if resp is not None else "N/A",
            )
            raise
        _raise_if_auth_failed(data)
        if "devs" in data:
            devs: list[Dict[str, Any]] = data["devs"]
            _LOGGER.info("获取到 %d 台设备", len(devs))
            return devs
        raise RuntimeError(f"devices response missing 'devs': {data}")

    async def async_get_devices(self) -> list[Dict[str, Any]]:
        """从 HTTP API 获取设备列表（异步版本，使用 aiohttp）。"""
        if not self.uid or not self.api_key:
            raise RuntimeError("请先调用 login()")

        uid, api_key = self.uid, self.api_key

        import aiohttp

        url = f"{self.http_base_url}/app/devices"
        headers = build_auth_headers(uid, api_key)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        _raise_if_auth_failed(data)
        if "devs" in data:
            devs: list[Dict[str, Any]] = data["devs"]
            _LOGGER.info("获取到 %d 台设备", len(devs))
            return devs
        raise RuntimeError(f"devices response missing 'devs': {data}")

    # ---- 连接长连接服务器 ----
    def connect_server(self) -> bool:
        """连接长连接服务器（用于云端控制）。"""
        if not self.server_ip or not self.server_port or not self.session_key:
            raise RuntimeError("请先调用 login() 获取服务器地址和 token")

        heartbeat = 40
        bArr = bytes([0x00, 0x00, (heartbeat >> 8) & 0xFF, heartbeat & 0xFF])
        bArr2 = self.session_key.encode("utf-8")
        serial = self._next_serial()
        frame = make_data(MSG_TYPE_CONNECT, serial, bArr, bArr2, self._password_md5)

        _LOGGER.debug(
            "connect_server: serial=%d session_key长度=%d password_md5=***",
            serial, len(self.session_key or ""),
        )

        _LOGGER.debug("正在连接长连接服务器 %s:%d", self.server_ip, self.server_port)
        with self._lock:
            # 幂等：已有长连接直接复用，避免「查询回退 / relogin / 轮级探测」三条并发路径
            # 各建一条 socket，后者覆盖前者造成连接泄漏。
            # relogin()/reconnect() 都是「先 close_server() 再 connect_server()」，
            # 因此这里返回 True 不会挡掉重建连接的路径。
            if self._socket is not None:
                return True
            self._socket = socket.create_connection(
                (self.server_ip, self.server_port), timeout=self.timeout
            )
            try:
                self._socket.sendall(frame)
                self._socket.settimeout(3)
                resp = self._recv_frame()
                if resp is None or len(resp) < 7:
                    raise RuntimeError("connect_server: 握手响应无效")
                extra_len = resp[3] & 0xFF
                enc_len = _get_short(resp, 5)
            except Exception:
                self._socket.close()
                self._socket = None
                raise

        _LOGGER.debug("connect_server: 收到握手响应 %d 字节", len(resp))

        encrypted = resp[7 + extra_len : 7 + extra_len + enc_len]

        key = self._password_md5[:16].encode("utf-8")
        iv = self._password_md5[16:32].encode("utf-8")
        decrypted = _aes_cbc_decrypt(encrypted, key, iv)

        _LOGGER.debug("connect_server: 握手响应解密后 %d 字节", len(decrypted))

        if len(decrypted) >= 12:
            result_code = decrypted[11] if len(decrypted) > 11 else 1
            if result_code == 0:
                # 偏移规则与 parse_login_response 一致（响应体在解析前被剥掉 2 字节序号）：
                #   [0:2] 序号  [2:4] 序号回显  [4:6] 心跳秒数(大端)  ...  [11] 状态码
                hb = _get_short(decrypted, 4) if len(decrypted) >= 6 else 0
                if 10 <= hb <= 600:
                    heartbeat = hb
                else:
                    _LOGGER.debug("connect_server: 心跳值异常(%s)，沿用默认 %ds", hb, heartbeat)
                self._heartbeat_interval = heartbeat
                self._last_heartbeat = time.time()
                _LOGGER.info("长连接已建立: 心跳间隔 %ds", heartbeat)
                return True
            _LOGGER.warning("connect_server: 握手返回码=%d", result_code)

        with self._lock:
            self._socket.close()
            self._socket = None
        return False

    def close_server(self) -> None:
        """关闭长连接服务器连接。"""
        with self._lock:
            if self._socket:
                try:
                    self._socket.close()
                except Exception:
                    pass
                self._socket = None

    # ---- 会话恢复 ----
    # 并发路径（设备列表刷新线程 / 未来的其它调用方）之间的最小重登间隔。
    RELOGIN_MIN_INTERVAL = 60.0

    def relogin(self) -> bool:
        """会话失效后重新登录；原本有云端长连接则一并重建。

        Returns:
            True 表示本次确实执行了登录；False 表示被节流或协议已关闭，
            两者都不算失败，调用方不应改变计数/状态。

        Note:
            本方法在 _lock 内执行网络登录与握手（最坏 ~20s），期间云端回退查询
            与云端控制会阻塞等锁；局域网通道不受影响（走每设备独立的 link.lock）。
            触发频率受 api 层（每日 ≤2 次、2h 最小间隔）与 60s 并发节流三重限制。
        """
        with self._lock:
            if self._closed:
                return False
            now = time.monotonic()
            if now - self._last_relogin < self.RELOGIN_MIN_INTERVAL:
                return False
            self._last_relogin = now

            had_cloud = self._socket is not None
            self.close_server()      # 旧 session_key 已作废，先断开再重新登录
            self.login(quiet=True)   # 失败时异常向上抛，由调用方统一计数
            if had_cloud:
                try:
                    if self.connect_server():
                        self._cloud_down = False   # 复位"云端不可用"的一次性日志状态
                except Exception:  # noqa: BLE001 - 云端重建失败不影响 HTTP 侧已恢复
                    _LOGGER.warning("重新登录后长连接重建失败", exc_info=True)
            return True

    def close(self) -> None:
        """卸载时调用：先置关闭标记，再释放云端/局域网连接。"""
        with self._lock:
            self._closed = True
            self.close_server()
        self.close_local()

    # ---- 云端控制 ----
    def send_control(self, did: str, as_dict: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """通过云端发送控制命令（同步等待响应）。"""
        if timeout is None:
            timeout = self.timeout

        mid = str(int(time.time() * 1000))
        json_str = build_opt_message(did, as_dict, mid)

        serial = self._next_serial()
        frame = build_business_packet(serial, json_str, self._password_md5)

        _LOGGER.debug("云端下发控制 %s: %s", _short_did(did), json_str)

        with self._lock:
            self._ensure_connected()
            if not self._socket:
                raise RuntimeError("send_control: 无可用长连接")
            self._socket.sendall(frame)
            self._last_heartbeat = time.time()

            start_time = time.time()
            read_error_logged = False
            while time.time() - start_time < timeout:
                remaining = timeout - (time.time() - start_time)
                self._socket.settimeout(max(remaining, 1.0))
                try:
                    raw = self._recv_frame()
                    if raw is None:
                        continue
                    if len(raw) == 3 and raw[2] == MSG_TYPE_HEARTBEAT:
                        continue
                    result = self._parse_business_response(raw, self._password_md5)
                    resp_mid = (result or {}).get("mid") if isinstance(result, dict) else None
                    if resp_mid == mid:
                        return result
                except ConnectionError:
                    # 与 query_device 同口径：流损坏即关闭连接并返回错误，
                    # 避免死 socket 残留 / 持锁忙等。
                    _LOGGER.debug("send_control: 长连接流异常，关闭连接")
                    self.close_server()
                    return {"error": "connection closed"}
                except socket.timeout:
                    continue
                except Exception:
                    # 只报第一次：同一轮里同样的错会被反复撞到，堆栈留一份即可
                    if not read_error_logged:
                        read_error_logged = True
                        _LOGGER.warning("send_control: 读取响应异常", exc_info=True)
                    continue

        raise TimeoutError(f"send_control 超时: mid={mid}")

    def send_control_async(self, did: str, as_dict: Dict[str, Any]) -> Dict[str, Any]:
        """通过云端发送控制命令（不等待响应）。

        发送后立即返回，状态确认交给定时轮询的 coordinator。
        """
        mid = str(int(time.time() * 1000))
        json_str = build_opt_message(did, as_dict, mid)

        serial = self._next_serial()
        frame = build_business_packet(serial, json_str, self._password_md5)

        _LOGGER.debug("云端异步下发控制 %s: %s", _short_did(did), json_str)

        with self._lock:
            self._ensure_connected()
            if not self._socket:
                raise RuntimeError("send_control_async: 无可用长连接")
            self._socket.sendall(frame)
            self._last_heartbeat = time.time()

        return {"status": "sent", "mid": mid}

    # ---- UDP 局域网设备发现 ----
    def discover_devices(self, timeout: float = 3.0) -> List[DiscoveredDevice]:
        """通过 UDP 广播发现局域网设备，收集全部应答直到超时。

        广播端口 7680，payload 为 {len=0, timestamp[4], 0, 0}。
        多设备下必须逐台识别，因此这里收全部应答，由调用方按 did 回填地址。

        Returns:
            发现的设备列表（可能为空）。解析失败或重复的应答被丢弃。
        """
        sock: Optional[socket.socket] = None
        found: List[DiscoveredDevice] = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.settimeout(timeout)

            # payload: 1字节len=0 + 4字节timestamp + 2字节0x00
            ts = int(time.time())
            bArr = bytes([
                0x00,
                (ts >> 24) & 0xFF, (ts >> 16) & 0xFF,
                (ts >> 8) & 0xFF, ts & 0xFF,
                0x00, 0x00,
            ])
            frame = make_data_for_local(MSG_TYPE_BROADCAST, bArr)
            sock.sendto(frame, ("255.255.255.255", BROADCAST_PORT))
            _LOGGER.debug("discover_devices: 已发送 UDP 广播, ts=%d", ts)

            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    break
                item = parse_broadcast_reply(data, addr[0])
                if item is None:
                    continue
                if any(existing.did == item.did for existing in found):
                    continue  # 同一台设备重复应答
                found.append(item)
                _LOGGER.info(
                    "discover_devices: 发现 %s（%s）→ %s",
                    _short_did(item.did), item.model or "未知型号", item.ip,
                )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("discover_devices: UDP 广播失败：%s", err, exc_info=True)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:  # noqa: BLE001
                    pass
        if not found:
            _LOGGER.debug("discover_devices: 未发现设备（可能不在同一子网）")
        return found

    # ---- 局域网连接与认证 ----
    def local_link(self, did: str) -> Optional["_LocalLink"]:
        """取该设备已连接的局域网链路；未连接返回 None。"""
        with self._local_links_lock:
            link = self._local_links.get(did)
        if link is None:
            return None
        with link.lock:
            return link if link.socket is not None else None

    def is_local_connected(self, did: str) -> bool:
        """该设备是否已有可用的局域网连接。"""
        return self.local_link(did) is not None

    def connect_local(
        self,
        did: str,
        ip: str,
        port: int,
        lan_pin: str,
        timeout: Optional[float] = None,
    ) -> bool:
        """连接指定设备的局域网端口并认证，连接按 did 独立维护。

        timeout 为「TCP 连接 + 认证应答读取」的总预算，缺省用实例级 self.timeout。
        查询路径会传入轮询预算，避免设备不可达时把单轮耗时拖满默认超时。
        """
        connect_timeout = self.timeout if timeout is None else max(float(timeout), 0.05)
        with self._local_links_lock:
            if self.local_link(did) is not None:
                return True  # 已有可用连接，直接复用
            link = _LocalLink(did=did, lan_pin=lan_pin)
            self._local_links[did] = link

        _LOGGER.debug("正在连接局域网设备 %s（%s:%d）", _short_did(did), ip, port)
        full_lan_pin = lan_pin + lan_pin

        try:
            sock = socket.create_connection((ip, port), timeout=connect_timeout)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("局域网连接 %s:%d 失败：%s", ip, port, err, exc_info=True)
            self.close_local(did)
            return False

        with link.lock:
            link.socket = sock

        # 发送局域网认证消息
        try:
            serial = self._next_serial()
            frame = build_local_login_packet(serial, lan_pin)
            _LOGGER.debug("局域网认证帧已发送: %d 字节", len(frame))
            sock.sendall(frame)

            # 读取响应（帧格式）；按同一预算收敛，避免认证无响应时长时间挂住
            sock.settimeout(connect_timeout)
            raw = self._recv_local_frame(link, timeout=connect_timeout)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("局域网设备读取异常（%s）：%s", _short_did(did), err, exc_info=True)
            self.close_local(did)
            return False

        if raw is None:
            _LOGGER.warning("局域网设备无响应: %s", _short_did(did))
            self.close_local(did)
            return False

        if not parse_local_login_response(raw, full_lan_pin):
            # 认证失败多为 lanPin 不符或设备拒绝，调用方需要据此判断局域网是否可用
            _LOGGER.warning(
                "局域网认证失败: %s（响应 %d 字节，请核对设备 lanPin）",
                _short_did(did), len(raw),
            )
            self.close_local(did)
            return False

        _LOGGER.info("局域网认证成功: %s", _short_did(did))
        self._start_lan_heartbeat(link)
        return True

    def _recv_local_frame(self, link: "_LocalLink", timeout: Optional[float] = None) -> Optional[bytes]:
        """从该设备的局域网 socket 读取完整帧。

        `timeout` 透传给 `_recv_local_exact`（缺省沿用「至少 3 秒」的宽松下限）。
        """
        try:
            hdr = self._recv_local_exact(link, 7, timeout=timeout)
            if hdr[0] != 0xAA or hdr[1] != 0xBB:
                return None
            plaintext_len = hdr[3] & 0xFF
            enc_len = _get_short(hdr, 5)
            if enc_len > 65535:
                return None
            body = self._recv_local_exact(link, plaintext_len + enc_len, timeout=timeout)
            return hdr + body
        except (socket.timeout, ConnectionError):
            return None

    def _recv_local_exact(self, link: "_LocalLink", n: int, timeout: Optional[float] = None) -> bytes:
        """从该设备的局域网 socket 读取精确 n 字节。

        timeout 为本次读取的总预算，缺省给 3 秒兜底；认证路径会传入更紧的轮询预算。
        """
        if n > 65535:
            raise RuntimeError(f"_recv_local_exact: 请求读取 {n} 字节，超过最大允许值")
        if link.socket is None:
            raise ConnectionError("局域网连接已关闭")
        data = b""
        budget = max(n / 4096.0, 3.0) if timeout is None else max(float(timeout), 0.05)
        deadline = time.time() + budget
        while len(data) < n:
            link.socket.settimeout(max(deadline - time.time(), 0.1))
            chunk = link.socket.recv(n - len(data))
            if not chunk:
                raise ConnectionError("局域网连接已关闭")
            if time.time() > deadline:
                raise socket.timeout(f"_recv_local_exact: 读取超时, 已读 {len(data)}/{n}")
            data += chunk
        return data

    def close_local(self, did: Optional[str] = None) -> None:
        """关闭局域网连接：指定 `did` 只关该设备，不指定则关闭全部。"""
        with self._local_links_lock:
            if did is None:
                links = list(self._local_links.values())
                self._local_links.clear()
            else:
                link = self._local_links.pop(did, None)
                links = [link] if link is not None else []
        if not links:
            return

        _LOGGER.debug("关闭局域网连接: %s", _short_did(did) if did else "全部")
        for link in links:
            link.stop_event.set()
            with link.lock:
                if link.socket is not None:
                    try:
                        link.socket.close()
                    except Exception:  # noqa: BLE001
                        pass
                    link.socket = None
        for link in links:
            thread = link.heartbeat_thread
            if thread is not None and thread.is_alive():
                thread.join(timeout=2.0)
            link.heartbeat_thread = None

    def _start_lan_heartbeat(self, link: "_LocalLink") -> None:
        """为该设备启动局域网心跳守护线程，每 _lan_heartbeat_interval 秒发一次 AA BB 01。"""
        if link.heartbeat_thread is not None and link.heartbeat_thread.is_alive():
            return
        link.stop_event.clear()
        _LOGGER.debug(
            "启动局域网心跳线程（%s），间隔 %d 秒", link.did, self._lan_heartbeat_interval
        )
        link.heartbeat_thread = threading.Thread(
            target=self._lan_heartbeat_loop,
            args=(link,),
            daemon=True,
            name=f"wanjiale-lan-hb-{link.did[-6:]}",
        )
        link.heartbeat_thread.start()

    def _lan_heartbeat_loop(self, link: "_LocalLink") -> None:
        """该设备的局域网心跳循环。

        close_local(did) 通过 link.stop_event.set() 通知退出；
        心跳失败时清理该设备的链路并退出。
        """
        while not link.stop_event.wait(self._lan_heartbeat_interval):
            with link.lock:
                if link.socket is None:
                    return
                try:
                    link.socket.sendall(b"\xAA\xBB\x01")
                except Exception:  # noqa: BLE001
                    _LOGGER.warning("局域网心跳失败，关闭 %s 的局域网连接", _short_did(link.did))
                    try:
                        link.socket.close()
                    except Exception:  # noqa: BLE001
                        pass
                    link.socket = None
                    with self._local_links_lock:
                        self._local_links.pop(link.did, None)
                    return

    # ---- 局域网控制 ----
    def _require_local_link(self, did: str) -> "_LocalLink":
        """取该设备已连接的局域网链路，并校验链路归属。

        按 did 取值时结构上不会取错，这里显式校验是防止将来映射键错位后静默误发。
        """
        link = self.local_link(did)
        if link is None:
            raise RuntimeError(f"设备 {did} 尚未建立局域网连接，请先调用 connect_local()")
        if link.did != did:
            raise LocalTransportUnsupported(
                f"局域网链路属于 {link.did}，拒绝用于 {did}（跨设备误发保护）"
            )
        return link

    def send_local_control(
        self,
        did: str,
        as_dict: Dict[str, Any],
        old_device: bool = False,
    ) -> Dict[str, Any]:
        """通过局域网发送控制命令（不读响应），sendall 后立即返回。

        old_device=True 时改用老设备的单键报文，多键会被拒绝（抛
        LocalTransportUnsupported）并改走云端，避免设备静默丢弃多余键。
        """
        link = self._require_local_link(did)

        mid = str(int(time.time() * 1000))
        json_str = build_opt_message(did, as_dict, mid, old_device=old_device)

        serial = self._next_serial()
        full_lan_pin = link.lan_pin + link.lan_pin
        frame = build_business_packet(serial, json_str, full_lan_pin)

        _LOGGER.debug(
            "局域网下发控制 %s%s: %s", did, "（老设备单键）" if old_device else "", json_str
        )
        with link.lock:
            if link.socket is None:
                raise RuntimeError(f"设备 {did} 的局域网连接已关闭")
            link.socket.sendall(frame)

        return {"status": "sent", "mid": mid}

    def query_local_device(self, did: str, timeout: float = 5.0) -> Dict[str, Any]:
        """通过局域网查询设备状态（只读该设备自己的链路）。

        Returns:
            设备状态字典，失败时返回 {"error": "reason"}
        """
        link = self.local_link(did)
        if link is None:
            return {"error": "no local link（请先调用 connect_local()）"}

        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "query",
            "mid": mid,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        full_lan_pin = link.lan_pin + link.lan_pin
        frame = build_business_packet(serial, json_str, full_lan_pin)

        with link.lock:
            if link.socket is None:
                return {"error": "local link closed"}
            link.socket.sendall(frame)

            start_time = time.time()
            while time.time() - start_time < timeout:
                remaining = timeout - (time.time() - start_time)
                link.socket.settimeout(max(remaining, 1.0))
                try:
                    raw = self._recv_local_frame(link)
                    if raw is None:
                        continue
                    result = self._parse_business_response(raw, full_lan_pin)
                    resp_mid = (result or {}).get("mid") if isinstance(result, dict) else None
                    if resp_mid == mid:
                        return result
                except ConnectionError:
                    break
                except socket.timeout:
                    continue
                except Exception:  # noqa: BLE001
                    continue

        return {"error": "timeout"}

    # ---- 响应解析 ----
    def _recv_exact(self, n: int, timeout: Optional[float] = None) -> bytes:
        """从长连接 socket 读取精确 n 字节的数据。

        `timeout=None` 时按帧大小估算预算（`max(n/4096, 3.0)` 秒），与旧行为一致；
        传入 `timeout` 时以它为单帧读预算（下限 0.05s），供探测层的心跳应答 /
        排空积压等有界读使用。注意：本预算经 `settimeout` 生效，会覆盖调用方
        进入前设置的 socket 超时。
        """
        if n > 65535:
            raise RuntimeError(f"_recv_exact: 请求读取 {n} 字节，超过最大允许值")
        data = b""
        budget = max(n / 4096.0, 3.0) if timeout is None else max(float(timeout), 0.05)
        deadline = time.time() + budget
        while len(data) < n:
            self._socket.settimeout(max(deadline - time.time(), 0.1))
            try:
                chunk = self._socket.recv(n - len(data))
            except socket.timeout:
                if data:
                    # 已读出部分字节后超时：帧边界未知，流状态不可信
                    raise ConnectionError("半帧超时，流状态不可信") from None
                raise   # 本次 recv 无任何已读字节 = 帧边界干净超时
            if not chunk:
                raise ConnectionError("长连接已关闭")
            data += chunk            # 先并入：chunk 一经读出即视为已消费
            if len(data) < n and time.time() > deadline:
                raise ConnectionError("半帧超时，流状态不可信")   # 已读但不完整
        return data

    def _recv_frame(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """从长连接 socket 读取一个完整帧。

        处理心跳响应 AA BB 01（3字节）和标准协议帧（7+字节）。
        返回完整的帧字节，超时时返回 None。

        `timeout=None` 时沿用 `_recv_exact` 的默认预算；传入时作为整帧每个
        分段读的预算（调用方多按帧数×预算估算总量）。
        """
        try:
            hdr = self._recv_exact(2, timeout)

            if hdr[0] != 0xAA or hdr[1] != 0xBB:
                # 云端帧恒以 AA BB 开头（抓包已证），非魔数只可能是流错位：
                # 直接判流不可信，交由调用方 close + 重连重同步（不再返回碎片）。
                raise ConnectionError("帧头魔数不符，流状态不可信")

            msg_type_byte = self._recv_exact(1, timeout)
            msg_type = msg_type_byte[0] & 0xFF

            # 心跳 AA BB 01
            if msg_type == MSG_TYPE_HEARTBEAT:
                self._last_rx_at = time.time()   # 收到完整帧 → 接收活性刷新
                return bytes([0xAA, 0xBB, MSG_TYPE_HEARTBEAT])

            # 标准帧：剩余 4 字节头部
            rest_hdr = self._recv_exact(4, timeout)
            header = hdr + msg_type_byte + rest_hdr
            extra_len = header[3] & 0xFF
            enc_len = _get_short(header, 5)
            if enc_len > 65535:
                raise RuntimeError(f"_recv_frame: enc_len={enc_len} 异常")

            body = self._recv_exact(extra_len + enc_len, timeout)
            self._last_rx_at = time.time()       # 收到完整帧 → 接收活性刷新
            return header + body
        except socket.timeout:
            return None
        except RuntimeError:
            _LOGGER.debug("_recv_frame: 帧头长度异常", exc_info=True)
            return None

    def _parse_business_response(self, raw: bytes, encrypt_key: str) -> Dict[str, Any]:
        """解析业务消息响应。

        加密体结构：serial(2 bytes) + JSON_content

        返回前经 `normalize_business_payload` 归一化，保证调用方拿到的
        一定是「`as` 字典 + `fids` 数组」，无需关心老设备形态。
        """
        if len(raw) < 7:
            return {"error": "响应长度不足"}

        msg_type = raw[2] & 0xFF

        bArr2_len = raw[3] & 0xFF
        enc_len = _get_short(raw, 5)
        enc_start = 7 + bArr2_len

        if len(raw) < enc_start + enc_len:
            return {"error": "响应体长度不足"}

        encrypted = raw[enc_start : enc_start + enc_len]

        if not encrypted:
            # 设备与服务端都会发不带加密体的空帧（无业务数据），交由调用方继续等待
            return {"error": "空报文"}

        try:
            key = encrypt_key[:16].encode("utf-8")
            iv = encrypt_key[16:32].encode("utf-8")
            decrypted = _aes_cbc_decrypt(encrypted, key, iv)
            if len(decrypted) < 2:
                return {"error": "解密后数据长度不足"}
            json_str = decrypted[2:].decode("utf-8", errors="ignore")
            if not json_str.strip():
                return {"status": "ok"}
            return normalize_business_payload(json.loads(json_str))
        except Exception as e:  # noqa: BLE001
            _LOGGER.warning("业务响应解析失败：%s", e, exc_info=True)
            return {"error": str(e)}

    # ---- 心跳 ----
    def send_heartbeat(self) -> bool:
        """发送心跳包（保持长连接）。

        心跳包为 3 字节：AA BB 01。

        云端探测层段零也调用本方法（只发不收，对齐官方 App 每 40s 必发心跳的
        节拍，防服务端空闲回收）；需要「发心跳并读应答」的链路验证请用
        `send_heartbeat_with_ack()`。
        """
        if not self._socket:
            return False

        with self._lock:
            try:
                self._socket.sendall(b"\xAA\xBB\x01")
                self._last_heartbeat = time.time()
                return True
            except Exception:
                self.close_server()
                return False

    def send_heartbeat_with_ack(self) -> bool:
        """先排空接收积压，再发心跳并等待新应答（2s 预算）。

        为什么必须先排空：现网实测空闲期 Recv-Q 可达 3125B（≈2~284 帧，
        取决于帧构成）。不排空时本方法读到的是陈旧帧，会把已死链路误判为活。

        应答形态（抓包验证）：msgType=2、bArr2_len=4、enc_len=0 的 7+4 标准帧，
        `_recv_frame` 正常返回非 None。若应答前先到达业务推送帧，同样算活
        （该帧被本次消费，下一轮查询可补，轮询本身是主状态源）。

        公有 API 契约：返回 False 即链路不可信，**socket 必为空或已关闭**——
        `if not self._socket` 路径本就为空，EOF/发送失败/半帧/魔数各失败路径
        统一就地关闭；调用方仅需 `connect_server()` 重握手。
        """
        if not self._socket:
            return False
        with self._lock:
            # 1) 排空积压：队列空（读超时）或达帧数/时长上限即停
            deadline = time.time() + CLOUD_RX_DRAIN_MAX_SECONDS
            for _ in range(CLOUD_RX_DRAIN_MAX_FRAMES):
                if time.time() >= deadline:
                    break
                try:
                    if self._recv_frame(timeout=CLOUD_RX_DRAIN_TIMEOUT) is None:
                        break
                except ConnectionError:
                    self.close_server()
                    return False
            # 2) 发心跳
            try:
                self._socket.settimeout(CLOUD_HEARTBEAT_ACK_TIMEOUT)
                self._socket.sendall(b"\xAA\xBB\x01")
                self._last_heartbeat = time.time()
            except Exception:
                self.close_server()
                return False
            # 3) 读新应答（此时队列已空，读到的必然是本次心跳后的帧）。
            #    校验魔数属双保险：帧头魔数不符在 _recv_frame 内也会判流损坏；
            #    保留本校验以防该判定回滚或未来新增返回路径。
            try:
                raw = self._recv_frame(timeout=CLOUD_HEARTBEAT_ACK_TIMEOUT)
            except Exception:
                self.close_server()
                return False
            if raw is None or raw[:2] != b"\xAA\xBB":
                # ACK 超时（NAT 丢链）或魔数异常：链路不可信，统一就地关闭，
                # 调用方仅需 connect_server() 重握手（公有 API 契约，见 docstring）
                self.close_server()
                return False
            return True

    def _ensure_connected(self) -> None:
        """确保长连接可用（断线自动重连）。"""
        if self._socket is not None:
            if time.time() - self._last_heartbeat > self._heartbeat_interval * 0.8:
                if not self.send_heartbeat():
                    _LOGGER.warning("长连接心跳失败，重连中")
                    self.close_server()
            else:
                return

        if self._socket is None:
            err: Optional[Exception] = None
            try:
                ok = self.connect_server()
            except Exception as exc:  # noqa: BLE001
                ok, err = False, exc
            if not ok:
                # 云端不可用会一直持续到恢复为止，故只在首次进入失败态时记录
                if not self._cloud_down:
                    self._cloud_down = True
                    _LOGGER.error("云端长连接不可用，之后每次刷新都会重试", exc_info=err)
                self.close_server()
            elif self._cloud_down:
                self._cloud_down = False
                _LOGGER.info("云端长连接已恢复")

    # ---- 查询设备状态 ----
    def query_device(self, did: str, timeout: Optional[float] = None, accept_post: bool = False) -> Dict[str, Any]:
        """查询设备状态。

        当 accept_post=True 时，也会接受 cmd=post 且带 as 的响应帧
        （设备通过云端推送的状态快照，通常比 query resp 更快到达）。
        """
        if timeout is None:
            timeout = self.timeout

        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "query",
            "mid": mid,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        frame = build_business_packet(serial, json_str, self._password_md5)

        with self._lock:
            self._ensure_connected()
            if not self._socket:
                return {"error": "no connection"}
            try:
                self._socket.sendall(frame)
                self._last_heartbeat = time.time()
            except (OSError, AttributeError):
                _LOGGER.debug("query_device: socket断开")
                self.close_server()
                return {"error": "connection lost"}
            _LOGGER.debug("query_device: 已发送查询 mid=%s", mid)

            start_time = time.time()
            read_error_logged = False
            while time.time() - start_time < timeout:
                remaining = timeout - (time.time() - start_time)
                self._socket.settimeout(max(remaining, 1.0))
                try:
                    raw = self._recv_frame()
                    if raw is None:
                        continue
                    if len(raw) == 3 and raw[2] == MSG_TYPE_HEARTBEAT:
                        continue
                    result = self._parse_business_response(raw, self._password_md5)
                    if isinstance(result, dict) and result.get("error"):
                        continue
                    if accept_post and isinstance(result, dict) and result.get("cmd") == "post" and result.get("as"):
                        # 整个账号共用一条云端长连接，push 帧可能来自别的设备，
                        # 必须确认 from 是本次查询的设备，否则会把 B 的状态当成 A 的。
                        if str(result.get("from") or "") == did:
                            return result
                        continue
                    resp_mid = result.get("mid") if isinstance(result, dict) else None
                    if resp_mid == mid:
                        return result
                except ConnectionError:
                    # 流已损坏（EOF / 半帧 / 魔数错位）：就地关闭，等探测或下次
                    # 查询重握手；不再持锁忙等满 timeout，也不把死 socket 留着。
                    _LOGGER.debug("query_device: 长连接流异常，关闭连接")
                    self.close_server()
                    return {"error": "connection closed"}
                except socket.timeout:
                    continue
                except Exception:
                    # 只报第一次：同一轮里同样的错会被反复撞到，堆栈留一份即可
                    if not read_error_logged:
                        read_error_logged = True
                        _LOGGER.warning("query_device: 读取响应异常", exc_info=True)
                    continue

        raise TimeoutError(f"query_device 超时: 未收到 mid={mid} 的响应")
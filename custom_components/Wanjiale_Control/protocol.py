"""万家乐私有 TCP 协议完整实现。

基于 jadx 反编译代码分析还原的协议：

数据包格式（makeData）：
    AA BB | msgType | bArr2_len | checksum | enc_len_high | enc_len_low | bArr2(明文) | encrypted_body

加密体结构（加密前）：
    serial_high | serial_low | bArr

加密方式：
    AES/CBC/PKCS5Padding
    key = password_md5[0:16]
    iv = password_md5[16:32]

消息类型：
    9  - 登录（LoginMessage）
    10 - 连接长连接服务器（ConnectMessage）
    8  - 局域网认证（LocalLoginMessage）
    17 - 业务消息/控制命令（BusinessMessage）
    11 - 心跳响应（HeartMessage response）
    2  - 登录响应
    3  - 连接响应

局域网控制：
    - 加密密钥 = lanPin + lanPin (fullLanPin)
    - 先发送 LocalLoginMessage 认证
    - 认证成功后发送 BusinessMessage（JSON控制命令）

JSON控制命令格式：
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
from typing import Any, Dict, Optional, Tuple

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

    对应 Java 代码：Util.makeData(byte b4, int i4, byte[] bArr, byte[] bArr2, String str)

    结构：
        header[7]: AA BB | msgType | bArr2_len | checksum | enc_len_high | enc_len_low
        bArr2: 明文数据
        encrypted: AES加密后的 (serial + bArr)
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

    # 校验和（与 Java Util.makeData 一致）
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

    对应 Java 代码：Util.makeDataForLocal(byte b4, byte[] bArr)

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

    # 校验和（Java: sum of bArr bytes only, not including header fields）
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
# 登录消息构造（还原 LoginMessage.getData）
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
# 局域网认证消息构造（还原 LocalLoginMessage.getData）
# ======================================================================
def build_local_login_packet(serial: int, lan_pin: str) -> bytes:
    """构造局域网认证数据包。

    Java LocalLoginMessage.getData():
        byte[] bArr = {challenge_high, challenge_low};     // 2字节
        byte[] bArr2 = {challenge_high, challenge_low, 0, 10}; // 4字节
        return Util.makeData((byte)8, serial, bArr2, bArr, fullLanPin);
        //             加密体=bArr2(4字节)    明文头=bArr(2字节)

    msgType = 8, 加密密钥 = lanPin + lanPin
    """
    random_val = random.randint(0, 65535)
    bArr = bytes([(random_val >> 8) & 0xFF, random_val & 0xFF, 0x00, 0x0A])  # 加密体
    bArr2 = bytes([(random_val >> 8) & 0xFF, random_val & 0xFF])  # 明文头

    full_lan_pin = lan_pin + lan_pin  # 32位密钥
    return make_data(MSG_TYPE_LOCAL_LOGIN, serial, bArr, bArr2, full_lan_pin)


# ======================================================================
# 业务消息构造（还原 BusinessMessage.getData）
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
# 登录响应解析（还原 LoginMessage.handle）
# ======================================================================
def parse_login_response(
    raw: bytes,
    password_md5: str,
) -> Dict[str, Any]:
    """解析登录响应。

    响应结构（解密后）：
        [0:2]   serial
        [2:34]  session_key (32 bytes)
        [34:66] uid (32 bytes)
        [66:98] api_key (32 bytes)
        [98:102] server_ip (4 bytes)
        [102:104] server_port (大端)
    """
    if len(raw) < 7:
        _LOGGER.error("响应头长度不足: 只有 %d 字节, hex=%s", len(raw), raw.hex())
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
            "响应体长度不足: raw=%d expected=%d enc_len=%d, hex=%s",
            len(raw), enc_start + enc_len, enc_len, raw.hex(),
        )
        raise ValueError(f"响应体长度不足: {len(raw)} < {enc_start + enc_len}")

    encrypted = raw[enc_start : enc_start + enc_len]

    if len(encrypted) == 0:
        _LOGGER.error(
            "加密体为空！enc_len=%d bArr2_len=%d, raw_hex=%s",
            enc_len, bArr2_len, raw.hex(),
        )
        raise ValueError("服务器返回空加密数据，请检查账号密码是否正确")

    # 解密（key/iv 为 password_md5 hex 字符串的前/后 16 字符）
    key = password_md5[:16].encode("utf-8")
    iv = password_md5[16:32].encode("utf-8")
    decrypted = _aes_cbc_decrypt(encrypted, key, iv)

    _LOGGER.debug("解密成功: decrypted_length=%d", len(decrypted))

    if len(decrypted) < 106:
        _LOGGER.error(
            "解密后数据长度不足: %d < 106, decrypted_hex=%s",
            len(decrypted), decrypted.hex(),
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
        raise ValueError("未能从响应中解析出 uid，可能密码错误")

    return result


# ======================================================================
# 局域网认证响应解析（还原 LocalLoginMessage.handle）
# ======================================================================
def parse_local_login_response(raw: bytes, full_lan_pin: str) -> bool:
    """解析局域网认证响应。

    Java ConnectionManager.x() → 解密后调用 LocalLoginMessage.handle(bArr, bArr2)
    bArr = 解密后的 payload[2:]（跳过 serial）
    result_code = bArr[0:2]

    设备响应帧 = makeData 格式
    当 enc_len == 0 时：响应无加密体，result_code 直接在明文 plaintext 中
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
        "local auth parse: msg_type=%d pl_len=%d enc_len=%d full_pin=%s",
        raw[2] & 0xFF, plaintext_len, enc_len, full_lan_pin,
    )

    # enc_len == 0：无加密体，result_code 直接从明文读取
    if enc_len == 0:
        if plaintext_len < 2:
            return False
        result_code = _get_short(raw, 7)  # 跳过 7 字节 header
        return result_code == 0

    # 有加密体：解密后读取
    encrypted = raw[7 + plaintext_len : 7 + plaintext_len + enc_len]
    _LOGGER.debug("local auth enc hex: %s", encrypted.hex()[:40])

    key = full_lan_pin[:16].encode("utf-8")
    iv = full_lan_pin[16:32].encode("utf-8")

    try:
        decrypted = _aes_cbc_decrypt(encrypted, key, iv)
        _LOGGER.debug("local auth decrypted: len=%d hex=%s", len(decrypted), decrypted.hex())
    except Exception:
        _LOGGER.debug("local auth decrypt failed", exc_info=True)
        return False

    if len(decrypted) < 4:
        return False

    result_code = _get_short(decrypted, 2)  # 跳过 2 字节 serial
    return result_code == 0


# ======================================================================
# HTTP 设备列表 / 签名
# ======================================================================
def build_auth_headers(uid: str, api_key: str) -> Dict[str, str]:
    """构建 HTTP 请求头（ts/uid/key 签名）。"""
    ts = str(int(time.time() * 1000))
    key = md5_hash(api_key + ts)
    return {
        "ts": ts,
        "uid": uid,
        "key": key,
        "User-Agent": "okhttp/3.8.1",
        "Host": "newapi.machtalk.net",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip",
    }


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

        # 局域网控制（需要设备的 lanPin）
        proto.connect_local(device_ip, device_port, lan_pin)  # 局域网认证
        proto.send_local_control(did, {"2": "71045130"})       # 局域网控制
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
        self._local_socket: Optional[socket.socket] = None
        self._local_lan_pin: Optional[str] = None
        self._serial = random.randint(0, 9999)
        self._last_heartbeat = 0.0
        self._heartbeat_interval = 40
        self._lock = threading.RLock()
        self._local_lock = threading.RLock()

        # LAN 心跳（防止设备端空闲关闭连接，对应 App ConnectionManager.J() 的 HeartMessage）
        self._lan_heartbeat_interval: float = 40.0
        self._lan_stop_event = threading.Event()
        self._lan_heartbeat_thread: Optional[threading.Thread] = None

    def _next_serial(self) -> int:
        """获取下一个序列号。"""
        self._serial = (self._serial + 1) % 65536
        return self._serial

    # ---- TCP 登录 ----
    def login(self) -> Dict[str, Any]:
        """登录并获取 uid/api_key。"""
        bArr2 = build_login_bArr2(self.username, USER_TYPE_NORMAL)  # 明文部分
        bArr = build_login_bArr(self.username, self.imei)  # 加密体部分
        serial = self._next_serial()
        frame = make_data(MSG_TYPE_LOGIN, serial, bArr, bArr2, self._password_md5)

        _LOGGER.debug(
            "login frame: serial=%d bArr_len=%d bArr2_len=%d bArr2_hex=%s bArr_hex=%s pw_md5=%s",
            serial, len(bArr), len(bArr2), bArr2.hex(), bArr.hex(), self._password_md5,
        )
        _LOGGER.debug("login frame hex: %s", frame.hex())

        _LOGGER.debug("connecting to %s:%d", self.host, self.port)
        with socket.create_connection((self.host, self.port), timeout=self.timeout) as s:
            s.sendall(frame)
            _LOGGER.debug("login packet sent, %d bytes, waiting for response...", len(frame))
            resp = s.recv(4096)

        _LOGGER.debug("received %d bytes, hex=%s", len(resp), resp[:80].hex())

        if not resp:
            raise RuntimeError("服务器返回空响应")

        result = parse_login_response(resp, self._password_md5)

        self.uid = result.get("uid")
        self.api_key = result.get("api_key")
        self.session_key = result.get("session_key")
        self.server_ip = result.get("server_ip")
        self.server_port = result.get("server_port")

        _LOGGER.info("login ok: uid=%s server=%s:%s", self.uid, self.server_ip, self.server_port)
        return result

    # ---- HTTP 拉设备列表 ----
    def get_devices(self) -> list[Dict[str, Any]]:
        """从 HTTP API 获取设备列表。"""
        if not self.uid or not self.api_key:
            raise RuntimeError("请先调用 login()")

        try:
            import requests
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("requests 未安装") from e

        url = f"{self.http_base_url}/app/devices"
        resp = None
        try:
            resp = requests.get(url, headers=build_auth_headers(self.uid, self.api_key), timeout=self.http_timeout)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            _LOGGER.debug(
                "HTTP get_devices 失败: status=%s body=%s",
                getattr(resp, "status_code", None) if resp is not None else "N/A",
                (getattr(resp, "text", "")[:200]) if resp is not None else "N/A",
            )
            raise
        if "devs" in data:
            devs: list[Dict[str, Any]] = data["devs"]
            _LOGGER.info("got %d devices", len(devs))
            return devs
        raise RuntimeError(f"devices response missing 'devs': {data}")

    async def async_get_devices(self) -> list[Dict[str, Any]]:
        """从 HTTP API 获取设备列表（异步版本，使用 aiohttp）。"""
        if not self.uid or not self.api_key:
            raise RuntimeError("请先调用 login()")

        import aiohttp

        url = f"{self.http_base_url}/app/devices"
        headers = build_auth_headers(self.uid, self.api_key)
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        if "devs" in data:
            devs: list[Dict[str, Any]] = data["devs"]
            _LOGGER.info("got %d devices", len(devs))
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
            "connect_server: serial=%d session_key=%s bArr2_hex=%s password_md5=%s",
            serial, self.session_key, bArr2.hex(), self._password_md5,
        )
        _LOGGER.debug("connect_server frame hex: %s", frame.hex())

        _LOGGER.debug("connecting to server %s:%d", self.server_ip, self.server_port)
        with self._lock:
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

        _LOGGER.debug("connect_server received: len=%d hex=%s", len(resp), resp[:40].hex())

        encrypted = resp[7 + extra_len : 7 + extra_len + enc_len]

        key = self._password_md5[:16].encode("utf-8")
        iv = self._password_md5[16:32].encode("utf-8")
        decrypted = _aes_cbc_decrypt(encrypted, key, iv)

        _LOGGER.debug("connect_server decrypted: len=%d hex=%s", len(decrypted), decrypted[:20].hex())

        if len(decrypted) >= 12:
            result_code = decrypted[11] if len(decrypted) > 11 else 1
            if result_code == 0:
                heartbeat = ((decrypted[2] & 0xFF) << 8) | (decrypted[3] & 0xFF)
                self._heartbeat_interval = heartbeat
                self._last_heartbeat = time.time()
                _LOGGER.info("connected to long connection server, heartbeat=%ds", heartbeat)
                return True
            _LOGGER.warning("connect_server: result_code=%d", result_code)

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

    # ---- 云端控制 ----
    def send_control(self, did: str, as_dict: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        """通过云端发送控制命令（同步等待响应）。"""
        if timeout is None:
            timeout = self.timeout

        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "opt",
            "mid": mid,
            "as": as_dict,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        frame = build_business_packet(serial, json_str, self._password_md5)

        _LOGGER.debug("sending control to %s: %s", did, json_str)

        with self._lock:
            self._ensure_connected()
            if not self._socket:
                raise RuntimeError("send_control: 无可用长连接")
            self._socket.sendall(frame)
            self._last_heartbeat = time.time()

            start_time = time.time()
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
                    raise
                except socket.timeout:
                    continue
                except Exception:
                    continue

        raise TimeoutError(f"send_control 超时: mid={mid}")

    def send_control_async(self, did: str, as_dict: Dict[str, Any]) -> Dict[str, Any]:
        """通过云端发送控制命令（fire-and-forget，不等待响应）。

        对应原始 App 的 SendAction → ViewAdapter.b() → PostMessage 入队后立即返回。
        状态确认由定时轮询的 coordinator 完成，与 App 的 onSuccess 回调模式一致。
        """
        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "opt",
            "mid": mid,
            "as": as_dict,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        frame = build_business_packet(serial, json_str, self._password_md5)

        _LOGGER.debug("sending control async to %s: %s", did, json_str)

        with self._lock:
            self._ensure_connected()
            if not self._socket:
                raise RuntimeError("send_control_async: 无可用长连接")
            self._socket.sendall(frame)
            self._last_heartbeat = time.time()

        return {"status": "sent", "mid": mid}

    # ---- UDP 局域网设备发现 ----
    def discover_device(self, timeout: float = 3.0) -> Optional[str]:
        """通过 UDP 广播发现设备局域网 IP。

        对应 Java: BroadcastManager.binary broadcast on port 7680
        BroadcastMessage payload: {0, timestamp[4], 0, 0}

        Returns:
            设备局域网 IP 字符串，或 None
        """
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
            _LOGGER.debug("discover_device: 已发送 UDP 广播, ts=%d", ts)

            # 等待设备响应
            data, addr = sock.recvfrom(1024)
            sock.close()

            if len(data) >= 3 and data[0] == 0xAA and data[1] == 0xBB:
                _LOGGER.info("discover_device: 发现设备 %s", addr[0])
                return addr[0]

            _LOGGER.debug("discover_device: 收到非 AA BB 响应: %s", data[:10].hex())
            return None

        except socket.timeout:
            _LOGGER.debug("discover_device: 广播超时（设备可能不在同一子网）")
            return None
        except Exception:
            _LOGGER.debug("discover_device: 广播失败", exc_info=True)
            return None
        finally:
            try:
                sock.close()
            except Exception:
                pass

    # ---- 局域网连接与认证 ----
    def connect_local(self, ip: str, port: int, lan_pin: str) -> bool:
        """连接设备局域网端口并进行认证。"""
        _LOGGER.debug("connecting to local device %s:%d", ip, port)

        full_lan_pin = lan_pin + lan_pin

        with self._local_lock:
            self._local_socket = socket.create_connection((ip, port), timeout=self.timeout)

            # 发送局域网认证消息
            serial = self._next_serial()
            frame = build_local_login_packet(serial, lan_pin)
            _LOGGER.debug("local login frame hex: %s", frame.hex())
            self._local_socket.sendall(frame)

            # 读取响应（帧格式）
            try:
                self._local_socket.settimeout(self.timeout)
                raw = self._recv_local_frame()
            except Exception:
                _LOGGER.exception("local device read error")
                self._local_socket.close()
                self._local_socket = None
                return False

            if raw is None:
                _LOGGER.error("local device no response")
                self._local_socket.close()
                self._local_socket = None
                return False

            success = parse_local_login_response(raw, full_lan_pin)

            if success:
                self._local_lan_pin = lan_pin
                _LOGGER.info("local device authentication success")
                self._start_lan_heartbeat()
            else:
                _LOGGER.debug("local auth raw hex: %s", raw.hex() if raw else "None")
                self._local_socket.close()
                self._local_socket = None
                _LOGGER.error("local device authentication failed")

            return success

    def _recv_local_frame(self) -> Optional[bytes]:
        """从局域网 socket 读取完整帧。"""
        try:
            hdr = self._recv_local_exact(7)
            if hdr[0] != 0xAA or hdr[1] != 0xBB:
                return None
            plaintext_len = hdr[3] & 0xFF
            enc_len = _get_short(hdr, 5)
            if enc_len > 65535:
                return None
            body = self._recv_local_exact(plaintext_len + enc_len)
            return hdr + body
        except (socket.timeout, ConnectionError):
            return None

    def _recv_local_exact(self, n: int) -> bytes:
        """从局域网 socket 读取精确 n 字节。"""
        if n > 65535:
            raise RuntimeError(f"_recv_local_exact: 请求读取 {n} 字节，超过最大允许值")
        data = b""
        deadline = time.time() + max(n / 4096.0, 3.0)
        while len(data) < n:
            self._local_socket.settimeout(max(deadline - time.time(), 0.1))
            chunk = self._local_socket.recv(n - len(data))
            if not chunk:
                raise ConnectionError("局域网连接已关闭")
            if time.time() > deadline:
                raise socket.timeout(f"_recv_local_exact: 读取超时, 已读 {len(data)}/{n}")
            data += chunk
        return data

    def close_local(self) -> None:
        """关闭局域网连接。"""
        self._lan_stop_event.set()
        with self._local_lock:
            if self._local_socket:
                try:
                    self._local_socket.close()
                except Exception:
                    pass
                self._local_socket = None
                self._local_lan_pin = None
        if self._lan_heartbeat_thread is not None and self._lan_heartbeat_thread.is_alive():
            self._lan_heartbeat_thread.join(timeout=2.0)
        self._lan_heartbeat_thread = None
        self._lan_stop_event.clear()

    def _start_lan_heartbeat(self) -> None:
        """启动 LAN 心跳守护线程。

        对应 App: ConnectionManager.J() 通过 NIO Selector isWritable 事件触发 HeartMessage 发送。
        设备端嵌入式 TCP 空闲超时通常 15-60s，心跳每 40s 发一次 AA BB 01 保持连接活跃。
        """
        if self._lan_heartbeat_thread is not None and self._lan_heartbeat_thread.is_alive():
            return
        self._lan_stop_event.clear()
        self._lan_heartbeat_thread = threading.Thread(
            target=self._lan_heartbeat_loop, daemon=True, name="wanjiale-lan-hb",
        )
        self._lan_heartbeat_thread.start()

    def _lan_heartbeat_loop(self) -> None:
        """LAN 心跳循环：每 _lan_heartbeat_interval 秒发 AA BB 01。

        close_local() 通过 _lan_stop_event.set() 通知退出。
        心跳失败（BrokenPipe/ConnectionReset）时自动清理 socket 并退出。
        """
        while not self._lan_stop_event.wait(self._lan_heartbeat_interval):
            with self._local_lock:
                if self._local_socket is None:
                    return
                try:
                    self._local_socket.sendall(b"\xAA\xBB\x01")
                except Exception:
                    _LOGGER.debug("LAN 心跳失败, 关闭本地连接")
                    try:
                        self._local_socket.close()
                    except Exception:
                        pass
                    self._local_socket = None
                    self._local_lan_pin = None
                    return

    # ---- 局域网控制 ----
    def send_local_control(self, did: str, as_dict: Dict[str, Any]) -> Dict[str, Any]:
        """通过局域网发送控制命令（fire-and-forget，不读响应）。

        Java ConnectionManager 使用 NIO Selector 非阻塞 send，
        此处同步 sendall 后立即返回，不阻塞 executor 线程。
        """
        if not self._local_socket or not self._local_lan_pin:
            raise RuntimeError("请先调用 connect_local()")

        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "opt",
            "mid": mid,
            "as": as_dict,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        full_lan_pin = self._local_lan_pin + self._local_lan_pin
        frame = build_business_packet(serial, json_str, full_lan_pin)

        _LOGGER.debug("sending local control to %s: %s", did, json_str)
        with self._local_lock:
            self._local_socket.sendall(frame)

        return {"status": "sent", "mid": mid}

    def query_local_device(self, did: str, timeout: float = 5.0) -> Dict[str, Any]:
        """通过局域网查询设备状态。

        Returns:
            设备状态字典，失败时返回 {"error": "reason"}
        """
        if not self._local_socket or not self._local_lan_pin:
            return {"error": "请先调用 connect_local()"}

        mid = str(int(time.time() * 1000))
        json_obj = {
            "to": did,
            "cmd": "query",
            "mid": mid,
        }
        json_str = json.dumps(json_obj, separators=(",", ":"))

        serial = self._next_serial()
        full_lan_pin = self._local_lan_pin + self._local_lan_pin
        frame = build_business_packet(serial, json_str, full_lan_pin)

        with self._local_lock:
            self._local_socket.sendall(frame)

            start_time = time.time()
            while time.time() - start_time < timeout:
                remaining = timeout - (time.time() - start_time)
                self._local_socket.settimeout(max(remaining, 1.0))
                try:
                    raw = self._recv_local_frame()
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
                except Exception:
                    continue

        return {"error": "timeout"}

    # ---- 响应解析 ----
    def _recv_exact(self, n: int) -> bytes:
        """从长连接 socket 读取精确 n 字节的数据。"""
        if n > 65535:
            raise RuntimeError(f"_recv_exact: 请求读取 {n} 字节，超过最大允许值")
        data = b""
        deadline = time.time() + max(n / 4096.0, 3.0)
        while len(data) < n:
            self._socket.settimeout(max(deadline - time.time(), 0.1))
            chunk = self._socket.recv(n - len(data))
            if not chunk:
                raise ConnectionError("长连接已关闭")
            if time.time() > deadline:
                raise socket.timeout(f"_recv_exact: 读取超时, 已读 {len(data)}/{n}")
            data += chunk
        return data

    def _recv_frame(self) -> Optional[bytes]:
        """从长连接 socket 读取一个完整帧。

        处理心跳响应 AA BB 01（3字节）和标准协议帧（7+字节）。
        返回完整的帧字节，超时时返回 None。
        """
        try:
            hdr = self._recv_exact(2)

            if hdr[0] != 0xAA or hdr[1] != 0xBB:
                return hdr

            msg_type_byte = self._recv_exact(1)
            msg_type = msg_type_byte[0] & 0xFF

            # 心跳 AA BB 01
            if msg_type == MSG_TYPE_HEARTBEAT:
                return bytes([0xAA, 0xBB, MSG_TYPE_HEARTBEAT])

            # 标准帧：剩余 4 字节头部
            rest_hdr = self._recv_exact(4)
            header = hdr + msg_type_byte + rest_hdr
            extra_len = header[3] & 0xFF
            enc_len = _get_short(header, 5)
            if enc_len > 65535:
                raise RuntimeError(f"_recv_frame: enc_len={enc_len} 异常")

            body = self._recv_exact(extra_len + enc_len)
            return header + body
        except (socket.timeout, ConnectionError):
            return None
        except RuntimeError:
            return None

    def _parse_business_response(self, raw: bytes, encrypt_key: str) -> Dict[str, Any]:
        """解析业务消息响应。

        加密体结构：serial(2 bytes) + JSON_content
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

        try:
            key = encrypt_key[:16].encode("utf-8")
            iv = encrypt_key[16:32].encode("utf-8")
            decrypted = _aes_cbc_decrypt(encrypted, key, iv)
            if len(decrypted) < 2:
                return {"error": "解密后数据长度不足"}
            json_str = decrypted[2:].decode("utf-8", errors="ignore")
            if not json_str.strip():
                return {"status": "ok"}
            return json.loads(json_str)
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    # ---- 心跳 ----
    def send_heartbeat(self) -> bool:
        """发送心跳包（保持长连接）。

        心跳包是原始 3 字节：AA BB 01（与 Android 日志一致）。
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

    def _ensure_connected(self) -> None:
        """确保长连接可用（断线自动重连，静默失败）。"""
        if self._socket is not None:
            if time.time() - self._last_heartbeat > self._heartbeat_interval * 0.8:
                if not self.send_heartbeat():
                    _LOGGER.debug("心跳失败")
                    self.close_server()
            else:
                return

        if self._socket is None:
            try:
                if not self.connect_server():
                    _LOGGER.debug("重连长连接失败")
                    self.close_server()
            except Exception:
                _LOGGER.debug("重连长连接异常", exc_info=True)
                self.close_server()

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
            _LOGGER.debug("query_device sent: mid=%s", mid)

            start_time = time.time()
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
                        return result
                    resp_mid = result.get("mid") if isinstance(result, dict) else None
                    if resp_mid == mid:
                        return result
                except ConnectionError:
                    raise
                except socket.timeout:
                    continue
                except Exception:
                    _LOGGER.debug("query_device: 读取异常", exc_info=True)
                    continue

        raise TimeoutError(f"query_device 超时: 未收到 mid={mid} 的响应")
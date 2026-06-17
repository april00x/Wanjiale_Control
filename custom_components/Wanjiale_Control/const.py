"""万家乐集成的常量定义。"""
from __future__ import annotations

DOMAIN = "wanjiale_control"

# config_entry / data 字段
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_IMEI = "imei"

# 本地（局域网）控制配置
CONF_LOCAL_HOST = "local_host"
CONF_LOCAL_PORT = "local_port"

# 轮询间隔（秒）
DEFAULT_SCAN_INTERVAL = 10

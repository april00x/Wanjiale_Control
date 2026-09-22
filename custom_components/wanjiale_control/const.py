"""万家乐集成的常量定义。"""
from __future__ import annotations

DOMAIN = "wanjiale_control"

# config_entry / data 字段
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_IMEI = "imei"

# 选项（OptionsFlow）
CONF_ENABLE_CONTROL_RIGHTS = "enable_control_rights"   # 是否启用多端操作权抢占
CONF_FEATURE_FORCE_ON = "feature_force_on"             # 强制开启的功能（多选）
CONF_FEATURE_FORCE_OFF = "feature_force_off"           # 强制关闭的功能（多选）

# 轮询间隔（秒）
DEFAULT_SCAN_INTERVAL = 10

# 需要注册的实体平台
PLATFORMS = (
    "water_heater",
    "switch",
    "number",
    "select",
    "button",
    "sensor",
    "binary_sensor",
)

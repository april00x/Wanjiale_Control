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

# 核心最小平台集：通用只读设备（未注册品类）也要落在这两个平台上方能"可见"，
# 因此无论注册了哪些品类都必须转发它们。
# 各品类自己需要的平台由 `registry.all_platforms()` 求并集（见 __init__.py）。
CORE_PLATFORMS = (
    "sensor",
    "binary_sensor",
)

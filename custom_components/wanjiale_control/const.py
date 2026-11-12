"""万家乐集成的常量定义。"""
from __future__ import annotations

DOMAIN = "wanjiale_control"

# config_entry / data 字段
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_IMEI = "imei"

# 选项（OptionsFlow）
CONF_ENABLE_CONTROL_RIGHTS = "enable_control_rights"   # 是否启用多端操作权抢占
# 功能覆盖按设备保存：{did: {"on": [功能名...], "off": [功能名...]}}
CONF_FEATURE_OVERRIDES = "feature_overrides"
CONF_DEVICE_FORCE_ON = "force_on"                      # 单台设备的强制开启列表
CONF_DEVICE_FORCE_OFF = "force_off"                    # 单台设备的强制关闭列表
CONF_DEVICE_DID = "device_did"                         # 选项页当前配置的设备
CONF_DEVICE_FEATURES = "device_features"               # 选项页「已开启的功能」多选（仅表单字段，不落库）
# 旧版账号级覆盖（仅用于迁移，读取后从 options 中移除）
CONF_FEATURE_FORCE_ON = "feature_force_on"
CONF_FEATURE_FORCE_OFF = "feature_force_off"
# 一次性动作：勾选后本次重载允许云端新设备加入；不落库（只进内存标记集合）
CONF_RESCAN_DEVICES = "rescan_devices"

# 轮询间隔（秒）
DEFAULT_SCAN_INTERVAL = 10

# 核心最小平台集：通用只读设备（未注册品类）也要落在这两个平台上方能"可见"，
# 因此无论注册了哪些品类都必须转发它们。
# 各品类自己需要的平台由 `registry.all_platforms()` 求并集（见 __init__.py）。
CORE_PLATFORMS = (
    "sensor",
    "binary_sensor",
)

"""品类插件目录：一个品类一个子包，导入即自注册。

目录约定：

    categories/<产品码>/
        plugin.py                   构造并 register_plugin(CategoryPlugin(...))
        capability_<product>.json   机型级点位/能力表（须带 schemaVersion）
        faults_<product>.json       机型级故障文案表
        specs.py                    该品类在各 HA 平台上的 spec 表

新增品类的完整动作就是只新增这一个目录，核心层与既有平台模块零改动。

加载时机：由 `__init__.py` 在 `async_setup_entry` 内调用 `load_all()`，
不在导入时自动执行，避免 import-time 副作用。
"""
from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import List

_LOGGER = logging.getLogger(__name__)

# 名称以下划线开头的子包视为内部实现，不作为品类加载
_SKIP_PREFIX = "_"


def load_all() -> List[str]:
    """导入本目录下所有品类子包，触发其自注册；返回已加载的子包名。

    单个品类加载失败直接跳过，不阻断整个 setup。
    """
    loaded: List[str] = []
    for info in pkgutil.iter_modules(__path__):  # type: ignore[name-defined]
        name = info.name
        if name.startswith(_SKIP_PREFIX):
            continue
        try:
            importlib.import_module(f"{__name__}.{name}")
        except Exception:  # noqa: BLE001
            _LOGGER.warning("品类插件 %s 加载失败，已跳过", name, exc_info=True)
            continue
        loaded.append(name)

    if loaded:
        _LOGGER.info("已加载品类插件：%s", "、".join(loaded))
    return loaded

"""008 品类子包：导入即自注册（`categories/__init__.py` 用 pkgutil 逐包导入）。

缺本文件整包不会被加载（`pkgutil.iter_modules` 认的是常规包），与 `categories/001/__init__.py` 同构。
"""
from . import plugin  # noqa: F401  （导入即 register_plugin）

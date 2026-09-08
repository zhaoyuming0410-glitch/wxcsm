# -*- coding: utf-8 -*-
"""shim: 让 sources 包在跨平台下都能 import 到 MacWxLiveSource。

真正实现在 core/mac_wx4/data_source.py; 此处仅转发, 并保证非 mac 上 import 不崩。
"""
from ..mac_wx4.data_source import MacWxLiveSource  # noqa: F401

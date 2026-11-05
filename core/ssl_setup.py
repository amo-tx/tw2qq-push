# -*- coding: utf-8 -*-
"""SSL 信任设置：让 Python 改用 Windows 系统证书库

解决 Watt Toolkit(Steam++) 等 MITM 型加速工具导致的
CERTIFICATE_VERIFY_FAILED：这类工具用自签证书代理 GitHub，
浏览器信任（证书装在系统库）而 Python 默认用自带 certifi，不认。
注入后 Python 与浏览器一样信任系统证书库（含 Watt 的根证书）。
"""
import logging


def enable_system_trust():
    try:
        import truststore
        truststore.inject_into_ssl()
        logging.info("已启用系统证书库信任（truststore）")
        return True
    except Exception as e:
        logging.warning("系统证书信任启用失败（回退 certifi）: %s", e)
        return False

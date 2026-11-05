# -*- coding: utf-8 -*-
"""消息模板渲染"""
MAX_LEN = 4000  # QQ 单条消息安全长度


def format_message(template: str, **fields) -> str:
    msg = template.format(**fields).strip()
    if len(msg) > MAX_LEN:
        msg = msg[: MAX_LEN - 1] + "…"
    return msg

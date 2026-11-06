"""本机设备名：一台设备一个身份，统一取 Windows 计算机名。

Hub 设备绑定、任务执行者、注册申报与记忆来源节点名都用它，与 HubTaskRunner
`windows_readonly.computer_name()` 使用同一个 API（GetComputerNameW），保证两边一致。
本机文件锁与后台作业的持有者标识不是设备身份，继续使用 socket.gethostname()。
"""

from __future__ import annotations

import os
import re
import socket

DEVICE_NAME_PATTERN = re.compile(r"[^\W_][\w.:-]{0,95}")


class DeviceNameError(RuntimeError):
    """本机设备名无法读取或不满足 Hub 设备名格式。"""


def _windows_computer_name() -> str:
    import ctypes
    from ctypes import wintypes

    buffer = ctypes.create_unicode_buffer(256)
    size = wintypes.DWORD(len(buffer))
    if not ctypes.windll.kernel32.GetComputerNameW(buffer, ctypes.byref(size)):
        raise DeviceNameError("GetComputerNameW failed")
    return buffer.value


def raw_device_name() -> str:
    """原样返回本机设备名，不改大小写；Windows 为计算机名，其它平台为主机名第一段。"""
    if os.name == "nt":
        return _windows_computer_name()
    return socket.gethostname().split(".", 1)[0]


def device_name() -> str:
    name = raw_device_name()
    if not name or not DEVICE_NAME_PATTERN.fullmatch(name):
        raise DeviceNameError("invalid_device_name")
    return name


def device_name_or(default: str) -> str:
    """供展示用途（如记忆来源节点名）：读取失败时回退，不阻断本地写入。"""
    try:
        return device_name()
    except (DeviceNameError, OSError, AttributeError):
        return default


__all__ = ["DEVICE_NAME_PATTERN", "DeviceNameError", "device_name", "device_name_or", "raw_device_name"]

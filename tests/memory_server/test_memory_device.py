"""本机设备名：计算机名来源、记忆来源节点名与本机锁标识的边界。"""
from __future__ import annotations

import socket

import pytest

from servers.memory_server import memory_device, memory_identity, memory_locks


def test_device_name_is_used_verbatim_and_validated(monkeypatch):
    monkeypatch.setattr(memory_device, "raw_device_name", lambda: "DESK-WORKSTATIO")
    assert memory_device.device_name() == "DESK-WORKSTATIO"
    for invalid in ("", "_lead", "has space", "x" * 97):
        monkeypatch.setattr(memory_device, "raw_device_name", lambda value=invalid: value)
        with pytest.raises(memory_device.DeviceNameError):
            memory_device.device_name()
    assert memory_device.device_name_or("fallback") == "fallback"


def test_non_windows_uses_the_first_hostname_label(monkeypatch):
    monkeypatch.setattr(memory_device.os, "name", "posix")
    monkeypatch.setattr(memory_device.socket, "gethostname", lambda: "build-01.corp.example")
    assert memory_device.raw_device_name() == "build-01"


def test_memory_source_node_name_uses_the_computer_name(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_identity, "device_name_or", lambda default: "DESK-WORKSTATIO")
    monkeypatch.setattr(socket, "gethostname", lambda: "desk-workstation-long")
    identity = memory_identity.load_runtime_identity(tmp_path)
    assert identity.source_node_name == "DESK-WORKSTATIO"
    # 显式传入仍然优先；记录包路径相关的 workspace/source_node_id 不受设备名影响。
    explicit = memory_identity.load_runtime_identity(tmp_path, {"source_node_name": "custom"})
    assert explicit.source_node_name == "custom"
    assert explicit.source_node_id == identity.source_node_id and explicit.workspace_id == identity.workspace_id


def test_local_lock_owner_still_uses_the_hostname():
    """锁持有者不是设备身份：升级时新旧进程必须按同一主机名识别过期锁。"""
    import inspect

    source = inspect.getsource(memory_locks)
    assert "socket.gethostname()" in source and "device_name" not in source

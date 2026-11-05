"""限流来源按真实客户端地址计算：只信任受信代理写入的 X-Forwarded-For。"""
import ipaddress
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from memory_hub.api.client_address import client_source
from memory_hub.config import load_settings

PROXY = ipaddress.ip_network("172.16.0.0/12")


def request(peer, forwarded=None, trusted=(PROXY,)):
    headers = [(b"x-forwarded-for", forwarded.encode())] if forwarded is not None else []
    app = SimpleNamespace(state=SimpleNamespace(settings=SimpleNamespace(trusted_proxies=trusted)))
    return Request({"type": "http", "headers": headers, "client": (peer, 1234), "app": app})


def test_forwarded_address_is_used_only_from_a_trusted_proxy():
    assert client_source(request("172.19.0.5", "203.0.113.7")) == "203.0.113.7"
    # 直连方不是受信代理时，伪造的转发头被忽略。
    assert client_source(request("198.51.100.9", "203.0.113.7")) == "198.51.100.9"
    # 未配置受信代理时保持原行为。
    assert client_source(request("172.19.0.5", "203.0.113.7", trusted=())) == "172.19.0.5"


def test_only_the_rightmost_entry_written_by_the_proxy_is_trusted():
    # 客户端自带的左侧值不可信；只取受信代理写入的最右一项。
    assert client_source(request("172.19.0.5", "1.1.1.1, 203.0.113.7")) == "203.0.113.7"
    # Caddy 看到的是 Docker 网关（如 IPv6 经 docker-proxy 转发）时如实返回，不再退回 Caddy 自身地址。
    assert client_source(request("172.19.0.5", "172.19.0.1")) == "172.19.0.1"
    assert client_source(request("172.19.0.5", "not-an-ip")) == "172.19.0.5"
    assert client_source(request("172.19.0.5", "")) == "172.19.0.5"
    assert client_source(request("172.19.0.5", "2001:db8::1")) == "2001:db8::1"


def test_trusted_proxy_setting_is_parsed_and_invalid_values_fail_loudly(monkeypatch):
    monkeypatch.setenv("MEMORY_HUB_TRUSTED_PROXIES", "172.16.0.0/12, 10.0.0.5")
    assert load_settings().trusted_proxies == (ipaddress.ip_network("172.16.0.0/12"), ipaddress.ip_network("10.0.0.5/32"))
    monkeypatch.setenv("MEMORY_HUB_TRUSTED_PROXIES", "not-a-network")
    with pytest.raises(ValueError):
        load_settings()
    monkeypatch.delenv("MEMORY_HUB_TRUSTED_PROXIES")
    assert load_settings().trusted_proxies == ()

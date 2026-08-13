"""
agent_bot stores the same hosts with an /api suffix, so a copied value must
not silently produce /api/api/... and 404 every cross-service call.
"""
import importlib

import pytest

import config.settings as settings_module


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://boxmanage.smartfleetllc.com", "https://boxmanage.smartfleetllc.com"),
        ("https://boxmanage.smartfleetllc.com/", "https://boxmanage.smartfleetllc.com"),
        ("https://boxmanage.smartfleetllc.com/api", "https://boxmanage.smartfleetllc.com"),
        ("https://boxmanage.smartfleetllc.com/api/", "https://boxmanage.smartfleetllc.com"),
        ("https://boxmanage.smartfleetllc.com/api/v1", "https://boxmanage.smartfleetllc.com"),
        ("https://boxmanage.smartfleetllc.com/api/v1/", "https://boxmanage.smartfleetllc.com"),
        ("http://localhost:8080", "http://localhost:8080"),
        ("", ""),
    ],
)
def test_base_url_is_reduced_to_the_origin(monkeypatch, raw, expected):
    monkeypatch.setenv("PEER_URL", raw)
    assert settings_module._base_url("PEER_URL") == expected


def test_a_host_named_api_is_not_mangled(monkeypatch):
    monkeypatch.setenv("PEER_URL", "https://api.example.com")
    assert settings_module._base_url("PEER_URL") == "https://api.example.com"


def test_config_exposes_origins(monkeypatch):
    monkeypatch.setenv("BOXTRUCK_BASE_URL", "https://tms.example.com/api")
    monkeypatch.setenv("ATREK_BASE_URL", "https://tms.example.com/api/v1")
    reloaded = importlib.reload(settings_module)
    try:
        assert reloaded.config.BOXTRUCK_BASE_URL == "https://tms.example.com"
        assert reloaded.config.ATREK_BASE_URL == "https://tms.example.com"
    finally:
        monkeypatch.undo()
        importlib.reload(settings_module)

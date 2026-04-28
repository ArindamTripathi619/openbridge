from __future__ import annotations

import pytest

from openbridge.opencode_bridge import (
    BridgeConfig,
    DEFAULT_OPENCODE_BACKOFF_BASE_MS,
    DEFAULT_OPENCODE_BACKOFF_MAX_MS,
    DEFAULT_OPENCODE_BACKOFF_FACTOR,
    DEFAULT_OPENCODE_BACKOFF_JITTER_PCT,
)


def base_mapping():
    return {"TELEGRAM_BOT_TOKEN": "x", "OPENCODE_WORKING_DIR": "."}


def test_backoff_defaults_from_env():
    cfg = BridgeConfig.from_mapping(base_mapping())
    assert cfg.opencode_backoff_base_ms == DEFAULT_OPENCODE_BACKOFF_BASE_MS
    assert cfg.opencode_backoff_max_ms == DEFAULT_OPENCODE_BACKOFF_MAX_MS
    assert abs(cfg.opencode_backoff_factor - DEFAULT_OPENCODE_BACKOFF_FACTOR) < 1e-9
    assert abs(cfg.opencode_backoff_jitter_pct - DEFAULT_OPENCODE_BACKOFF_JITTER_PCT) < 1e-9


def test_backoff_custom_values_parsed():
    m = base_mapping()
    m["OPENBRIDGE_OPENCODE_BACKOFF_BASE_MS"] = "100"
    m["OPENBRIDGE_OPENCODE_BACKOFF_MAX_MS"] = "2000"
    m["OPENBRIDGE_OPENCODE_BACKOFF_FACTOR"] = "1.5"
    m["OPENBRIDGE_OPENCODE_BACKOFF_JITTER_PCT"] = "0.5"
    cfg = BridgeConfig.from_mapping(m)
    assert cfg.opencode_backoff_base_ms == 100
    assert cfg.opencode_backoff_max_ms == 2000
    assert abs(cfg.opencode_backoff_factor - 1.5) < 1e-9
    assert abs(cfg.opencode_backoff_jitter_pct - 0.5) < 1e-9


def test_backoff_invalid_values_raise():
    m = base_mapping()
    m["OPENBRIDGE_OPENCODE_BACKOFF_BASE_MS"] = "0"
    with pytest.raises(ValueError):
        BridgeConfig.from_mapping(m)

    m = base_mapping()
    m["OPENBRIDGE_OPENCODE_BACKOFF_FACTOR"] = "1"
    with pytest.raises(ValueError):
        BridgeConfig.from_mapping(m)

    m = base_mapping()
    m["OPENBRIDGE_OPENCODE_BACKOFF_JITTER_PCT"] = "2.0"
    with pytest.raises(ValueError):
        BridgeConfig.from_mapping(m)

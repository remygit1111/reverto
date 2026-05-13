"""Tests for core/markets.py — the (exchange_type, market_type)
registry that everything (account store, exchange clients, routes,
engine boot) reads from.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from core import markets  # noqa: E402


class TestAllowedMarkets:

    def test_bitget_lists_all_four(self):
        # Display order matters — spot first, then derivatives —
        # because the frontend renders the dropdown in this order.
        assert markets.allowed_markets("bitget") == [
            "spot", "coin_m", "usdt_m", "usdc_m",
        ]

    def test_kraken_lists_spot_and_futures(self):
        assert markets.allowed_markets("kraken") == ["spot", "futures"]

    def test_unknown_exchange_returns_empty(self):
        # ``allowed_markets`` is the lenient lookup — used to build
        # UI dropdowns, where an unknown exchange should produce an
        # empty list rather than raise.
        assert markets.allowed_markets("ftx") == []


class TestGetMarketConfig:

    def test_bitget_coin_m_balance_currency_is_btc(self):
        cfg = markets.get_market_config("bitget", "coin_m")
        assert cfg["balance_currency"] == "BTC"
        assert cfg["ccxt_params"] == {"productType": "COIN-FUTURES"}

    def test_bitget_usdt_m_uses_usdt_balance(self):
        cfg = markets.get_market_config("bitget", "usdt_m")
        assert cfg["balance_currency"] == "USDT"
        assert cfg["ccxt_params"] == {"productType": "USDT-FUTURES"}

    def test_bitget_usdc_m_uses_usdc_balance(self):
        cfg = markets.get_market_config("bitget", "usdc_m")
        assert cfg["balance_currency"] == "USDC"
        assert cfg["ccxt_params"] == {"productType": "USDC-FUTURES"}

    def test_bitget_spot_uses_usdt_balance(self):
        cfg = markets.get_market_config("bitget", "spot")
        assert cfg["balance_currency"] == "USDT"
        assert cfg["ccxt_options"] == {"defaultType": "spot"}

    def test_kraken_spot_routes_to_kraken_class(self):
        cfg = markets.get_market_config("kraken", "spot")
        assert cfg["ccxt_client"] == "kraken"
        assert cfg["balance_currency"] == "USD"

    def test_kraken_futures_routes_to_krakenfutures_class(self):
        cfg = markets.get_market_config("kraken", "futures")
        assert cfg["ccxt_client"] == "krakenfutures"
        # Inverse-margined futures — balance is in BTC.
        assert cfg["balance_currency"] == "BTC"

    def test_unknown_exchange_raises(self):
        with pytest.raises(ValueError, match="Unknown exchange_type"):
            markets.get_market_config("ftx", "spot")

    def test_unknown_market_raises_with_supported_list(self):
        with pytest.raises(ValueError) as ei:
            markets.get_market_config("bitget", "options")
        msg = str(ei.value)
        assert "options" in msg
        # The error must list the supported alternatives so the
        # operator knows what to retry with.
        assert "spot" in msg
        assert "coin_m" in msg


class TestValidateCombo:

    def test_valid_combo_passes(self):
        # Each registered combination must not raise.
        for ex_name, ms in markets.MARKETS.items():
            for m_name in ms:
                markets.validate_combo(ex_name, m_name)

    def test_unknown_exchange_rejected(self):
        with pytest.raises(ValueError, match="Unknown exchange_type"):
            markets.validate_combo("ftx", "spot")

    def test_unknown_market_rejected(self):
        with pytest.raises(ValueError, match="not supported"):
            markets.validate_combo("bitget", "options")

    def test_cross_exchange_market_rejected(self):
        # Kraken's "futures" is not a valid Bitget market_type even
        # though both exchanges support futures-style products under
        # different names.
        with pytest.raises(ValueError):
            markets.validate_combo("bitget", "futures")
        with pytest.raises(ValueError):
            markets.validate_combo("kraken", "coin_m")


class TestContractTypeValidation:
    """``inverse_perpetual`` bots only make sense against a coin-
    margined wallet. The engine-boot check refuses any other combo so
    the bot doesn't end up routing orders into the wrong wallet."""

    def test_inverse_perpetual_on_bitget_coin_m_passes(self):
        markets.validate_contract_type(
            "inverse_perpetual", "bitget", "coin_m",
        )

    def test_inverse_perpetual_on_kraken_futures_passes(self):
        markets.validate_contract_type(
            "inverse_perpetual", "kraken", "futures",
        )

    def test_inverse_perpetual_on_bitget_usdt_m_rejected(self):
        with pytest.raises(ValueError, match="inverse_perpetual"):
            markets.validate_contract_type(
                "inverse_perpetual", "bitget", "usdt_m",
            )

    def test_inverse_perpetual_on_bitget_spot_rejected(self):
        with pytest.raises(ValueError):
            markets.validate_contract_type(
                "inverse_perpetual", "bitget", "spot",
            )

    def test_unknown_contract_type_passes_through(self):
        # Additive — until a contract_type is explicitly characterised
        # the validator must not block the operator. A future
        # ``spot_rebalance`` (or whatever) lands in the registry
        # alongside the supporting markets and then the check kicks in.
        markets.validate_contract_type(
            "something-future", "bitget", "spot",
        )


class TestSupportedExchangesPayload:

    def test_payload_shape_matches_endpoint_contract(self):
        # The frontend reads this exact shape from
        # /api/exchanges/supported. Pinning it here catches a future
        # field-rename in core.markets that would silently break the
        # admin tile's market dropdown.
        payload = markets.supported_exchanges_payload()
        assert isinstance(payload, list)
        names = [e["name"] for e in payload]
        assert "bitget" in names
        assert "kraken" in names
        bitget = next(e for e in payload if e["name"] == "bitget")
        assert isinstance(bitget["markets"], list)
        keys = [m["key"] for m in bitget["markets"]]
        assert keys == ["spot", "coin_m", "usdt_m", "usdc_m"]
        for m in bitget["markets"]:
            assert "label" in m
            assert isinstance(m["label"], str) and m["label"]

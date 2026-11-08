"""Cost estimates: price variables, parsing them, and the sums."""

import logging

import pytest

from app.pricing import cost, parse_price, price_var, read_prices


def test_price_var_names_follow_the_model():
    assert price_var("gpt-6.1-sol") == "PAINTY_PRICE_GPT_6_1_SOL"
    assert price_var("gpt-5.4-mini") == "PAINTY_PRICE_GPT_5_4_MINI"
    assert price_var("org/Model--x ") == "PAINTY_PRICE_ORG_MODEL_X"


def test_a_billed_amount_travels_with_the_estimate():
    prices = {"PAINTY_PRICE_M": (1.0, 0.1, 2.0, 1.25)}
    assert cost("m", 1000, 0, 0, prices, billed=0.5)["billed"] == 0.5
    assert cost("unpriced", 1000, 0, 0, prices, billed=0.5) == {
        "usd": None, "var": "PAINTY_PRICE_UNPRICED", "billed": 0.5}
    assert "billed" not in cost("m", 1000, 0, 0, prices)


def test_prices_are_read_with_two_three_or_four_numbers():
    assert parse_price("2/0.1/10/2.5") == (2.0, 0.1, 10.0, 2.5)
    assert parse_price("1.25/0.125/10") == (1.25, 0.125, 10.0, 1.25)  # writes cost the same as input
    assert parse_price(" 1.25 / 10 ") == (1.25, 1.25, 10.0, 1.25)     # and so does cached
    for bad in ("", "1.25", "1/2/3/4/5", "a/b/c", "-1/0/2", "nan/0/1", "inf/0/1"):
        assert parse_price(bad) is None, bad


def test_only_price_variables_are_read_and_bad_ones_are_skipped(caplog):
    env = {"PAINTY_PRICE_GPT_6_LUNA": "1/0.1/8", "PAINTY_PRICE_BLANK": "  ",
           "PAINTY_PRICE_BROKEN": "cheap", "PAINTY_MODELS": "gpt-6-luna"}
    with caplog.at_level(logging.WARNING, logger="painty"):
        prices = read_prices(env)
    assert prices == {"PAINTY_PRICE_GPT_6_LUNA": (1.0, 0.1, 8.0, 1.0)}
    assert "PAINTY_PRICE_BROKEN" in caplog.text and "PAINTY_PRICE_BLANK" not in caplog.text


def test_cost_charges_uncached_cached_and_output_at_their_own_rates():
    prices = {"PAINTY_PRICE_GPT_6_LUNA": (2.0, 0.5, 10.0, 2.0)}
    c = cost("gpt-6-luna", input_tokens=1_000_000, cached_tokens=600_000,
             output_tokens=100_000, prices=prices)
    # 400k uncached at $2, 600k cached at $0.50, 100k out at $10
    assert c["usd"] == pytest.approx(0.8 + 0.3 + 1.0)
    assert c["rates"] == [2.0, 0.5, 10.0, 2.0] and c["var"] == "PAINTY_PRICE_GPT_6_LUNA"


def test_cache_writes_replace_the_input_rate_for_their_tokens():
    prices = {"PAINTY_PRICE_M": (1.0, 0.1, 5.0, 1.25)}
    c = cost("m", input_tokens=1_000_000, cached_tokens=500_000, output_tokens=0,
             prices=prices, cache_write_tokens=200_000)
    # 300k plain input at $1, 500k cached at $0.10, 200k written at $1.25: never billed twice
    assert c["usd"] == pytest.approx(0.3 + 0.05 + 0.25)


def test_cost_without_a_price_says_which_variable_to_set():
    assert cost("gpt-6-luna", 1000, 0, 100, {}) == {"usd": None, "var": "PAINTY_PRICE_GPT_6_LUNA"}


def test_cost_copes_with_older_paintings_missing_counts():
    prices = {"PAINTY_PRICE_M": (1.0, 0.1, 2.0, 1.25)}
    assert cost("m", 1_000_000, None, None, prices, None)["usd"] == pytest.approx(1.0)

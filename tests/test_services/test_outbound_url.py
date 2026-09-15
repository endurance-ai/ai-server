"""Tests for partner attribution on outbound product URLs."""

from app.services.outbound_url import with_partner_attribution


def test_slowsteadyclub_url_gets_required_utm_and_preserves_query_and_fragment():
    actual = with_partner_attribution("https://slowsteadyclub.com/product/detail.html?product_no=27440#reviews")

    assert actual == (
        "https://slowsteadyclub.com/product/detail.html?product_no=27440"
        "&utm_source=kiko&utm_medium=referral&utm_campaign=slowsteadyclub#reviews"
    )


def test_slowsteadyclub_www_replaces_existing_partner_utm_without_duplicates():
    actual = with_partner_attribution(
        "https://www.slowsteadyclub.com/p/1?utm_source=old&utm_medium=email&utm_campaign=legacy&color=navy"
    )

    assert actual == (
        "https://www.slowsteadyclub.com/p/1?color=navy&utm_source=kiko&utm_medium=referral&utm_campaign=slowsteadyclub"
    )


def test_other_merchants_and_malformed_urls_are_unchanged():
    other = "https://example.com/p/1?utm_source=merchant"
    malformed = "https://[invalid"

    assert with_partner_attribution(other) == other
    assert with_partner_attribution(malformed) == malformed

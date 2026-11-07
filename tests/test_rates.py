import httpx
import pytest

from bot.services.rates import CBU_URL, RatesService, RateUnavailableError, convert, parse_cbu

CBU_SAMPLE = [{
    "id": 1, "Code": "840", "Ccy": "USD", "CcyNm_RU": "Доллар США", "Nominal": "1",
    "Rate": "11987.16", "Diff": "13.68", "Date": "07.11.2026",
}]


def test_convert():
    assert convert(10, "USD", "UZS", 12000) == 120000
    assert convert(60000, "UZS", "USD", 12000) == 5
    assert convert(5, "UZS", "UZS", 12000) == 5
    with pytest.raises(ValueError):
        convert(1, "USD", "UZS", 0)
    with pytest.raises(ValueError):
        convert(1, "EUR", "UZS", 1)


def test_parse_cbu():
    assert parse_cbu(CBU_SAMPLE) == (11987.16, "2026-11-07")
    with pytest.raises(ValueError):
        parse_cbu([{"Ccy": "EUR", "Rate": "1", "Date": "07.11.2026"}])


def service(db, handler):
    return RatesService(db, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_rates_cached_and_saved(db):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        assert str(request.url) == CBU_URL
        return httpx.Response(200, json=CBU_SAMPLE)

    rates = service(db, handler)
    info = await rates.get_usd_rate()
    await rates.get_usd_rate()
    assert info.rate == 11987.16 and not info.stale and calls["n"] == 1
    assert (await db.last_rate())["usd_uzs"] == 11987.16


async def test_rates_fallback_to_saved(db):
    await db.save_rate("2026-11-06", 11950.0, "2026-11-06 05:00:00")
    rates = service(db, lambda r: httpx.Response(503))
    info = await rates.get_usd_rate()
    assert info.stale and info.rate == 11950.0 and "06.11.2026" in info.warning()


async def test_rates_unavailable(db):
    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(RateUnavailableError):
        await service(db, handler).get_usd_rate()

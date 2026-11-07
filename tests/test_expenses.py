import pytest

from bot.services.expenses import ExpenseInputError, compute_totals, fmt_money, parse_expense_input


@pytest.mark.parametrize(
    "raw, amount, currency, category, note",
    [
        ("такси 25000", 25000, "UZS", "Такси", None),
        ("еда 45 000 обед с коллегами", 45000, "UZS", "Еда", "обед с коллегами"),
        ("кофе 3 usd", 3, "USD", "Кофе", None),
        ("$12.5 подписка", 12.5, "USD", "Подписка", None),
        ("1,5 млн аренда", 1_500_000, "UZS", "Аренда", None),
        ("25к такси", 25000, "UZS", "Такси", None),
        ("30 тыс сум продукты", 30000, "UZS", "Продукты", None),
        ("50000", 50000, "UZS", "Другое", None),
    ],
)
def test_parse_expense(raw, amount, currency, category, note):
    p = parse_expense_input(raw)
    assert (p.amount, p.currency, p.category, p.note) == (amount, currency, category, note)


@pytest.mark.parametrize("raw", ["такси", "0 сум еда", "2000000 usd яхта", "еда 5000 " + "x" * 300])
def test_parse_expense_invalid(raw):
    with pytest.raises(ExpenseInputError):
        parse_expense_input(raw)


def test_totals_both_currencies():
    rows = [
        {"amount": 120000, "currency": "UZS", "category": "Еда", "rate_used": 12000},
        {"amount": 10, "currency": "USD", "category": "Такси", "rate_used": 12000},
        {"amount": 5, "currency": "USD", "category": "Еда", "rate_used": None},  # курс текущий
    ]
    t = compute_totals(rows, fallback_rate=10000)
    assert t.native == {"UZS": 120000, "USD": 15}
    assert t.uzs == 120000 + 120000 + 50000
    assert t.usd == pytest.approx(10 + 10 + 5)
    assert t.by_category_uzs == {"Еда": 170000, "Такси": 120000}


def test_totals_without_rate():
    t = compute_totals([{"amount": 5, "currency": "USD", "category": "X", "rate_used": None}], None)
    assert t.uzs is None and t.usd is None and t.native == {"USD": 5}


def test_fmt_money():
    assert fmt_money(1234567, "UZS") == "1 234 567 сум"
    assert fmt_money(12.5, "USD") == "$12.50"

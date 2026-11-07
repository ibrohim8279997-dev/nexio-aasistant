from datetime import date, timedelta

from bot.services import charts
from bot.services.tracker import compute_streak

TODAY = date(2026, 11, 7)


def test_streak():
    d = lambda n: TODAY - timedelta(days=n)  # noqa: E731
    assert compute_streak({d(0), d(1), d(2), d(4)}, TODAY) == 3
    assert compute_streak({d(1), d(2)}, TODAY) == 2  # сегодня ещё можно продолжить
    assert compute_streak({d(2), d(3)}, TODAY) == 0
    assert compute_streak(set(), TODAY) == 0


def test_charts_render_png():
    days = [TODAY - timedelta(days=i) for i in range(30)][::-1]
    pngs = [
        charts.expenses_by_day(days, [i * 10000.0 for i in range(30)]),
        charts.expenses_by_category(["Еда", "Транспорт", "Покупки"], [500000, 120000, 1_500_000]),
        charts.tasks_by_day(days[-14:], [1, 2, 0] * 4 + [1, 1], [2, 2, 1] * 4 + [3, 1]),
    ]
    assert all(p.startswith(b"\x89PNG") and len(p) > 10_000 for p in pngs)

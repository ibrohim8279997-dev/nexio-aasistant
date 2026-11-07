"""Графики matplotlib (headless, backend Agg) в едином стиле. Функции возвращают PNG-байты."""
from __future__ import annotations

import io
import threading
from collections.abc import Sequence
from datetime import date

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

PRIMARY = "#3B82F6"
SUCCESS = "#22C55E"
DANGER = "#EF4444"
MUTED = "#CBD5E1"
TEXT = "#1E293B"
GRID = "#E2E8F0"

plt.rcParams.update(
    {
        "font.family": "DejaVu Sans",  # есть кириллица, входит в matplotlib
        "font.size": 11,
        "axes.titlesize": 15,
        "axes.titleweight": "bold",
        "axes.titlepad": 14,
        "axes.edgecolor": GRID,
        "axes.labelcolor": TEXT,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "xtick.color": TEXT,
        "ytick.color": TEXT,
        "text.color": TEXT,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "figure.dpi": 100,
    }
)


# pyplot не потокобезопасен, а графики строятся через asyncio.to_thread
_LOCK = threading.Lock()


def _locked(func):
    def wrapper(*args, **kwargs):
        with _LOCK:
            return func(*args, **kwargs)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper


def _money_short(value: float, _pos: int | None = None) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}".rstrip("0").rstrip(".") + " млн"
    if value >= 1_000:
        return f"{value / 1_000:.0f} тыс"
    return f"{value:.0f}"


def _png(fig: plt.Figure) -> bytes:
    buf = io.BytesIO()
    fig.tight_layout()
    fig.savefig(buf, format="png", dpi=150)
    plt.close(fig)
    return buf.getvalue()


def _day_labels(days: Sequence[date]) -> list[str]:
    step = 1 if len(days) <= 14 else 3
    return [d.strftime("%d.%m") if i % step == 0 or i == len(days) - 1 else "" for i, d in enumerate(days)]


@_locked
def expenses_by_day(days: Sequence[date], values_uzs: Sequence[float]) -> bytes:
    fig, ax = plt.subplots(figsize=(10, 5))
    xs = range(len(days))
    ax.bar(xs, values_uzs, color=PRIMARY, width=0.7, zorder=2)
    ax.set_xticks(list(xs), _day_labels(days), rotation=45, ha="right")
    ax.yaxis.set_major_formatter(FuncFormatter(_money_short))
    ax.grid(axis="y", color=GRID, zorder=0)
    total = sum(values_uzs)
    avg = total / len(days) if days else 0
    if avg:
        ax.axhline(avg, color=DANGER, linestyle="--", linewidth=1.2, zorder=3)
    ax.set_ylim(0, max(values_uzs or [1]) * 1.1 or 1)
    ax.set_title(
        f"Расходы по дням — всего {_money_short(total)} сум\n"
        f"в среднем {_money_short(avg)} сум/день (красная линия)",
        linespacing=1.6,
    )
    ax.set_ylabel("сум")
    return _png(fig)


@_locked
def expenses_by_category(categories: Sequence[str], values_uzs: Sequence[float]) -> bytes:
    pairs = sorted(zip(categories, values_uzs), key=lambda p: p[1])[-10:]
    cats = [c[:22] for c, _ in pairs]
    vals = [v for _, v in pairs]
    total = sum(values_uzs) or 1
    fig, ax = plt.subplots(figsize=(10, max(3.5, 0.6 * len(cats) + 1.5)))
    bars = ax.barh(cats, vals, color=PRIMARY, height=0.6, zorder=2)
    ax.xaxis.set_major_formatter(FuncFormatter(_money_short))
    ax.grid(axis="x", color=GRID, zorder=0)
    for bar, value in zip(bars, vals):
        ax.text(
            bar.get_width(), bar.get_y() + bar.get_height() / 2,
            f"  {_money_short(value)} ({value / total:.0%})", va="center", fontsize=10,
        )
    ax.set_xlim(0, max(vals) * 1.3 if vals else 1)
    ax.set_title("Расходы по категориям за 30 дней, сум")
    return _png(fig)


@_locked
def tasks_by_day(days: Sequence[date], done: Sequence[int], total: Sequence[int]) -> bytes:
    fig, ax = plt.subplots(figsize=(10, 5))
    xs = list(range(len(days)))
    open_ = [t - d for d, t in zip(done, total)]
    ax.bar(xs, done, color=SUCCESS, width=0.7, label="Выполнено", zorder=2)
    ax.bar(xs, open_, bottom=done, color=MUTED, width=0.7, label="Не выполнено", zorder=2)
    for x, d, t in zip(xs, done, total):
        if t:
            ax.text(x, t + 0.05, f"{d / t:.0%}", ha="center", va="bottom", fontsize=9)
    ax.set_xticks(xs, _day_labels(days), rotation=45, ha="right")
    ax.yaxis.get_major_locator().set_params(integer=True)
    ax.set_ylim(0, max(total or [1]) * 1.25 + 0.5)
    ax.grid(axis="y", color=GRID, zorder=0)
    ax.legend(frameon=False, loc="upper left")
    s_done, s_total = sum(done), sum(total)
    rate = f" — {s_done / s_total:.0%}" if s_total else ""
    ax.set_title(f"Выполнение задач по дням{rate}")
    ax.set_ylabel("задач")
    return _png(fig)

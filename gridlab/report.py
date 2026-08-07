"""Standalone HTML report: price with grid levels, equity curve, PnL breakdown.

No dependencies and no network — the charts are hand-built SVG, so the file
opens anywhere and keeps working offline.
"""

from __future__ import annotations

import datetime as dt
import html
import os
from typing import List, Optional, Sequence, Tuple

from .engine import BacktestResult

W, H = 1000, 320
PAD_L, PAD_R, PAD_T, PAD_B = 62, 18, 18, 30
MAX_POINTS = 900


def _fmt(value: float, digits: int = 2) -> str:
    return f"{value:,.{digits}f}".replace(",", " ")


def _date(ts: int) -> str:
    return dt.datetime.utcfromtimestamp(ts / 1000).strftime("%Y-%m-%d")


def _downsample(series: Sequence[Tuple[int, float]], limit: int = MAX_POINTS
                ) -> List[Tuple[int, float]]:
    if len(series) <= limit:
        return list(series)
    stride = len(series) / limit
    picked = [series[min(len(series) - 1, int(i * stride))] for i in range(limit)]
    if picked[-1] != series[-1]:
        picked.append(series[-1])
    return picked


class _Scale:
    def __init__(self, xs: Sequence[float], ys: Sequence[float], pad: float = 0.04):
        self.x0, self.x1 = min(xs), max(xs)
        lo, lo_hi = min(ys), max(ys)
        span = (lo_hi - lo) or (abs(lo) or 1.0)
        self.y0 = lo - span * pad
        self.y1 = lo_hi + span * pad
        if self.x1 == self.x0:
            self.x1 = self.x0 + 1

    def px(self, x: float) -> float:
        return PAD_L + (x - self.x0) / (self.x1 - self.x0) * (W - PAD_L - PAD_R)

    def py(self, y: float) -> float:
        return H - PAD_B - (y - self.y0) / (self.y1 - self.y0) * (H - PAD_T - PAD_B)


def _axes(scale: _Scale, ts_min: int, ts_max: int, digits: int = 0) -> str:
    parts: List[str] = []
    for i in range(5):
        value = scale.y0 + (scale.y1 - scale.y0) * i / 4
        y = scale.py(value)
        parts.append(f'<line class="grid" x1="{PAD_L}" y1="{y:.1f}" x2="{W - PAD_R}" y2="{y:.1f}"/>')
        parts.append(f'<text class="tick" x="{PAD_L - 8}" y="{y + 4:.1f}" '
                     f'text-anchor="end">{_fmt(value, digits)}</text>')
    for i in range(4):
        frac = i / 3
        x = PAD_L + frac * (W - PAD_L - PAD_R)
        ts = int(ts_min + (ts_max - ts_min) * frac)
        anchor = "start" if i == 0 else ("end" if i == 3 else "middle")
        parts.append(f'<text class="tick" x="{x:.1f}" y="{H - 8}" '
                     f'text-anchor="{anchor}">{_date(ts)}</text>')
    return "".join(parts)


def _polyline(points: Sequence[Tuple[float, float]], css: str) -> str:
    if not points:
        return ""
    coords = " ".join(f"{x:.1f},{y:.1f}" for x, y in points)
    return f'<polyline class="{css}" points="{coords}"/>'


def _price_chart(result: BacktestResult) -> str:
    series = _downsample(result.prices)
    xs = [float(t) for t, _ in series]
    ys = [p for _, p in series]
    lo = min(min(ys), result.config.lower)
    hi = max(max(ys), result.config.upper)
    scale = _Scale(xs, [lo, hi])

    body: List[str] = [_axes(scale, int(xs[0]), int(xs[-1]), 0)]

    top = scale.py(result.config.upper)
    bottom = scale.py(result.config.lower)
    body.append(f'<rect class="zone" x="{PAD_L}" y="{top:.1f}" '
                f'width="{W - PAD_L - PAD_R}" height="{max(bottom - top, 0):.1f}"/>')

    step = max(1, len(result.levels) // 40)
    for level in result.levels[::step]:
        y = scale.py(level)
        body.append(f'<line class="level" x1="{PAD_L}" y1="{y:.1f}" '
                    f'x2="{W - PAD_R}" y2="{y:.1f}"/>')

    body.append(_polyline([(scale.px(x), scale.py(y)) for x, y in zip(xs, ys)], "price"))

    shown = [t for t in result.trades if t.kind in ("init", "exit", "liquidation")]
    for trade in shown[:60]:
        x, y = scale.px(trade.ts), scale.py(trade.price)
        css = "mark-exit" if trade.kind != "init" else "mark-init"
        body.append(f'<circle class="{css}" cx="{x:.1f}" cy="{y:.1f}" r="4"/>')

    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Prix et niveaux de grille">' \
           + "".join(body) + "</svg>"


def _equity_chart(result: BacktestResult) -> str:
    series = _downsample(result.equity)
    if not series:
        return ""
    xs = [float(t) for t, _ in series]
    ys = [v for _, v in series]

    invest = result.config.investment
    price_series = dict(_downsample(result.prices))
    first_price = result.metrics["first_price"]
    hold = [invest * (1 + (price_series.get(int(t), first_price) / first_price - 1)
                      * result.config.leverage) for t in xs]

    scale = _Scale(xs, ys + hold + [invest])
    body = [_axes(scale, int(xs[0]), int(xs[-1]), 0)]

    base_y = scale.py(invest)
    body.append(f'<line class="baseline" x1="{PAD_L}" y1="{base_y:.1f}" '
                f'x2="{W - PAD_R}" y2="{base_y:.1f}"/>')
    body.append(_polyline([(scale.px(x), scale.py(y)) for x, y in zip(xs, hold)], "hold"))
    body.append(_polyline([(scale.px(x), scale.py(y)) for x, y in zip(xs, ys)], "equity"))

    return f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Courbe d\'equite">' \
           + "".join(body) + "</svg>"


def _breakdown_chart(result: BacktestResult) -> str:
    m = result.metrics
    items = [
        ("PnL realise", m["realized_pnl"]),
        ("Frais", -m["fees_paid"]),
        ("Funding", -m["funding_paid"]),
        ("Position ouverte", m["unrealized_pnl"]),
        ("PnL net", m["net_pnl"]),
    ]
    height = 220
    width = 1000
    span = max(abs(v) for _, v in items) or 1.0
    zero_x = 300
    usable = width - zero_x - 130
    bar_h = 26
    gap = 14

    parts: List[str] = [f'<line class="baseline" x1="{zero_x}" y1="10" '
                        f'x2="{zero_x}" y2="{height - 10}"/>']
    for i, (label, value) in enumerate(items):
        y = 18 + i * (bar_h + gap)
        length = abs(value) / span * usable
        x = zero_x if value >= 0 else zero_x - length
        css = "bar-pos" if value >= 0 else "bar-neg"
        if label == "PnL net":
            css = "bar-net-pos" if value >= 0 else "bar-net-neg"
        parts.append(f'<text class="bar-label" x="{zero_x - 12}" y="{y + 18}" '
                     f'text-anchor="end">{html.escape(label)}</text>')
        parts.append(f'<rect class="{css}" x="{x:.1f}" y="{y}" '
                     f'width="{max(length, 1):.1f}" height="{bar_h}" rx="3"/>')
        text_x = zero_x + length + 10 if value >= 0 else zero_x - length - 10
        anchor = "start" if value >= 0 else "end"
        parts.append(f'<text class="bar-value" x="{text_x:.1f}" y="{y + 18}" '
                     f'text-anchor="{anchor}">{value:+,.2f}</text>'.replace(",", " "))

    return f'<svg viewBox="0 0 {width} {height}" role="img" ' \
           f'aria-label="Decomposition du PnL">' + "".join(parts) + "</svg>"


def _cards(result: BacktestResult) -> str:
    m = result.metrics
    cfg = result.config
    cards = [
        ("PnL net", f"{m['net_pnl']:+,.2f} USDT".replace(",", " "),
         f"{m['return_pct']:+.2f}% sur {m['days']:.0f} j", m["net_pnl"] >= 0),
        ("APR", f"{m['apr_pct']:+.1f}%", "annualise, hors composition", m["apr_pct"] >= 0),
        ("Cycles fermes", f"{m['matched_cycles']:,}".replace(",", " "),
         f"{m['cycles_per_day']:.1f} / jour | brut {m['cycle_profit_gross']:+.0f} USDT",
         True),
        ("Drawdown max", f"{m['max_drawdown_pct']:.2f}%",
         "sur l'equite totale", m["max_drawdown_pct"] < 25),
        ("Temps dans range", f"{m['time_in_range_pct']:.0f}%",
         f"grille {_fmt(cfg.lower)} - {_fmt(cfg.upper)}", m["time_in_range_pct"] > 70),
        ("Buy & hold", f"{m['buy_hold_pct']:+.2f}%",
         f"meme levier x{cfg.leverage:g}", m["buy_hold_pct"] <= m["return_pct"]),
    ]
    out = []
    for title, value, note, good in cards:
        tone = "good" if good else "bad"
        out.append(
            f'<div class="card"><div class="card-title">{html.escape(title)}</div>'
            f'<div class="card-value {tone}">{html.escape(value)}</div>'
            f'<div class="card-note">{html.escape(note)}</div></div>'
        )
    return "".join(out)


def _params_table(result: BacktestResult) -> str:
    cfg = result.config
    m = result.metrics
    liq = m["liq_price"]
    rows = [
        ("Direction", cfg.direction),
        ("Range", f"{_fmt(cfg.lower)} - {_fmt(cfg.upper)} USDT"),
        ("Grilles", f"{cfg.n_grids} ({cfg.mode}), pas {m['step_pct']:.3f}%"),
        ("Investissement", f"{_fmt(cfg.investment)} USDT"),
        ("Levier / notionnel", f"x{cfg.leverage:g} / {_fmt(cfg.notional)} USDT"),
        ("Frais maker / taker", f"{cfg.maker_fee * 100:.3f}% / {cfg.taker_fee * 100:.3f}%"),
        ("Ordres executes", f"{m['trades']}"),
        ("Position finale", f"{m['final_position']:+.6f} @ {_fmt(m['final_entry'])}"),
        ("Prix de liquidation", _fmt(liq) if liq and liq > 0 else "hors de portee"),
        ("Statut", "LIQUIDE" if result.liquidated else (result.stop_reason or "termine")),
    ]
    body = "".join(
        f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows
    )
    return f"<table class='params'>{body}</table>"


CSS = """
:root{--bg:#fbfbfa;--fg:#1c1b19;--muted:#6b6862;--line:#e3e1dc;--card:#fff;
--accent:#2f6f4e;--warn:#b3452c;--price:#3b3a37;--hold:#a9a49b;--zone:#2f6f4e14;}
@media (prefers-color-scheme:dark){:root{--bg:#141413;--fg:#eeece7;--muted:#9b968d;
--line:#2c2b28;--card:#1d1c1a;--accent:#6cc39a;--warn:#e08163;--price:#d6d3cc;
--hold:#6a665f;--zone:#6cc39a1a;}}
:root[data-theme=dark]{--bg:#141413;--fg:#eeece7;--muted:#9b968d;--line:#2c2b28;
--card:#1d1c1a;--accent:#6cc39a;--warn:#e08163;--price:#d6d3cc;--hold:#6a665f;
--zone:#6cc39a1a;}
:root[data-theme=light]{--bg:#fbfbfa;--fg:#1c1b19;--muted:#6b6862;--line:#e3e1dc;
--card:#fff;--accent:#2f6f4e;--warn:#b3452c;--price:#3b3a37;--hold:#a9a49b;
--zone:#2f6f4e14;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif;}
.wrap{max-width:1060px;margin:0 auto;padding:40px 20px 72px}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.sub{color:var(--muted);margin:0 0 28px;font-size:14px}
h2{font-size:15px;text-transform:uppercase;letter-spacing:.07em;color:var(--muted);
margin:36px 0 12px;font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(165px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card-title{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.05em}
.card-value{font-size:23px;font-weight:600;margin:6px 0 2px;
font-variant-numeric:tabular-nums}
.card-value.good{color:var(--accent)}.card-value.bad{color:var(--warn)}
.card-note{font-size:12px;color:var(--muted)}
.panel{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:10px 6px;overflow-x:auto}
svg{display:block;width:100%;height:auto;min-width:640px}
.grid{stroke:var(--line);stroke-width:1}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.level{stroke:var(--accent);stroke-width:.6;opacity:.32}
.zone{fill:var(--zone)}
.price{fill:none;stroke:var(--price);stroke-width:1.4}
.equity{fill:none;stroke:var(--accent);stroke-width:1.8}
.hold{fill:none;stroke:var(--hold);stroke-width:1.3;stroke-dasharray:4 3}
.baseline{stroke:var(--muted);stroke-width:1;stroke-dasharray:3 3;opacity:.6}
.mark-init{fill:var(--accent)}.mark-exit{fill:var(--warn)}
.bar-pos{fill:var(--accent);opacity:.55}.bar-neg{fill:var(--warn);opacity:.55}
.bar-net-pos{fill:var(--accent)}.bar-net-neg{fill:var(--warn)}
.bar-label{fill:var(--fg);font-size:13px}
.bar-value{fill:var(--muted);font-size:12px;font-variant-numeric:tabular-nums}
table.params{border-collapse:collapse;width:100%;font-size:14px}
table.params th{text-align:left;font-weight:500;color:var(--muted);
padding:7px 14px 7px 0;white-space:nowrap;width:210px}
table.params td{padding:7px 0;font-variant-numeric:tabular-nums;
border-bottom:1px solid var(--line)}
.legend{display:flex;gap:18px;font-size:12px;color:var(--muted);
padding:8px 14px 2px;flex-wrap:wrap}
.swatch{display:inline-block;width:14px;height:3px;vertical-align:middle;margin-right:6px}
.note{color:var(--muted);font-size:13px;margin-top:10px}
.warn{border-left:3px solid var(--warn);padding:10px 14px;background:var(--card);
border-radius:0 8px 8px 0;margin-top:16px;font-size:14px}
"""


def render_html(result: BacktestResult, title: str = "GridLab") -> str:
    m = result.metrics
    cfg = result.config
    subtitle = (f"{_date(result.prices[0][0])} &rarr; {_date(result.prices[-1][0])} "
                f"&middot; {m['bars']} bougies &middot; grille {cfg.direction} "
                f"x{cfg.n_grids} &middot; levier x{cfg.leverage:g}")

    warning = ""
    if result.liquidated:
        warning = ('<div class="warn"><strong>Position liquidee.</strong> '
                   "Le capital alloue est perdu&nbsp;: la grille n'a pas survecu a la "
                   "sortie de range avec ce levier.</div>")
    elif m["unrealized_pnl"] < -abs(m["realized_pnl"]) * 0.5 and m["unrealized_pnl"] < 0:
        warning = ('<div class="warn"><strong>Perte latente superieure a la moitie du '
                   "profit de grille.</strong> Le bot &laquo;&nbsp;gagne&nbsp;&raquo; "
                   "sur les cycles mais accumule une position perdante.</div>")

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<div class="wrap">
<h1>{html.escape(title)}</h1>
<p class="sub">{subtitle}</p>
<div class="cards">{_cards(result)}</div>
{warning}

<h2>Prix et niveaux de grille</h2>
<div class="panel">
<div class="legend"><span><span class="swatch" style="background:var(--price)"></span>
Prix</span><span><span class="swatch" style="background:var(--accent)"></span>
Niveaux de grille</span><span><span class="swatch" style="background:var(--warn)"></span>
Entree / sortie forcee</span></div>
{_price_chart(result)}</div>

<h2>Equite vs buy &amp; hold</h2>
<div class="panel">
<div class="legend"><span><span class="swatch" style="background:var(--accent)"></span>
Equite du bot</span><span><span class="swatch" style="background:var(--hold)"></span>
Buy &amp; hold au meme levier</span></div>
{_equity_chart(result)}</div>

<h2>D&eacute;composition du PnL</h2>
<div class="panel">{_breakdown_chart(result)}</div>
<p class="note">Le profit de grille est brut&nbsp;: les frais, le funding et la
position encore ouverte s'y soustraient. C'est cette derni&egrave;re ligne qui
d&eacute;cide du r&eacute;sultat r&eacute;el.</p>

<h2>Param&egrave;tres</h2>
{_params_table(result)}
<p class="note">Simulation sur donn&eacute;es historiques. Les remplissages
intrabar sont estim&eacute;s, le slippage et les rejets d'ordre ne sont pas
mod&eacute;lis&eacute;s. Les performances pass&eacute;es ne pr&eacute;disent
rien.</p>
</div></body></html>"""


def write_report(result: BacktestResult, path: str, title: str = "GridLab") -> str:
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(render_html(result, title))
    return os.path.abspath(path)

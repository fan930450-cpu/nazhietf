#!/usr/bin/env python3
"""刷新 159501 净值、行情、纳指同类溢价，并重写 data.js。

系数与 index.html 里的 drawdownCoef / premiumCoef 一致。
只访问公开网页，不使用密钥。任一数据源失败时，沿用 data/ 里上一份成功快照。
"""
from __future__ import annotations

import csv
import json
import re
import statistics
import time
import urllib.parse
import urllib.request
from calendar import monthrange
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

CST = timezone(timedelta(hours=8))
PRIMARY = "159501"
# 计算器与顶部金额的默认基准。页面预填、自检和写入 briefing 都用这个数。
DEFAULT_BASELINE = 3000.0
# 标普500是另一条定投，基准 2000，回撤阶梯和纳指不同。主标的用短名单里的 513500。
SPX_BASELINE = 2000.0
SPX_PRIMARY = "513500"
# 距高点回撤（正数百分点）→ 回撤系数。不足 5% 为 1.0；超过最后一档封顶。
# 相邻锚点之间线性。例如 7.5%→2.5，12.5%→5.0。
_DD_ANCHORS: tuple[tuple[float, float], ...] = (
    (5.0, 1.67),
    (10.0, 3.33),
    (15.0, 6.67),
    (20.0, 8.33),
    (25.0, 10.0),
    (30.0, 11.67),
    (40.0, 13.33),
)
# 页面策略只在「纳指100」同类里挑更便宜的一只，不拿标普/日经来替换。
KNOWN_NASDAQ = {
    "159501",
    "159513",
    "159632",
    "159659",
    "159660",
    "159696",
    "159941",
    "513100",
    "513110",
    "513300",
    "513390",
    "513870",
}
# 短名单：先纳指，再标普。条件性代码始终展示，但只在价差回到约 2 个点内才可能成为今日优先。
PICK_GROUPS = (
    {
        "id": "nasdaq-100",
        "title": "纳指100",
        "core": ("159513", "159632", "513110"),
        "conditional": ("159501",),
    },
    {
        "id": "sp-500",
        "title": "标普500",
        "core": ("159655", "513650"),
        "conditional": ("513500",),
    },
)
PICK_CODES = tuple(dict.fromkeys(c for g in PICK_GROUPS for c in (*g["core"], *g["conditional"])))
PICK_SPREAD = 2.0
PICK_MIN_TURNOVER = 50_000_000.0

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

# 上一份人工核对过的同类快照。网络和本地 CSV 都不可用时才用它。
BAKED_PEERS = [
    {"code": "159513", "name": "大成纳斯达克100", "close": None, "iopv": None, "ref_premium_pct": 8.72, "nav_premium_pct": None},
    {"code": "159632", "name": "华安纳斯达克100", "close": 2.434, "iopv": 2.234, "ref_premium_pct": 8.94, "nav_premium_pct": 11.99},
    {"code": "513390", "name": "博时纳斯达克100", "close": 2.420, "iopv": 2.207, "ref_premium_pct": 9.67, "nav_premium_pct": 12.44},
    {"code": "159660", "name": "汇添富纳斯达克100", "close": 2.358, "iopv": 2.149, "ref_premium_pct": 9.74, "nav_premium_pct": 12.48},
    {"code": "513300", "name": "华夏纳斯达克100", "close": 2.674, "iopv": 2.437, "ref_premium_pct": 9.74, "nav_premium_pct": 12.66},
    {"code": "159659", "name": "招商纳斯达克100", "close": 2.356, "iopv": 2.125, "ref_premium_pct": 10.85, "nav_premium_pct": 13.30},
    {"code": "159941", "name": "广发纳指100", "close": 1.649, "iopv": 1.471, "ref_premium_pct": 12.12, "nav_premium_pct": 14.92},
    {"code": "159696", "name": "易方达纳斯达克100", "close": 2.072, "iopv": 1.831, "ref_premium_pct": 13.14, "nav_premium_pct": 15.66},
    {"code": "513100", "name": "国泰纳斯达克100", "close": 2.239, "iopv": 1.966, "ref_premium_pct": 13.89, "nav_premium_pct": 16.46},
    {"code": "159501", "name": "嘉实纳斯达克100", "close": 2.158, "iopv": 1.847, "ref_premium_pct": 16.83, "nav_premium_pct": 18.29},
]


def now_cst() -> datetime:
    return datetime.now(CST)


def get(url: str, headers: dict | None = None, retries: int = 3, timeout: int = 40) -> bytes:
    h = dict(UA)
    if headers:
        h.update(headers)
    last: Exception | None = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.8 + i)
    raise RuntimeError(f"GET failed {url}: {last}")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def symbol_of(code: str) -> str:
    return ("sh" if code.startswith(("5", "6")) else "sz") + code


def short_name(name: str) -> str:
    name = re.sub(r"（QDII[^）]*）|\(QDII[^)]*\)", "", name or "")
    name = name.replace("ETF", "").replace("基金", "").strip()
    return name or "纳指100"


def drawdown_coef(dd: float | None, day_chg: float | None = None) -> tuple[float, str]:
    """距近一年最高净值的回撤（正数，单位百分点）。与 index.html 一致。

    <5% → 1.0。之后按锚点分段线性：
    5%→1.67，10%→3.33，15%→6.67，20%→8.33，25%→10.0，30%→11.67，40%→13.33。
    >40% → 13.33。例如 7.5%≈2.5，12.5%≈5.0。
    最新净值日相对前一净值日下跌达到 3%，在分段值上再 +0.5。没有 2 倍上限。
    """
    if dd is None:
        base, label = 1.0, "数据不足"
    elif dd < 5:
        base, label = 1.0, "距高点<5%"
    elif dd > 40:
        base, label = _DD_ANCHORS[-1][1], "距高点>40%"
    else:
        base, label = _DD_ANCHORS[-1][1], "距高点>40%"
        for (lo, lo_c), (hi, hi_c) in zip(_DD_ANCHORS, _DD_ANCHORS[1:]):
            if dd <= hi:
                t = (dd - lo) / (hi - lo)
                base = lo_c + t * (hi_c - lo_c)
                label = f"距高点{lo:g}–{hi:g}%"
                break
    if day_chg is not None and day_chg <= -3:
        return base + 0.5, label + " · 单日跌≥3% +0.5"
    return base, label


def spx_drawdown_coef(dd: float | None, day_chg: float | None = None) -> tuple[float, str]:
    """标普500回撤系数。与 index.html 的 spxDrawdownCoef 一致。

    <5% → 1（金额=基准 2000）。从 5% 起系数为回撤百分点÷2：
    5%→2.5（¥5000），10%→5（¥10000），15%→7.5，20%→10，25%→12.5，
    30%→15，35%→17.5，40%→20。中间线性。比 40% 更深仍按同一斜率，不封顶。
    前一净值日跌幅 ≥3%，再 +0.5。
    """
    if dd is None:
        base, label = 1.0, "数据不足"
    elif dd < 5:
        base, label = 1.0, "距高点<5%"
    else:
        base = dd / 2.0
        lo = int(dd // 5) * 5
        if abs(dd - lo) < 1e-9:
            label = f"距高点{lo:g}%"
        else:
            label = f"距高点{lo:g}–{lo + 5:g}%"
    if day_chg is not None and day_chg <= -3:
        return base + 0.5, label + " · 单日跌≥3% +0.5"
    return base, label


def _premium_relative(p: float, p60: float) -> float:
    """相对自己的 P60 连续变化。d = P−P60。

    d≤−5 → 1.15；d=0 → 1.00；d≥+5 → 0.85；两端之间线性。
    """
    d = p - p60
    if d <= -5:
        return 1.15
    if d <= 0:
        t = (d + 5) / 5.0
        return 1.15 + t * (1.0 - 1.15)
    if d <= 5:
        t = d / 5.0
        return 1.0 + t * (0.85 - 1.0)
    return 0.85


def premium_coef(
    p: float,
    p60: float,
    peer_min_p: float | None,
    peak_dd: float | None = None,
) -> tuple[float, str, bool]:
    """相对 P60 连续计算，再用绝对溢价、同类价差封顶，深回撤时抬底。

    买多少仍主要由回撤系数决定；这里只轻度修正（大约 0.85–1.15）。
    1. 相对 P60：≤−5 → 1.15，=0 → 1.00，≥+5 → 0.85，中间线性。
    2. 绝对软顶：P≥22 → ≤0.85；P≥18 → ≤0.90；P≥15 → ≤0.95。
    3. 比最便宜同类贵 ≥5 个点 → ≤0.90。
    4. 回撤下限：距高点 ≥15% → ≥0.95；≥10% → ≥0.90。
    """
    coef = _premium_relative(p, p60)
    if p >= 22:
        coef = min(coef, 0.85)
    elif p >= 18:
        coef = min(coef, 0.90)
    elif p >= 15:
        coef = min(coef, 0.95)
    if peer_min_p is not None and p - peer_min_p >= 5:
        coef = min(coef, 0.90)
    if peak_dd is not None:
        if peak_dd >= 15:
            coef = max(coef, 0.95)
        elif peak_dd >= 10:
            coef = max(coef, 0.90)
    expensive = (p >= 15) or (peer_min_p is not None and p - peer_min_p >= 5) or (p - p60 >= 5)
    # 新区间是 0.85–1.15（旧为 0.50–1.30）。标签按同一形状换算，
    # 不再因为系数落到已经取消的 0.50 地板就叫「明显贵」。
    # 相对便宜：c≥1.075，对应比 P60 低约 2.5 个点（旧阈值 c≥1.15 的同一位置）。
    # 偏贵：c≤0.97，对应比 P60 高约 1 个点（旧阈值 c≤0.90 的同一位置），或 P≥15。
    # 明显贵：系数贴在新的贵端锚点 0.85，且本身已判为偏贵情形。
    if expensive and coef <= 0.85 + 1e-9:
        label = "明显贵"
    elif p >= 15 or coef <= 0.97:
        label = "偏贵"
    elif coef >= 1.075:
        label = "相对便宜"
    else:
        label = "中性"
    return coef, label, expensive


def _self_check() -> None:
    expect = [
        (0, 1.0),
        (4.9, 1.0),
        (5, 1.67),
        (7.5, 2.5),
        (10, 3.33),
        (12.5, 5.0),
        (15, 6.67),
        (17.5, 7.5),
        (20, 8.33),
        (22.5, 9.165),
        (25, 10.0),
        (27.5, 10.835),
        (30, 11.67),
        (35, 12.5),
        (40, 13.33),
        (50, 13.33),
    ]
    for dd, coef in expect:
        got, _ = drawdown_coef(dd)
        if abs(got - coef) > 1e-9:
            raise SystemExit(f"回撤系数自检失败 dd={dd} got={got} expect={coef}")
    got, label = drawdown_coef(20, -3)
    if abs(got - 8.83) > 1e-9 or "单日跌" not in label:
        raise SystemExit(f"单日急跌加分自检失败 got={got} {label}")
    if abs(drawdown_coef(20, -2.99)[0] - 8.33) > 1e-9:
        raise SystemExit("未满 3% 的单日跌不应加分")
    # 基准 3000，溢价系数 1.0，单日涨跌 0。锚点金额取两位小数系数，接近整千。
    amount_expect = {
        5: 5010.0,
        10: 9990.0,
        15: 20010.0,
        20: 24990.0,
        40: 39990.0,
    }
    for dd, want in amount_expect.items():
        amount = DEFAULT_BASELINE * drawdown_coef(dd)[0] * 1
        if abs(amount - want) > 1e-6:
            raise SystemExit(f"阶梯金额自检失败 dd={dd} amount={amount} expect={want}")
    if amount_expect[40] <= DEFAULT_BASELINE * 2:
        raise SystemExit("取消 2 倍上限自检失败")
    if abs(premium_coef(10, 10, 9)[0] - 1.0) > 1e-9:
        raise SystemExit("溢价中性自检失败")
    coef, label, expensive = premium_coef(5, 10, 4)
    if abs(coef - 1.15) > 1e-9 or label != "相对便宜" or expensive:
        raise SystemExit(f"溢价偏便宜自检失败 {coef} {label} {expensive}")
    coef, label, expensive = premium_coef(16, 16, 15.5)
    if abs(coef - 0.95) > 1e-9 or label != "偏贵" or not expensive:
        raise SystemExit(f"绝对溢价 15% 软顶自检失败 {coef} {label}")
    coef, label, expensive = premium_coef(20.24, 20.24, 12.92)
    if abs(coef - 0.90) > 1e-9 or label != "偏贵" or not expensive:
        raise SystemExit(f"绝对溢价 18% 软顶自检失败 {coef} {label}")
    coef, label, expensive = premium_coef(22, 22, 21)
    if abs(coef - 0.85) > 1e-9 or label != "明显贵" or not expensive:
        raise SystemExit(f"绝对溢价 22% 软顶自检失败 {coef} {label}")
    coef, label, expensive = premium_coef(12, 12, 6)
    if abs(coef - 0.90) > 1e-9 or label != "偏贵" or not expensive:
        raise SystemExit(f"同类价差封顶自检失败 {coef} {label}")
    if abs(_premium_relative(10, 15) - 1.15) > 1e-9:
        raise SystemExit("相对 P60 ≤−5 自检失败")
    if abs(_premium_relative(10, 10) - 1.0) > 1e-9:
        raise SystemExit("相对 P60 =0 自检失败")
    if abs(_premium_relative(15, 10) - 0.85) > 1e-9:
        raise SystemExit("相对 P60 ≥+5 自检失败")
    if abs(_premium_relative(18, 15) - 0.91) > 1e-9:
        raise SystemExit(f"相对 P60 线性自检失败 {_premium_relative(18, 15)}")
    # 基准 3000，peakDd=10，dayChg=0，P=18，P60=15，peerMin=12。
    # 相对 ~0.91 → 绝对软顶 0.90 → 同类 0.90 → 回撤下限 0.90。
    # 回撤系数 3.33。金额 = 3000 × 3.33 × 0.90 = 8991。
    coef, label, expensive = premium_coef(18, 15, 12, 10)
    sample_dd, _sample_label = drawdown_coef(10, 0)
    sample_amount = DEFAULT_BASELINE * sample_dd * coef
    if (
        abs(coef - 0.90) > 1e-9
        or abs(sample_dd - 3.33) > 1e-9
        or abs(sample_amount - 8991) > 1e-6
        or label != "偏贵"
        or not expensive
    ):
        raise SystemExit(
            f"溢价新政策样例自检失败 coef={coef} dd={sample_dd} amount={sample_amount} {label}"
        )
    coef, label, expensive = premium_coef(22, 17, 16, 15)
    if abs(coef - 0.95) > 1e-9 or label != "偏贵" or not expensive:
        raise SystemExit(f"回撤 15% 下限自检失败 {coef} {label}")
    coef, label, _expensive = premium_coef(22, 17, 16, 10)
    if abs(coef - 0.90) > 1e-9 or label != "偏贵":
        raise SystemExit(f"回撤 10% 下限自检失败 {coef} {label}")
    coef, label, _expensive = premium_coef(22, 17, 16, 9)
    if abs(coef - 0.85) > 1e-9 or label != "明显贵":
        raise SystemExit(f"未触发回撤下限时应明显贵 {coef} {label}")
    coef, label, _expensive = premium_coef(5, 10, 4, 20)
    if abs(coef - 1.15) > 1e-9 or label != "相对便宜":
        raise SystemExit(f"回撤下限不应压低便宜溢价 {coef} {label}")
    nasdaq_today = choose_today(
        [
            {"code": "159513", "ref_premium_pct": 12.92, "fee_total_pct": 1.0, "aum_cny": 82e8, "turnover_cny": 2.7e8},
            {"code": "513110", "ref_premium_pct": 12.93, "fee_total_pct": 1.0, "aum_cny": 52e8, "turnover_cny": 3.3e8},
            {"code": "159632", "ref_premium_pct": 13.24, "fee_total_pct": 0.8, "aum_cny": 118e8, "turnover_cny": 4e8},
            {"code": "159501", "ref_premium_pct": 20.24, "fee_total_pct": 0.6, "aum_cny": 134e8, "turnover_cny": 1.5e8},
        ]
    )
    if not nasdaq_today or nasdaq_today["code"] != "159632":
        raise SystemExit(f"纳指短名单自检失败: {nasdaq_today}")
    sp_today = choose_today(
        [
            {"code": "159655", "ref_premium_pct": 7.91, "fee_total_pct": 0.75, "aum_cny": 42e8, "turnover_cny": 2e8},
            {"code": "513650", "ref_premium_pct": 8.16, "fee_total_pct": 0.75, "aum_cny": 87e8, "turnover_cny": 5e8},
            {"code": "513500", "ref_premium_pct": 10.28, "fee_total_pct": 0.8, "aum_cny": 252e8, "turnover_cny": 3e8},
        ]
    )
    if not sp_today or sp_today["code"] != "513650":
        raise SystemExit(f"标普短名单自检失败: {sp_today}")
    spx_expect = [
        (0, 1.0),
        (4.9, 1.0),
        (5, 2.5),
        (7.5, 3.75),
        (10, 5.0),
        (15, 7.5),
        (20, 10.0),
        (25, 12.5),
        (30, 15.0),
        (35, 17.5),
        (40, 20.0),
        (45, 22.5),
        (50, 25.0),
    ]
    for dd, coef in spx_expect:
        got, _label = spx_drawdown_coef(dd)
        if abs(got - coef) > 1e-9:
            raise SystemExit(f"标普回撤系数自检失败 dd={dd} got={got} expect={coef}")
    spx_amounts = {5: 5000.0, 10: 10000.0, 15: 15000.0, 20: 20000.0, 40: 40000.0, 45: 45000.0}
    for dd, want in spx_amounts.items():
        amount = SPX_BASELINE * spx_drawdown_coef(dd)[0]
        if abs(amount - want) > 1e-6:
            raise SystemExit(f"标普阶梯金额自检失败 dd={dd} amount={amount} expect={want}")
    got, label = spx_drawdown_coef(10, -3)
    if abs(got - 5.5) > 1e-9 or "单日跌" not in label:
        raise SystemExit(f"标普单日急跌加分自检失败 got={got} {label}")
    if abs(spx_drawdown_coef(10, -2.99)[0] - 5.0) > 1e-9:
        raise SystemExit("标普未满 3% 的单日跌不应加分")
    sample = trailing_drawdown_pct(
        [
            {"date": "2025-01-01", "close": 100},
            {"date": "2025-06-01", "close": 120},
            {"date": "2026-01-01", "close": 90},
        ]
    )
    if abs(sample["2025-01-01"]) > 1e-9 or abs(sample["2025-06-01"]) > 1e-9:
        raise SystemExit(f"滚动高点当日应为 0: {sample}")
    if abs(sample["2026-01-01"] - (-25.0)) > 1e-9:
        raise SystemExit(f"365 天滚动回撤自检失败: {sample}")
    sample_fields = [""] * 83
    sample_fields[1] = "纳指ETF嘉实"
    sample_fields[3] = "2.119"
    sample_fields[4] = "2.144"
    sample_fields[30] = "20261103111133"
    sample_fields[77] = "20.49"
    sample_fields[78] = "1.7586"
    sample_fields[81] = "1.7831"
    parsed = parse_tencent_fields(sample_fields)
    if not parsed or abs(parsed["price"] - 2.119) > 1e-9 or abs(parsed["premium"] - (2.119 / 1.7586 - 1) * 100) > 0.02:
        raise SystemExit(f"腾讯行情解析自检失败: {parsed}")
    if parsed["date"] != "20261103" or abs((parsed["iopv"] or 0) - 1.7586) > 1e-9:
        raise SystemExit(f"腾讯行情时间或 IOPV 自检失败: {parsed}")
    sample_fields[78] = "0"
    odd = parse_tencent_fields(sample_fields)
    if not odd or abs(odd["premium"] - 20.49) > 1e-6:
        raise SystemExit(f"IOPV 异常时应改用溢价字段: {odd}")
    published = iopv_drawdown_pct(1.9591, 1.7831)
    live_dd = iopv_drawdown_pct(1.9591, 1.7586)
    if published is None or live_dd is None or not (live_dd > published + 1.0):
        raise SystemExit(f"IOPV 回撤应高于已公布净值回撤: {published} {live_dd}")


def _official_bars(bars: list[dict]) -> list[dict]:
    """交易未收盘时，最后一根日 K 是盘中价，不当作收盘价。"""
    if not bars:
        return bars
    now = now_cst()
    today = now.strftime("%Y-%m-%d")
    before_close = now.hour < 15 or (now.hour == 15 and now.minute < 5)
    if bars[-1]["day"] == today and before_close and len(bars) > 1:
        return bars[:-1]
    return bars


def fetch_nav(code: str = PRIMARY) -> list[dict]:
    """优先一次拿完全部净值；失败再走东财分页接口。"""
    try:
        raw = get(
            f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
            {"Referer": f"https://fund.eastmoney.com/{code}.html"},
            timeout=30,
        ).decode("utf-8", "replace")
        m = re.search(r"var Data_netWorthTrend = (\[.*?\]);", raw)
        if not m:
            raise RuntimeError("pingzhongdata 里没有净值序列")
        trend = json.loads(m.group(1))
        rows = []
        for item in trend:
            y = item.get("y")
            x = item.get("x")
            if y is None or x is None:
                continue
            day = datetime.fromtimestamp(x / 1000, CST).strftime("%Y-%m-%d")
            rows.append({"date": day, "nav": float(y), "chg_pct": item.get("equityReturn")})
        if len(rows) < 30:
            raise RuntimeError(f"净值条数过少: {len(rows)}")
        rows.sort(key=lambda r: r["date"])
        print(f"NAV {code} pingzhongdata: {len(rows)} {rows[0]['date']} -> {rows[-1]['date']}")
        return rows
    except Exception as e:  # noqa: BLE001
        print("pingzhongdata 失败，改分页接口:", e)

    referer = {"Referer": f"https://fundf10.eastmoney.com/jjjz_{code}.html"}
    raw = get(
        f"https://api.fund.eastmoney.com/f10/lsjz?callback=&fundCode={code}"
        f"&pageIndex=1&pageSize=20&startDate=&endDate=",
        referer,
    )
    d0 = json.loads(raw.decode())
    total = int(d0.get("TotalCount") or 0)
    pages = max(1, (total + 19) // 20)
    packed: list[dict] = []
    for p in range(1, pages + 1):
        d = json.loads(
            get(
                f"https://api.fund.eastmoney.com/f10/lsjz?callback=&fundCode={code}"
                f"&pageIndex={p}&pageSize=20&startDate=&endDate=",
                referer,
            ).decode()
        )
        packed.extend((d.get("Data") or {}).get("LSJZList") or [])
        time.sleep(0.08)
    rows = []
    for r in packed:
        if not r.get("DWJZ"):
            continue
        chg = r.get("JZZZL")
        rows.append(
            {
                "date": r["FSRQ"],
                "nav": float(r["DWJZ"]),
                "chg_pct": float(chg) if chg not in (None, "") else None,
            }
        )
    rows.sort(key=lambda r: r["date"])
    print(f"NAV {code} lsjz: {len(rows)} {rows[0]['date']} -> {rows[-1]['date']}")
    return rows


def _load_nav_csv() -> list[dict]:
    path = DATA / f"nav_{PRIMARY}.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("date") or not r.get("nav"):
                continue
            chg = r.get("chg_pct") or ""
            rows.append(
                {
                    "date": r["date"],
                    "nav": float(r["nav"]),
                    "chg_pct": float(chg) if chg else None,
                }
            )
    rows.sort(key=lambda r: r["date"])
    return rows


def _save_nav_csv(rows: list[dict]) -> None:
    path = DATA / f"nav_{PRIMARY}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "nav", "chg_pct"])
        for r in rows:
            w.writerow([r["date"], r["nav"], "" if r.get("chg_pct") is None else r["chg_pct"]])


def fetch_sina_kline(symbol: str = "sz159501", datalen: int = 1023) -> list[dict]:
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={symbol}&scale=240&ma=no&datalen={datalen}"
    )
    data = json.loads(get(url).decode())
    bars = [{"day": c["day"], "close": float(c["close"])} for c in data if c.get("close")]
    bars.sort(key=lambda r: r["day"])
    print(f"Sina kline: {len(bars)} {bars[0]['day']} -> {bars[-1]['day']}")
    return bars


def fetch_tencent_kline(code: str = PRIMARY) -> list[dict]:
    symbol = symbol_of(code)
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,2000,"
    payload = json.loads(get(url, timeout=30).decode())
    block = (payload.get("data") or {}).get(symbol) or {}
    raw_rows = block.get("day") or block.get("qfqday") or []
    bars = []
    for row in raw_rows:
        # [date, open, close, high, low, volume]
        if len(row) < 3:
            continue
        bars.append({"day": row[0], "close": float(row[2])})
    bars.sort(key=lambda r: r["day"])
    if len(bars) < 30:
        raise RuntimeError(f"腾讯 K 线过短: {len(bars)}")
    print(f"Tencent kline: {len(bars)} {bars[0]['day']} -> {bars[-1]['day']}")
    return bars


def fetch_closes() -> list[dict]:
    errors = []
    for fn in (fetch_sina_kline, fetch_tencent_kline):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
            print("K 线失败:", fn.__name__, e)
    raise RuntimeError(" ; ".join(errors))


def _load_premium_csv() -> list[dict]:
    path = DATA / "premium_159501.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("date"):
                continue
            rows.append(
                {
                    "date": r["date"],
                    "close": float(r["close"]),
                    "nav": float(r["nav"]),
                    "premium_pct": float(r["premium_pct"]),
                }
            )
    rows.sort(key=lambda r: r["date"])
    return rows


def _brace_object(text: str, open_at: int) -> str | None:
    depth = 0
    for i in range(open_at, len(text)):
        ch = text[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[open_at : i + 1]
    return None


def _fee_pair(fees: list) -> tuple[float | None, float | None]:
    mgmt = custody = None
    for fee in fees or []:
        kind = str(fee.get("fee_type") or "")
        rate = fee.get("rate_pct")
        if rate is None:
            continue
        if kind == "management":
            mgmt = float(rate)
        elif kind == "custody":
            custody = float(rate)
    return mgmt, custody


def parse_qdiilens(html: str) -> tuple[list[dict], str | None, dict]:
    """纳指同类表，外加纳指/标普短名单用的费率、规模、成交和全市场溢价。"""
    text = html
    if '\\"code\\"' in html or '\\"quote\\"' in html:
        text = html.replace('\\"', '"').replace("\\/", "/")

    prem_re = re.compile(
        r'\{"code":"(\d{6})","name":"([^"]+)","index":"([^"]+)",'
        r'"trading_date":"(\d{4}-\d{2}-\d{2})","prev_trading_date":"[^"]+",'
        r'"premium_pct":([-0-9.]+)'
    )
    by_code: dict[str, dict] = {}
    index_premiums: dict[str, list[float]] = {"nasdaq-100": [], "sp-500": []}
    for code, name, index, tdate, prem in prem_re.findall(text):
        premium = float(prem)
        if index in index_premiums:
            index_premiums[index].append(premium)
        if index != "nasdaq-100":
            continue
        by_code[code] = {
            "code": code,
            "name": short_name(name),
            "ref_premium_pct": round(float(prem), 4),
            "trading_date": tdate,
            "close": None,
            "iopv": None,
            "nav_premium_pct": None,
        }

    start = 0
    marker = '"quote":{'
    while True:
        i = text.find(marker, start)
        if i < 0:
            break
        blob = _brace_object(text, i + len(marker) - 1)
        start = i + len(marker)
        if not blob:
            continue
        try:
            q = json.loads(blob)
        except json.JSONDecodeError:
            continue
        code = str(q.get("code") or "")
        row = by_code.get(code)
        if row is None:
            continue
        if q.get("market_price") is not None:
            row["close"] = float(q["market_price"])
        if q.get("iopv") is not None:
            row["iopv"] = float(q["iopv"])
        if q.get("indicative_premium_pct") is not None:
            row["ref_premium_pct"] = round(float(q["indicative_premium_pct"]), 4)
        if q.get("official_nav_premium_pct") is not None:
            row["nav_premium_pct"] = round(float(q["official_nav_premium_pct"]), 4)
        if q.get("trading_date"):
            row["trading_date"] = q["trading_date"]
        if q.get("name"):
            row["name"] = short_name(str(q["name"]))

    funds: dict[str, dict] = {}
    start = 0
    while True:
        i = text.find('"aum_cny":', start)
        if i < 0:
            break
        j = text.rfind('{"code":"', 0, i)
        start = i + 10
        if j < 0:
            continue
        blob = _brace_object(text, j)
        if not blob:
            continue
        try:
            fund = json.loads(blob)
        except json.JSONDecodeError:
            continue
        code = str(fund.get("code") or "")
        if code not in PICK_CODES:
            continue
        mgmt, custody = _fee_pair(fund.get("fees") or [])
        total = None
        if mgmt is not None or custody is not None:
            total = round((mgmt or 0) + (custody or 0), 4)
        funds[code] = {
            "code": code,
            "name": short_name(str(fund.get("name") or code)),
            "index": fund.get("index"),
            "fee_mgmt_pct": mgmt,
            "fee_custody_pct": custody,
            "fee_total_pct": total,
            "fee_note": "管理费+托管费，年费率",
            "aum_cny": fund.get("aum_cny"),
            "aum_as_of": fund.get("aum_as_of"),
        }

    # 第二遍行情：短名单需要成交额，纳指全表不展示它。
    start = 0
    while True:
        i = text.find(marker, start)
        if i < 0:
            break
        blob = _brace_object(text, i + len(marker) - 1)
        start = i + len(marker)
        if not blob:
            continue
        try:
            q = json.loads(blob)
        except json.JSONDecodeError:
            continue
        code = str(q.get("code") or "")
        if code not in funds and code not in PICK_CODES:
            continue
        slot = funds.setdefault(
            code,
            {"code": code, "name": short_name(str(q.get("name") or code))},
        )
        if q.get("indicative_premium_pct") is not None:
            slot["ref_premium_pct"] = round(float(q["indicative_premium_pct"]), 4)
        if q.get("turnover_cny") is not None:
            slot["turnover_cny"] = float(q["turnover_cny"])
        if q.get("trading_date"):
            slot["trading_date"] = q["trading_date"]
        if q.get("name"):
            slot["name"] = short_name(str(q["name"]))

    peers = [by_code[c] for c in sorted(by_code)]
    ref_date = None
    primary = by_code.get(PRIMARY)
    if primary:
        ref_date = primary.get("trading_date")
    elif peers:
        ref_date = peers[0].get("trading_date")
    catalog = {"premiums": index_premiums, "funds": funds, "ref_date": ref_date}
    return peers, ref_date, catalog


def fetch_tracking_error(code: str) -> dict | None:
    url = f"https://fundf10.eastmoney.com/tsdata_{code}.html"
    html = get(url, {"Referer": url}, retries=2, timeout=25).decode("utf-8", "replace")
    m = re.search(
        r"年化跟踪误差</th>.*?<tr>\s*<td[^>]*>([^<]*)</td>\s*<td[^>]*>([\d.]+)%</td>",
        html,
        re.S,
    )
    if not m:
        return None
    tail = html[m.end() : m.end() + 400]
    d = re.search(r"截止至：(\d{4}-\d{2}-\d{2})", tail) or re.search(r"截止至：(\d{4}-\d{2}-\d{2})", html)
    return {
        "index_name": m.group(1).strip(),
        "tracking_error_pct": float(m.group(2)),
        "asof": d.group(1) if d else None,
    }


def fetch_tracking_errors(codes: tuple[str, ...] = PICK_CODES) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(fetch_tracking_error, code): code for code in codes}
        for fut in futs:
            code = futs[fut]
            try:
                row = fut.result()
            except Exception as e:  # noqa: BLE001
                print("跟踪误差失败", code, e)
                continue
            if row:
                out[code] = row
    print("跟踪误差", ",".join(f"{k}:{v['tracking_error_pct']}" for k, v in out.items()) or "无")
    return out


def _load_saved_picks() -> dict | None:
    path = DATA / "dashboard_payload.json"
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    picks = payload.get("picks")
    return picks if isinstance(picks, dict) and picks.get("groups") else None


def choose_today(items: list[dict]) -> dict | None:
    """价差约 2 个点以内时，在成交够用的里面偏向更低费率、再偏向更大规模。"""
    usable = []
    for item in items:
        if item.get("ref_premium_pct") is None:
            continue
        turn = item.get("turnover_cny")
        if turn is not None and turn < PICK_MIN_TURNOVER:
            continue
        usable.append(item)
    if not usable:
        return None
    floor = min(item["ref_premium_pct"] for item in usable)
    band = [item for item in usable if item["ref_premium_pct"] <= floor + PICK_SPREAD]

    def sort_key(item: dict) -> tuple:
        fee = item.get("fee_total_pct")
        return (
            999.0 if fee is None else float(fee),
            -(item.get("aum_cny") or 0),
            item["ref_premium_pct"],
        )

    return min(band, key=sort_key)


def _yi(amount: float | None) -> str:
    if amount is None:
        return ""
    return f"{amount / 1e8:.1f} 亿"


def _why(item: dict, today: dict, floor: float, median: float | None, anchor: str) -> str:
    gap = item["ref_premium_pct"] - floor
    fee = item.get("fee_total_pct")
    if item["code"] == today["code"]:
        parts = []
        if gap <= 0.05:
            parts.append("短名单里参考溢价最低")
        else:
            parts.append(f"与最低溢价只差 {gap:.2f} 个点")
        turn = item.get("turnover_cny")
        parts.append(f"成交约 {_yi(turn)}" if turn else "成交够用")
        if fee is not None:
            parts.append(f"管理+托管费 {fee:.2f}%")
        return "今日优先：" + "，".join(parts) + "。"
    if item["code"] == anchor and median is not None and item["ref_premium_pct"] > median + 1:
        return (
            f"溢价 {item['ref_premium_pct']:.2f}%，高于同类中枢约 {median:.2f}%。"
            "等溢价回到中枢附近再当主买点；今天先看更便宜的同类。"
        )
    if gap <= 0.05:
        return (
            f"溢价和短名单最低一档几乎一样（差 {gap:.2f} 个点），成交够用。"
            "价差在约 2 个点内，今天让给了费率更低或规模更大的一只。"
        )
    if gap <= PICK_SPREAD:
        return (
            f"溢价只高 {gap:.2f} 个点，成交够用。"
            "价差在约 1–2 个点内可改看费率或规模；今天已偏向更合适的一只。"
        )
    return (
        f"溢价比短名单低位高 {gap:.2f} 个点。"
        "价差拉开时先买更便宜的；回到约 2 个点内再比费率和规模。"
    )


def build_picks(catalog: dict, tracking: dict[str, dict]) -> dict:
    funds: dict[str, dict] = catalog.get("funds") or {}
    premiums: dict[str, list[float]] = catalog.get("premiums") or {}
    groups = []
    for spec in PICK_GROUPS:
        items = []
        for code in (*spec["core"], *spec["conditional"]):
            src = funds.get(code)
            if not src or src.get("ref_premium_pct") is None:
                continue
            te = tracking.get(code) or {}
            items.append(
                {
                    "code": code,
                    "name": src.get("name") or code,
                    "role": "conditional" if code in spec["conditional"] else "core",
                    "ref_premium_pct": src["ref_premium_pct"],
                    "fee_mgmt_pct": src.get("fee_mgmt_pct"),
                    "fee_custody_pct": src.get("fee_custody_pct"),
                    "fee_total_pct": src.get("fee_total_pct"),
                    "aum_cny": src.get("aum_cny"),
                    "aum_as_of": src.get("aum_as_of"),
                    "turnover_cny": src.get("turnover_cny"),
                    "trading_date": src.get("trading_date"),
                    "tracking_error_pct": te.get("tracking_error_pct"),
                    "tracking_asof": te.get("asof"),
                }
            )
        today = choose_today(items)
        series = premiums.get(spec["id"]) or [it["ref_premium_pct"] for it in items]
        median = round(statistics.median(series), 4) if series else None
        floor = min(it["ref_premium_pct"] for it in items) if items else None
        anchor = spec["conditional"][0]
        for item in items:
            item["is_today"] = bool(today and item["code"] == today["code"])
            item["why"] = _why(item, today or item, floor if floor is not None else item["ref_premium_pct"], median, anchor)
        items.sort(key=lambda it: (not it["is_today"], it["ref_premium_pct"]))
        today_line = ""
        if today:
            today_line = f"今日优先 {today['code']} {today.get('name') or ''} · 参考溢价 {today['ref_premium_pct']:.2f}%"
        groups.append(
            {
                "id": spec["id"],
                "title": spec["title"],
                "median_premium_pct": median,
                "today_code": today["code"] if today else None,
                "today_line": today_line.strip(),
                "items": items,
            }
        )
    dates = [it.get("trading_date") for g in groups for it in g["items"] if it.get("trading_date")]
    te_dates = [it.get("tracking_asof") for g in groups for it in g["items"] if it.get("tracking_asof")]
    aum_dates = [it.get("aum_as_of") for g in groups for it in g["items"] if it.get("aum_as_of")]
    return {
        "premium_date": max(dates) if dates else catalog.get("ref_date"),
        "tracking_asof": max(te_dates) if te_dates else None,
        "aum_as_of": max(aum_dates) if aum_dates else None,
        "spread_pct": PICK_SPREAD,
        "rule": "成交够用的前提下先看更低溢价；价差在约 2 个点内，再偏向更低的管理+托管费，然后是更大规模。",
        "groups": groups,
    }


def fetch_qdiilens_bundle() -> tuple[list[dict], str | None, dict]:
    html = get("https://qdiilens.com/", timeout=40).decode("utf-8", "replace")
    peers, ref_date, catalog = parse_qdiilens(html)
    if not any(p["code"] == PRIMARY for p in peers) or len(peers) < 5:
        raise RuntimeError(f"QDII Lens 解析到的纳指 ETF 不足: {len(peers)}")
    try:
        tracking = fetch_tracking_errors()
    except Exception as e:  # noqa: BLE001
        print("跟踪误差批次失败:", e)
        tracking = {}
    picks = build_picks(catalog, tracking)
    print(
        "QDII Lens peers:",
        len(peers),
        "ref_date=",
        ref_date,
        "picks=",
        [(g["id"], g["today_code"]) for g in picks["groups"]],
    )
    return peers, ref_date, picks


def _load_peers_csv() -> list[dict]:
    path = DATA / "peers_latest.csv"
    if not path.exists():
        return []
    rows = []
    with path.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if not r.get("code"):
                continue

            def num(key: str) -> float | None:
                v = (r.get(key) or "").strip()
                return float(v) if v else None

            rows.append(
                {
                    "code": r["code"],
                    "name": r.get("name") or r["code"],
                    "close": num("close"),
                    "iopv": num("iopv"),
                    "ref_premium_pct": num("ref_premium_pct"),
                    "nav_premium_pct": num("nav_premium_pct"),
                }
            )
    return rows


def fetch_sina_spot(codes: list[str]) -> dict[str, dict]:
    parts = [symbol_of(c) for c in codes]
    url = "https://hq.sinajs.cn/list=" + ",".join(parts)
    text = get(url, {"Referer": "https://finance.sina.com.cn"}).decode("gbk", "replace")
    spot: dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r'var hq_str_(sz|sh)(\d+)="(.*)"', line)
        if not m:
            continue
        code = m.group(2)
        fields = m.group(3).split(",")
        if len(fields) < 4 or not fields[3]:
            continue
        spot[code] = {
            "name": fields[0],
            "price": float(fields[3]),
            "date": fields[30] if len(fields) > 30 else "",
            "time": fields[31] if len(fields) > 31 else "",
        }
    if len(spot) < 3:
        raise RuntimeError(f"新浪行情只返回 {list(spot)}")
    print("Sina spot:", ",".join(sorted(spot)))
    return spot


def _field_float(fields: list[str], idx: int) -> float | None:
    if idx >= len(fields):
        return None
    raw = (fields[idx] or "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse_tencent_fields(fields: list[str]) -> dict | None:
    """腾讯行情：3=现价，4=昨收，30=时间戳，77=溢价%，78=IOPV，81=净值。

    IOPV 缺失或和现价明显不成比例时，用第 77 字段的溢价。
    """
    price = _field_float(fields, 3)
    if price is None or price <= 0:
        return None
    stamp = fields[30] if len(fields) > 30 else ""
    iopv = _field_float(fields, 78)
    prem_field = _field_float(fields, 77)
    nav = _field_float(fields, 81)
    iopv_odd = iopv is None or iopv <= 0 or iopv > price * 1.5 or iopv < price * 0.4
    premium = None
    if not iopv_odd and iopv:
        premium = (price / iopv - 1.0) * 100.0
    elif prem_field is not None:
        premium = prem_field
        if iopv_odd:
            iopv = price / (1.0 + prem_field / 100.0) if prem_field > -90 else None
    if premium is None:
        return None
    return {
        "name": fields[1] if len(fields) > 1 else "",
        "price": price,
        "prev_close": _field_float(fields, 4),
        "date": stamp[:8],
        "time": stamp[8:14],
        "iopv": None if iopv is None else round(iopv, 4),
        "premium": round(premium, 4),
        "nav": None if nav is None else round(nav, 4),
    }


def fetch_tencent_spot(codes: list[str]) -> dict[str, dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(symbol_of(c) for c in codes)
    text = get(url).decode("gbk", "replace")
    spot: dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r'v_(sz|sh)(\d+)="(.*)"', line.strip().rstrip(";"))
        if not m:
            continue
        parsed = parse_tencent_fields(m.group(3).split("~"))
        if not parsed:
            continue
        spot[m.group(2)] = parsed
    if len(spot) < 3:
        raise RuntimeError("腾讯行情不足")
    print("Tencent spot:", ",".join(sorted(spot)))
    return spot


def fetch_spot(codes: list[str]) -> dict[str, dict]:
    """先腾讯，盘中能拿到现价、IOPV 和溢价；失败再退回新浪现价。"""
    for fn in (fetch_tencent_spot, fetch_sina_spot):
        try:
            return fn(codes)
        except Exception as e:  # noqa: BLE001
            print("现价失败:", fn.__name__, e)
    return {}


def intraday_quote_active(spot: dict[str, dict], now: datetime | None = None) -> bool:
    """工作日 9:30–15:05，且至少一只行情时间戳是今天，才把盘中价写进快照。"""
    now = now or now_cst()
    if now.weekday() >= 5:
        return False
    mins = now.hour * 60 + now.minute
    if mins < 9 * 60 + 30 or mins > 15 * 60 + 5:
        return False
    today = now.strftime("%Y%m%d")
    return any((row.get("date") or "") == today and row.get("premium") is not None for row in spot.values())


def fetch_pingzhong_nav(code: str) -> list[dict]:
    """东财一次拉完全部净值。给同类计算器用，不再走慢的分页接口。"""
    raw = get(
        f"https://fund.eastmoney.com/pingzhongdata/{code}.js",
        {"Referer": f"https://fund.eastmoney.com/{code}.html"},
        retries=2,
        timeout=30,
    ).decode("utf-8", "replace")
    m = re.search(r"var Data_netWorthTrend = (\[.*?\]);", raw)
    if not m:
        raise RuntimeError("没有净值序列")
    rows = []
    for item in json.loads(m.group(1)):
        y, x = item.get("y"), item.get("x")
        if y is None or x is None:
            continue
        rows.append(
            {
                "date": datetime.fromtimestamp(x / 1000, CST).strftime("%Y-%m-%d"),
                "nav": float(y),
            }
        )
    rows.sort(key=lambda r: r["date"])
    if len(rows) < 40:
        raise RuntimeError(f"净值过短 {len(rows)}")
    return rows


def etf_calc_stats(code: str) -> dict:
    """一只 ETF 自己的近一年高点回撤、单日涨跌、较上月净值、P60。"""
    nav_rows = fetch_pingzhong_nav(code)
    closes = _official_bars(fetch_tencent_kline(code))
    premium_rows = align_premium(nav_rows, closes)
    if len(premium_rows) < 30:
        raise RuntimeError(f"溢价样本不足 {len(premium_rows)}")
    stats = nav_metrics(nav_rows)
    stats["p60"] = round(statistics.median([r["premium_pct"] for r in premium_rows[-60:]]), 4)
    return stats


def fetch_peer_calc_stats(codes: list[str]) -> dict[str, dict]:
    def one(code: str) -> tuple[str, dict | None]:
        try:
            stats = etf_calc_stats(code)
            print(
                f"计算器 {code}: 距高点 {stats['peak_dd_pct']}%（{stats['peak_date']}） "
                f"单日 {stats['day_chg_pct']}% 较上月 {stats['mom_pct']} P60 {stats['p60']} 净值日 {stats['nav_date']}"
            )
            return code, stats
        except Exception as e:  # noqa: BLE001
            print("计算器字段失败", code, e)
            return code, None

    out: dict[str, dict] = {}
    if not codes:
        return out
    with ThreadPoolExecutor(max_workers=6) as pool:
        for code, stats in pool.map(one, codes):
            if stats and stats.get("peak_dd_pct") is not None and stats.get("p60") is not None:
                out[code] = stats
    return out


def mom_nav_change(nav_sorted: list[dict], latest_date: str, latest_nav: float):
    """较上月净值：最新净值相对约一个月前同一天。不是相对近期高点回撤。"""
    ld = datetime.strptime(latest_date, "%Y-%m-%d")
    y, m = ld.year, ld.month - 1
    if m == 0:
        y, m = y - 1, 12
    day = min(ld.day, monthrange(y, m)[1])
    target = datetime(y, m, day).strftime("%Y-%m-%d")
    prev_nav = prev_date = None
    for r in reversed(nav_sorted):
        if r["date"] <= target:
            prev_nav, prev_date = r["nav"], r["date"]
            break
    if prev_nav is None:
        return None, None
    return (latest_nav / prev_nav - 1.0) * 100.0, prev_date


def nav_metrics(nav_rows: list[dict]) -> dict:
    """近一年高点回撤、前一净值日涨跌、较上月净值。较上月只作参照。"""
    latest = nav_rows[-1]
    latest_date = latest["date"]
    latest_nav = float(latest["nav"])
    end = datetime.strptime(latest_date, "%Y-%m-%d")
    start = (end - timedelta(days=365)).strftime("%Y-%m-%d")
    window = [r for r in nav_rows if start <= r["date"] <= latest_date] or nav_rows[-252:]
    peak = max(window, key=lambda r: float(r["nav"]))
    peak_nav = float(peak["nav"])
    peak_dd = 0.0 if peak_nav <= 0 else max(0.0, (peak_nav - latest_nav) / peak_nav * 100.0)
    mom_pct, mom_from = mom_nav_change(nav_rows, latest_date, latest_nav)
    day_chg = None
    day_from = None
    if len(nav_rows) >= 2 and float(nav_rows[-2]["nav"]) > 0:
        day_from = nav_rows[-2]["date"]
        day_chg = (latest_nav / float(nav_rows[-2]["nav"]) - 1.0) * 100.0
    last60 = nav_rows[-60:]
    nav_60_high = max(float(r["nav"]) for r in last60)
    return {
        "nav": round(latest_nav, 4),
        "nav_date": latest_date,
        "peak_dd_pct": round(peak_dd, 4),
        "peak_nav": round(peak_nav, 4),
        "peak_date": peak["date"],
        "peak_window_start": window[0]["date"],
        "day_chg_pct": round(day_chg, 4) if day_chg is not None else None,
        "day_from_date": day_from,
        "mom_pct": round(mom_pct, 4) if mom_pct is not None else None,
        "mom_from_date": mom_from,
        "mom_to_date": latest_date,
        "is_nav_60_high": latest_nav >= nav_60_high - 1e-12,
    }


def align_premium(nav_rows: list[dict], closes: list[dict]) -> list[dict]:
    nav_map = {r["date"]: r["nav"] for r in nav_rows}
    close_map = {c["day"]: c["close"] for c in closes}
    rows = []
    for d in sorted(set(close_map) & set(nav_map)):
        c, n = close_map[d], nav_map[d]
        if n <= 0:
            continue
        rows.append(
            {
                "date": d,
                "close": round(c, 4),
                "nav": round(n, 4),
                "premium_pct": round((c / n - 1.0) * 100.0, 4),
            }
        )
    return rows


def merge_history(fresh: list[dict], old: list[dict]) -> tuple[list[dict], bool]:
    if not old or not fresh:
        return fresh or old, False
    if len(fresh) + 5 >= len(old):
        return fresh, False
    merged = {r["date"]: r for r in old}
    merged.update({r["date"]: r for r in fresh})
    rows = [merged[d] for d in sorted(merged)]
    return rows, True


def _calculator_etfs(peers_sorted: list[dict], stats: dict[str, dict], primary_calc: dict) -> list[dict]:
    """159501 用主流程数字，保证默认金额不变；其余纳指用各自的净值与溢价。"""
    premiums = {
        p["code"]: p["ref_premium_pct"]
        for p in peers_sorted
        if p.get("ref_premium_pct") is not None
    }
    ordered = [p for p in peers_sorted if p["code"] == PRIMARY] + [
        p for p in peers_sorted if p["code"] != PRIMARY
    ]
    etfs: list[dict] = []
    for p in ordered:
        code = p["code"]
        if code == PRIMARY:
            row = {
                "code": code,
                "name": p.get("name") or "嘉实纳斯达克100",
                "p": primary_calc.get("p"),
                "p60": primary_calc.get("p60"),
                "peak_dd_pct": primary_calc.get("peak_dd_pct"),
                "peak_nav": primary_calc.get("peak_nav"),
                "peak_date": primary_calc.get("peak_date"),
                "day_chg_pct": primary_calc.get("day_chg_pct"),
                "day_from_date": primary_calc.get("day_from_date"),
                "mom_pct": primary_calc.get("mom_pct"),
                "mom_from_date": primary_calc.get("mom_from_date"),
                "mom_to_date": primary_calc.get("mom_to_date"),
                "nav_date": primary_calc.get("nav_date"),
                "is_nav_60_high": bool(primary_calc.get("is_nav_60_high")),
                "peer_min_premium": primary_calc.get("peer_min_premium"),
            }
        else:
            st = stats.get(code)
            if not st or p.get("ref_premium_pct") is None:
                continue
            others = [v for c, v in premiums.items() if c != code]
            row = {
                "code": code,
                "name": p.get("name") or code,
                "p": round(float(p["ref_premium_pct"]), 4),
                "p60": st["p60"],
                "peak_dd_pct": st.get("peak_dd_pct"),
                "peak_nav": st.get("peak_nav"),
                "peak_date": st.get("peak_date"),
                "day_chg_pct": st.get("day_chg_pct"),
                "day_from_date": st.get("day_from_date"),
                "mom_pct": st.get("mom_pct"),
                "mom_from_date": st.get("mom_from_date"),
                "mom_to_date": st.get("mom_to_date"),
                "nav_date": st.get("nav_date"),
                "is_nav_60_high": bool(st.get("is_nav_60_high")),
                "peer_min_premium": round(min(others), 4) if others else None,
            }
        if row["p"] is None or row["p60"] is None or row.get("peak_dd_pct") is None or row["peer_min_premium"] is None:
            continue
        etfs.append(row)
    freshest = max((e.get("nav_date") or "" for e in etfs), default="")
    for row in etfs:
        row["nav_stale"] = bool(row.get("nav_date") and freshest and row["nav_date"] < freshest)
        row["nav_freshest"] = freshest or None
    return etfs


def align_pick_nav_dates(picks: dict | None, nav_dates: dict[str, str]) -> dict | None:
    """净值日落后的基金不参与「今日优先」，避免拿错开的回撤窗口互相比较。"""
    if not picks or not nav_dates:
        return picks
    freshest = max(nav_dates.values())
    picks["nav_freshest"] = freshest
    anchors = {spec["id"]: spec["conditional"][0] for spec in PICK_GROUPS}
    for group in picks.get("groups") or []:
        items = group.get("items") or []
        for item in items:
            nav_date = nav_dates.get(item["code"])
            item["nav_date"] = nav_date
            item["nav_stale"] = bool(nav_date and nav_date < freshest)
        fresh = [it for it in items if not it.get("nav_stale")]
        today = choose_today(fresh) or choose_today(items)
        premiums = [it["ref_premium_pct"] for it in items if it.get("ref_premium_pct") is not None]
        floor = min(premiums) if premiums else None
        anchor = anchors.get(group.get("id"), "")
        median = group.get("median_premium_pct")
        for item in items:
            item["is_today"] = bool(today and item["code"] == today["code"])
            if floor is None:
                continue
            item["why"] = _why(item, today or item, floor, median, anchor)
            if item.get("nav_stale"):
                item["why"] += f" 净值日 {item['nav_date']}，落后于同类最新净值日 {freshest}，不拿它的回撤和其他基金比。"
                item["is_today"] = False
        if today and today.get("nav_stale"):
            today = choose_today(fresh)
        group["today_code"] = today["code"] if today else None
        group["today_line"] = (
            f"今日优先 {today['code']} {today.get('name') or ''} · 参考溢价 {today['ref_premium_pct']:.2f}%"
            if today
            else ""
        )
        items.sort(key=lambda it: (not it.get("is_today"), it.get("ref_premium_pct") if it.get("ref_premium_pct") is not None else 999))
    return picks


def _nasdaq_today(picks: dict | None) -> dict | None:
    if not picks:
        return None
    for group in picks.get("groups") or []:
        if group.get("id") != "nasdaq-100" or not group.get("today_code"):
            continue
        for item in group.get("items") or []:
            if item.get("code") == group["today_code"]:
                return item
    return None


def build_spx_block(picks: dict | None, peer_stats: dict[str, dict] | None) -> dict:
    """标普短名单各自的回撤、P60 和溢价。金额按 513500，不用纳指的 P60。"""
    group = None
    for item in (picks or {}).get("groups") or []:
        if item.get("id") == "sp-500":
            group = item
            break
    items = (group or {}).get("items") or []
    premiums = {
        it["code"]: it["ref_premium_pct"]
        for it in items
        if it.get("code") and it.get("ref_premium_pct") is not None
    }
    etfs: list[dict] = []
    for it in items:
        code = it.get("code")
        if not code:
            continue
        st = (peer_stats or {}).get(code) or {}
        others = [v for c, v in premiums.items() if c != code]
        p = it.get("ref_premium_pct")
        etfs.append(
            {
                "code": code,
                "name": it.get("name") or code,
                "p": None if p is None else round(float(p), 4),
                "p60": st.get("p60"),
                "peak_dd_pct": st.get("peak_dd_pct"),
                "peak_nav": st.get("peak_nav"),
                "peak_date": st.get("peak_date"),
                "day_chg_pct": st.get("day_chg_pct"),
                "day_from_date": st.get("day_from_date"),
                "mom_pct": st.get("mom_pct"),
                "mom_from_date": st.get("mom_from_date"),
                "mom_to_date": st.get("mom_to_date"),
                "nav_date": st.get("nav_date") or it.get("nav_date"),
                "is_nav_60_high": bool(st.get("is_nav_60_high")),
                "peer_min_premium": round(min(others), 4) if others else None,
                "nav_stale": bool(it.get("nav_stale")),
            }
        )
    primary = next((e for e in etfs if e["code"] == SPX_PRIMARY), None)
    briefing = None
    if (
        primary
        and primary.get("p") is not None
        and primary.get("p60") is not None
        and primary.get("peak_dd_pct") is not None
    ):
        dc, dlabel = spx_drawdown_coef(primary["peak_dd_pct"], primary.get("day_chg_pct"))
        pc, plabel, expensive = premium_coef(
            float(primary["p"]),
            float(primary["p60"]),
            primary.get("peer_min_premium"),
            primary.get("peak_dd_pct"),
        )
        today = None
        today_code = (group or {}).get("today_code")
        if today_code:
            today = next((it for it in items if it.get("code") == today_code), None)
        if expensive and today and today.get("code") != SPX_PRIMARY and today.get("ref_premium_pct") is not None:
            buy_target = {
                "code": today["code"],
                "name": today.get("name") or today["code"],
                "premium": today["ref_premium_pct"],
                "reason": "513500明显贵；标普短名单在约 2 个点价差内优先更低费率或更大规模",
            }
        else:
            buy_target = {
                "code": SPX_PRIMARY,
                "name": primary.get("name") or "博时标普500",
                "premium": primary["p"],
                "reason": "标普主标的",
            }
        briefing = {
            "baseline": SPX_BASELINE,
            "code": SPX_PRIMARY,
            "name": primary.get("name") or "博时标普500",
            "P": primary["p"],
            "P60": primary["p60"],
            "peak_dd_pct": primary["peak_dd_pct"],
            "peak_date": primary.get("peak_date"),
            "day_chg_pct": primary.get("day_chg_pct"),
            "mom_pct": primary.get("mom_pct"),
            "drawdown_coef": round(dc, 4),
            "drawdown_label": dlabel,
            "premium_coef": round(pc, 4),
            "premium_label": plabel,
            "is_obviously_expensive": expensive,
            "is_nav_60_high": bool(primary.get("is_nav_60_high")),
            "suggested_amount": round(SPX_BASELINE * dc * pc, 2),
            "buy_target": buy_target,
            "suggest_sell_tactical": bool(primary.get("is_nav_60_high") and expensive),
            "peer_min_premium": primary.get("peer_min_premium"),
        }
    return {
        "baseline": SPX_BASELINE,
        "primary": SPX_PRIMARY,
        "etfs": etfs,
        "briefing": briefing,
    }


US_PEAK_WINDOW_DAYS = 365
US_YAHOO = {
    "qqq": "QQQ",
    "spy": "SPY",
    "vix": "^VIX",
}


def _yahoo_daily(symbol: str, range_: str = "2y") -> list[dict]:
    """Yahoo chart API v8 日线。日期用 UTC，避免北京时间把美股交易日拨到下一天。"""
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + urllib.parse.quote(symbol)
        + f"?range={range_}&interval=1d&includeAdjustedClose=true"
    )
    raw = json.loads(get(url, retries=2, timeout=30))
    result = (raw.get("chart") or {}).get("result") or []
    if not result:
        err = ((raw.get("chart") or {}).get("error") or {}).get("description") or "空结果"
        raise RuntimeError(f"Yahoo {symbol}: {err}")
    block = result[0]
    ts = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    adj = (((block.get("indicators") or {}).get("adjclose") or [{}])[0] or {}).get("adjclose")
    closes = quote.get("close") or []
    rows = []
    for i, t in enumerate(ts):
        c = None
        if adj and i < len(adj) and adj[i] is not None:
            c = float(adj[i])
        elif i < len(closes) and closes[i] is not None:
            c = float(closes[i])
        if c is None or c <= 0:
            continue
        day = datetime.fromtimestamp(int(t), timezone.utc).strftime("%Y-%m-%d")
        rows.append({"date": day, "close": c})
    rows.sort(key=lambda r: r["date"])
    dedup: dict[str, float] = {}
    for r in rows:
        dedup[r["date"]] = r["close"]
    return [{"date": d, "close": dedup[d]} for d in sorted(dedup)]


def _cboe_vix() -> list[dict]:
    """CBOE 官方 VIX 日线，Yahoo 失败时用。"""
    raw = get(
        "https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv",
        retries=2,
        timeout=30,
    ).decode("utf-8", "replace")
    rows = []
    for line in raw.splitlines()[1:]:
        parts = line.split(",")
        if len(parts) < 5 or not parts[4]:
            continue
        try:
            day = datetime.strptime(parts[0].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
            rows.append({"date": day, "close": float(parts[4])})
        except ValueError:
            continue
    rows.sort(key=lambda r: r["date"])
    if len(rows) > 600:
        rows = rows[-600:]
    return rows


def iopv_drawdown_pct(peak_nav: float | None, iopv: float | None) -> float | None:
    """距近一年最高公布净值的回撤，分母是高点净值，分子用今日 IOPV 代替滞后的公布净值。"""
    if peak_nav is None or iopv is None:
        return None
    peak = float(peak_nav)
    est = float(iopv)
    if peak <= 0 or est <= 0:
        return None
    return max(0.0, (peak - est) / peak * 100.0)


def trailing_drawdown_pct(points: list[dict], window_days: int = US_PEAK_WINDOW_DAYS) -> dict[str, float]:
    """收盘价 ÷ 近 window_days 天（含当天）最高收盘价 − 1，单位百分点。负数或 0。"""
    out: dict[str, float] = {}
    for i, row in enumerate(points):
        day = row["date"]
        close = float(row["close"])
        start = (datetime.strptime(day, "%Y-%m-%d") - timedelta(days=window_days)).strftime("%Y-%m-%d")
        peak = close
        for prev in points[: i + 1]:
            if prev["date"] >= start and float(prev["close"]) > peak:
                peak = float(prev["close"])
        out[day] = round((close / peak - 1.0) * 100.0, 4) if peak > 0 else 0.0
    return out


def _load_saved_us() -> dict | None:
    path = DATA / "dashboard_payload.json"
    if not path.exists():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    us = saved.get("us")
    if isinstance(us, dict) and us.get("rows"):
        return us
    return None


def fetch_us_block() -> tuple[dict, list[str]]:
    """QQQ、SPY、VIX 约两年日线。回撤按近 365 天滚动高点预先算好。"""
    warnings: list[str] = []
    series: dict[str, list[dict]] = {}
    sources: dict[str, str] = {}
    for key, symbol in US_YAHOO.items():
        try:
            rows = _yahoo_daily(symbol)
            if len(rows) < 40:
                raise RuntimeError(f"只有 {len(rows)} 根")
            series[key] = rows
            sources[key] = f"Yahoo chart v8 {symbol}"
            print(f"美股 {symbol}: {rows[0]['date']} → {rows[-1]['date']} ({len(rows)})")
        except Exception as e:  # noqa: BLE001
            print(f"Yahoo {symbol} 失败:", e)
            if key == "vix":
                try:
                    rows = _cboe_vix()
                    if len(rows) < 40:
                        raise RuntimeError(f"CBOE 只有 {len(rows)} 根")
                    series[key] = rows
                    sources[key] = "CBOE VIX_History.csv"
                    warnings.append("VIX 改用 CBOE 日线（Yahoo 失败）")
                    print(f"VIX CBOE: {rows[0]['date']} → {rows[-1]['date']} ({len(rows)})")
                    continue
                except Exception as e2:  # noqa: BLE001
                    print("CBOE VIX 失败:", e2)
            warnings.append(f"{symbol} 日线没取到")
    if not series:
        old = _load_saved_us()
        if old:
            warnings.append("美股日线接口失败，沿用上一份 QQQ/SPY/VIX 快照")
            return old, warnings
        return {
            "peak_window_days": US_PEAK_WINDOW_DAYS,
            "peak_window_label": "近 365 天滚动最高收盘价",
            "sources": {},
            "warnings": warnings or ["QQQ、SPY、VIX 都没有取到"],
            "rows": [],
        }, warnings
    dd = {key: trailing_drawdown_pct(rows) for key, rows in series.items() if key != "vix"}
    by_date: dict[str, dict] = {}
    for key, rows in series.items():
        for row in rows:
            item = by_date.setdefault(row["date"], {"date": row["date"]})
            item[key] = round(float(row["close"]), 4)
            if key in dd:
                item[key + "_dd"] = dd[key][row["date"]]
    rows_out = [by_date[d] for d in sorted(by_date)]
    # 图上的「近一年」需要窗口起点之前还有一年高点，所以多留一段。
    if len(rows_out) > 560:
        rows_out = rows_out[-560:]
    block = {
        "peak_window_days": US_PEAK_WINDOW_DAYS,
        "peak_window_label": "近 365 天滚动最高收盘价",
        "sources": sources,
        "warnings": warnings,
        "rows": rows_out,
    }
    return block, warnings


def build_payload(
    premium_rows: list[dict],
    nav_rows: list[dict],
    closes: list[dict],
    peers_static: list[dict],
    spot: dict[str, dict],
    ref_date: str | None,
    warnings: list[str],
    picks: dict | None = None,
    peer_stats: dict[str, dict] | None = None,
    us: dict | None = None,
) -> dict:
    if len(premium_rows) < 30:
        raise RuntimeError(f"溢价历史过短: {len(premium_rows)}")
    last60 = [r["premium_pct"] for r in premium_rows[-60:]]
    p60 = statistics.median(last60)
    latest_p = premium_rows[-1]["premium_pct"]

    if closes:
        official = _official_bars(closes)
        latest_close_date = official[-1]["day"]
        latest_close = float(official[-1]["close"])
    else:
        latest_close_date = premium_rows[-1]["date"]
        latest_close = float(premium_rows[-1]["close"])

    if nav_rows:
        latest_nav_date = nav_rows[-1]["date"]
        latest_nav = float(nav_rows[-1]["nav"])
        nav_sorted = nav_rows
    else:
        latest_nav_date = premium_rows[-1]["date"]
        latest_nav = float(premium_rows[-1]["nav"])
        nav_sorted = [{"date": r["date"], "nav": r["nav"]} for r in premium_rows]

    last60_nav = nav_sorted[-60:]
    nav_60_high = max(r["nav"] for r in last60_nav)
    is_nav_60_high = latest_nav >= nav_60_high - 1e-12
    mom_pct, prev_date = mom_nav_change(nav_sorted, latest_nav_date, latest_nav)
    metrics = nav_metrics(nav_sorted)
    peak_dd = metrics["peak_dd_pct"]
    day_chg = metrics["day_chg_pct"]

    use_live = intraday_quote_active(spot)
    peers = []
    for s in peers_static:
        live = spot.get(s["code"], {})
        ref = s.get("ref_premium_pct")
        iopv = s.get("iopv")
        if use_live and live.get("premium") is not None:
            ref = live["premium"]
        if use_live and live.get("iopv"):
            iopv = live["iopv"]
        peers.append(
            {
                "code": s["code"],
                "name": s.get("name") or s["code"],
                "close": s.get("close") if s.get("close") is not None else live.get("price"),
                "iopv": iopv,
                "ref_premium_pct": None if ref is None else round(float(ref), 4),
                "nav_premium_pct": s.get("nav_premium_pct"),
                "live_price": live.get("price"),
                "live_asof": f"{live.get('date', '')} {live.get('time', '')}".strip() or None,
                "is_primary": s["code"] == PRIMARY,
            }
        )
    if not any(p["code"] == PRIMARY for p in peers):
        raise RuntimeError("同类列表缺少 159501")
    peers_sorted = sorted(peers, key=lambda x: (x["ref_premium_pct"] is None, x["ref_premium_pct"] if x["ref_premium_pct"] is not None else 999))

    with (DATA / "peers_latest.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "code",
                "name",
                "close",
                "iopv",
                "ref_premium_pct",
                "nav_premium_pct",
                "live_price",
                "live_asof",
                "is_primary",
            ],
        )
        w.writeheader()
        w.writerows(peers_sorted)

    peer_others = [p for p in peers_sorted if p["code"] != PRIMARY and p["ref_premium_pct"] is not None]
    peer_min_fund = peer_others[0] if peer_others else None
    peer_min = peer_min_fund["ref_premium_pct"] if peer_min_fund else None

    primary = next(p for p in peers_sorted if p["code"] == PRIMARY)
    ref_iopv_premium = primary["ref_premium_pct"] if primary["ref_premium_pct"] is not None else latest_p
    p_for_calc = float(ref_iopv_premium)
    live_primary = spot.get(PRIMARY) or {}
    if use_live and live_primary.get("premium") is not None:
        p_for_calc = float(live_primary["premium"])
    if use_live and picks:
        for group in picks.get("groups") or []:
            for item in group.get("items") or []:
                quote = spot.get(item.get("code") or "") or {}
                if quote.get("premium") is not None:
                    item["ref_premium_pct"] = round(float(quote["premium"]), 4)
    nav_dates = {code: st["nav_date"] for code, st in (peer_stats or {}).items() if st.get("nav_date")}
    nav_dates[PRIMARY] = latest_nav_date
    picks = align_pick_nav_dates(picks, nav_dates)
    iopv_dd = iopv_drawdown_pct(metrics.get("peak_nav"), live_primary.get("iopv") if use_live else None)
    dd_for_amount = iopv_dd if iopv_dd is not None else peak_dd
    dc, dlabel = drawdown_coef(dd_for_amount, day_chg)
    pc, plabel, is_expensive = premium_coef(p_for_calc, p60, peer_min, dd_for_amount)
    base = DEFAULT_BASELINE
    amount = base * dc * pc
    suggest_sell = bool(is_nav_60_high and is_expensive)
    today = _nasdaq_today(picks) if is_expensive else None
    if is_expensive and today:
        buy_target = {
            "code": today["code"],
            "name": today.get("name") or today["code"],
            "premium": today["ref_premium_pct"],
            "reason": "159501明显贵；短名单在约 2 个点价差内优先更低费率或更大规模",
        }
    elif is_expensive and peer_min_fund:
        buy_target = {
            "code": peer_min_fund["code"],
            "name": peer_min_fund["name"],
            "premium": peer_min_fund["ref_premium_pct"],
            "reason": "159501明显贵，优先同类更便宜",
        }
    else:
        buy_target = {
            "code": PRIMARY,
            "name": "嘉实纳斯达克100",
            "premium": round(p_for_calc, 4),
            "reason": "主仓代码或溢价未到换仓阈值",
        }
    parts = [
        (
            (
                f"距近一年高点回撤{dd_for_amount:.2f}%（按今日盘中IOPV，高点{metrics['peak_date']}；已公布净值回撤{peak_dd:.2f}%）"
                f"→回撤系数{dc:.2f}（{dlabel}）"
                if iopv_dd is not None and peak_dd is not None
                else (
                    f"距近一年高点回撤{peak_dd:.2f}%（已公布净值，高点{metrics['peak_date']}→净值日{latest_nav_date}）"
                    f"→回撤系数{dc:.2f}（{dlabel}）"
                    if peak_dd is not None
                    else "近一年高点回撤缺失"
                )
            )
        ),
        f"溢价{p_for_calc:.2f}%（{plabel}）→溢价系数{pc:.2f}",
        (
            f"较上月净值{mom_pct:+.2f}%（{prev_date}→{latest_nav_date}，只作参照）"
            if mom_pct is not None
            else "较上月净值缺失"
        ),
    ]
    if buy_target["code"] != PRIMARY:
        parts.append(f"建议买{buy_target['code']}（同类更低溢价{buy_target['premium']:.2f}%）")
    parts.append("净值近60日新高且明显贵→可卖战术仓5–8%" if suggest_sell else "暂不触发卖出")

    payload = {
        "asof": {
            "built_at": now_cst().strftime("%Y-%m-%d %H:%M:%S 北京时间"),
            "price_date": now_cst().strftime("%Y-%m-%d") if use_live else latest_close_date,
            "price_session": "intraday" if use_live else "close",
            "nav_date": latest_nav_date,
            "premium_method": "历史：收盘价/单位净值-1；今日展示溢价优先用 QDII Lens 收盘参考溢价（价/IOPV-1）",
            "sources": [
                "eastmoney pingzhongdata / lsjz NAV",
                "sina or tencent daily K-line",
                "qdiilens.com nasdaq-100 and sp-500 premium, fee and turnover",
                "eastmoney tsdata annualized tracking error",
            ],
            "warnings": warnings,
        },
        "primary": {
            "code": PRIMARY,
            "name": "嘉实纳斯达克100ETF",
            "close": round(latest_close, 4),
            "close_date": latest_close_date,
            "quote_price": round(float(live_primary["price"]), 4) if use_live and live_primary.get("price") else None,
            "quote_date": now_cst().strftime("%Y-%m-%d") if use_live and live_primary.get("price") else None,
            "quote_iopv": live_primary.get("iopv") if use_live else None,
            "quote_premium_pct": round(float(live_primary["premium"]), 4) if use_live and live_primary.get("premium") is not None else None,
            "nav": round(latest_nav, 4),
            "nav_date": latest_nav_date,
            "premium_nav_pct": round(latest_p, 4),
            "premium_ref_iopv_pct": round(p_for_calc, 4),
            "premium_ref_date": ref_date or latest_close_date,
            "p60": round(p60, 4),
            "p_minus_p60": round(p_for_calc - p60, 4),
            "nav_60_high": round(nav_60_high, 4),
            "is_nav_60_high": is_nav_60_high,
            "mom_nav_pct": round(mom_pct, 4) if mom_pct is not None else None,
            "mom_from_date": prev_date,
            "mom_to_date": latest_nav_date,
            "peak_dd_pct": peak_dd,
            "peak_dd_iopv_pct": None if iopv_dd is None else round(iopv_dd, 4),
            "peak_nav": metrics["peak_nav"],
            "peak_date": metrics["peak_date"],
            "peak_window_start": metrics["peak_window_start"],
            "day_chg_pct": day_chg,
            "day_from_date": metrics["day_from_date"],
        },
        "briefing": {
            "baseline": base,
            "P": round(p_for_calc, 4),
            "P60": round(p60, 4),
            "mom_pct": round(mom_pct, 4) if mom_pct is not None else None,
            "peak_dd_pct": peak_dd,
            "peak_dd_iopv_pct": None if iopv_dd is None else round(iopv_dd, 4),
            "peak_date": metrics["peak_date"],
            "day_chg_pct": day_chg,
            "drawdown_coef": round(dc, 4),
            "drawdown_label": dlabel,
            "premium_coef": round(pc, 4),
            "premium_label": plabel,
            "is_obviously_expensive": is_expensive,
            "suggested_amount": round(amount, 2),
            "suggested_raw": round(amount, 2),
            "buy_target": buy_target,
            "suggest_sell_tactical": suggest_sell,
            "sell_range_pct": [5, 8],
            "reason": "；".join(parts),
            "peer_min_premium": peer_min,
        },
        "history": premium_rows,
        "peers": peers_sorted,
        "picks": picks,
        "spx": build_spx_block(picks, peer_stats or {}),
        "us": us if us is not None else (_load_saved_us() or {
            "peak_window_days": US_PEAK_WINDOW_DAYS,
            "peak_window_label": "近 365 天滚动最高收盘价",
            "sources": {},
            "warnings": ["QQQ、SPY、VIX 还没有快照"],
            "rows": [],
        }),
        "calculator": {
            "etfs": _calculator_etfs(
                peers_sorted,
                peer_stats or {},
                {
                    "p": round(p_for_calc, 4),
                    "p60": round(p60, 4),
                    "peak_dd_pct": peak_dd,
                    "peak_nav": metrics["peak_nav"],
                    "peak_date": metrics["peak_date"],
                    "day_chg_pct": day_chg,
                    "day_from_date": metrics["day_from_date"],
                    "mom_pct": round(mom_pct, 4) if mom_pct is not None else None,
                    "mom_from_date": prev_date,
                    "mom_to_date": latest_nav_date,
                    "nav_date": latest_nav_date,
                    "is_nav_60_high": is_nav_60_high,
                    "peer_min_premium": round(peer_min, 4) if peer_min is not None else None,
                },
            )
        },
    }
    def _stamp_iopv(rows: list[dict] | None) -> None:
        for row in rows or []:
            quote = spot.get(row.get("code") or "") or {}
            if quote.get("iopv"):
                row["iopv"] = quote["iopv"]
                row["iopv_asof"] = quote.get("date") or ""

    _stamp_iopv((payload.get("calculator") or {}).get("etfs"))
    _stamp_iopv((payload.get("spx") or {}).get("etfs"))
    atomic_write(
        ROOT / "data.js",
        "window.DASHBOARD_DATA = " + json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + ";\n",
    )
    atomic_write(DATA / "dashboard_payload.json", json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    with (DATA / "premium_159501.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["date", "close", "nav", "premium_pct"])
        w.writeheader()
        w.writerows(premium_rows)
    print(
        f"data.js ready | history={len(premium_rows)} | P={p_for_calc:.2f} P60={p60:.2f} "
        f"| suggest=¥{amount:.0f} -> {buy_target['code']}"
    )
    return payload


def main() -> None:
    print("Refreshing dashboard data…")
    _self_check()
    warnings: list[str] = []

    nav_rows: list[dict] = []
    closes: list[dict] = []
    peers: list[dict] = []
    ref_date: str | None = None
    picks: dict | None = None

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_nav = pool.submit(fetch_nav, PRIMARY)
        fut_close = pool.submit(fetch_closes)
        fut_lens = pool.submit(fetch_qdiilens_bundle)
        try:
            nav_rows = fut_nav.result()
            _save_nav_csv(nav_rows)
        except Exception as e:  # noqa: BLE001
            print("净值拉取失败:", e)
            nav_rows = _load_nav_csv()
            warnings.append("净值接口失败，已用本地 nav 快照" if nav_rows else "净值接口失败且无本地快照")
        try:
            closes = fut_close.result()
        except Exception as e:  # noqa: BLE001
            print("行情拉取失败:", e)
            warnings.append("行情接口失败，溢价历史沿用本地 CSV")
        try:
            peers, ref_date, picks = fut_lens.result()
        except Exception as e:  # noqa: BLE001
            print("QDII Lens 失败:", e)
            peers = _load_peers_csv() or [dict(p) for p in BAKED_PEERS]
            picks = _load_saved_picks()
            warnings.append("同类溢价接口失败，已用上一份快照")

    official_closes = _official_bars(closes) if closes else []
    fresh = align_premium(nav_rows, official_closes) if nav_rows and official_closes else []
    old = _load_premium_csv()
    premium_rows, merged = merge_history(fresh, old)
    if merged:
        warnings.append("本次行情短于存档，已并入旧的溢价序列")
    if not fresh and old:
        warnings.append("本次没能重算历史溢价，沿用 premium_159501.csv")
        premium_rows = old
    if not premium_rows:
        raise SystemExit("没有溢价历史，无法生成 data.js")

    codes = []
    seen = set()
    for row in peers:
        if row["code"] not in seen:
            seen.add(row["code"])
            codes.append(row["code"])
    for code in sorted(KNOWN_NASDAQ):
        if code not in seen:
            seen.add(code)
            codes.append(code)
    spot = fetch_spot(codes)
    peer_codes = [c for c in dict.fromkeys([*codes, *PICK_CODES]) if c != PRIMARY]
    peer_stats = fetch_peer_calc_stats(peer_codes)
    missing = [c for c in peer_codes if c not in peer_stats and c in {p["code"] for p in peers}]
    if missing:
        warnings.append("部分同类缺少近一年高点回撤或 P60：" + "、".join(missing))

    us_block, us_warnings = fetch_us_block()
    warnings.extend(us_warnings)
    payload = build_payload(
        premium_rows, nav_rows, closes, peers, spot, ref_date, warnings, picks, peer_stats, us_block
    )
    b = payload["briefing"]
    print(
        f"建议 ¥{b['suggested_amount']:.0f} 买入 {b['buy_target']['code']} "
        f"| 卖战术仓={b['suggest_sell_tactical']} | warnings={warnings or '无'}"
    )


if __name__ == "__main__":
    main()

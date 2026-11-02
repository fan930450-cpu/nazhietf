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


def drawdown_coef(chg: float | None) -> tuple[float, str]:
    """与 index.html drawdownCoef 一致。"""
    if chg is None:
        return 1.0, "数据不足"
    if chg >= 5:
        return 0.4, "上涨≥5%"
    if chg > 2:
        return 0.7, "上涨0–5%"
    if chg >= -2:
        return 1.0, "持平±2%"
    if chg > -5:
        return 1.2, "回撤2–5%"
    if chg > -10:
        t = (-chg - 5) / 5.0
        return 1.5 + 0.5 * t, "回撤5–10%"
    return 2.0, "回撤≥10%"


def premium_coef(p: float, p60: float, peer_min_p: float | None) -> tuple[float, str, bool]:
    """与 index.html premiumCoef 一致。"""
    if (p - p60 >= 5) or (peer_min_p is not None and p - peer_min_p >= 5) or (p >= 18):
        return 0.5, "明显贵", True
    if p60 - p >= 3:
        return 1.3, "相对便宜", False
    if abs(p - p60) <= 2:
        return 1.0, "中性", False
    if p > p60:
        return 0.75, "略贵", False
    return 1.0, "中性", False


def _self_check() -> None:
    expect = [
        (5, 0.4),
        (2.01, 0.7),
        (2, 1.0),
        (0, 1.0),
        (-2, 1.0),
        (-3.5255, 1.2),
        (-5, 1.5),
        (-7.5, 1.75),
        (-10, 2.0),
    ]
    for chg, coef in expect:
        got, _ = drawdown_coef(chg)
        if abs(got - coef) > 1e-9:
            raise SystemExit(f"回撤系数自检失败 chg={chg} got={got} expect={coef}")
    pc, _label, expensive = premium_coef(16.83, 12.64, 8.72)
    if pc != 0.5 or not expensive:
        raise SystemExit("溢价系数自检失败：明显贵")
    pc, label, expensive = premium_coef(10, 14, 9)
    if pc != 1.3 or label != "相对便宜" or expensive:
        raise SystemExit("溢价系数自检失败：相对便宜")
    pc, label, expensive = premium_coef(16, 12, 15)
    if pc != 0.75 or label != "略贵" or expensive:
        raise SystemExit("溢价系数自检失败：略贵")
    amount = min(4000 * 1.2 * 0.5, 8000)
    if amount != 2400:
        raise SystemExit("建议金额自检失败")


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


def parse_qdiilens(html: str) -> tuple[list[dict], str | None]:
    text = html
    if '\\"code\\"' in html or '\\"quote\\"' in html:
        text = html.replace('\\"', '"').replace("\\/", "/")

    prem_re = re.compile(
        r'\{"code":"(\d{6})","name":"([^"]+)","index":"([^"]+)",'
        r'"trading_date":"(\d{4}-\d{2}-\d{2})","prev_trading_date":"[^"]+",'
        r'"premium_pct":([-0-9.]+)'
    )
    by_code: dict[str, dict] = {}
    for code, name, index, tdate, prem in prem_re.findall(text):
        if index != "nasdaq-100" and code not in KNOWN_NASDAQ:
            continue
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
        if code not in by_code:
            continue
        row = by_code[code]
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

    peers = [by_code[c] for c in sorted(by_code)]
    ref_date = None
    primary = by_code.get(PRIMARY)
    if primary:
        ref_date = primary.get("trading_date")
    elif peers:
        ref_date = peers[0].get("trading_date")
    return peers, ref_date


def fetch_qdiilens_peers() -> tuple[list[dict], str | None]:
    html = get("https://qdiilens.com/", timeout=40).decode("utf-8", "replace")
    peers, ref_date = parse_qdiilens(html)
    if not any(p["code"] == PRIMARY for p in peers) or len(peers) < 5:
        raise RuntimeError(f"QDII Lens 解析到的纳指 ETF 不足: {len(peers)}")
    print(f"QDII Lens peers: {len(peers)} ref_date={ref_date}")
    return peers, ref_date


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


def fetch_tencent_spot(codes: list[str]) -> dict[str, dict]:
    url = "https://qt.gtimg.cn/q=" + ",".join(symbol_of(c) for c in codes)
    text = get(url).decode("gbk", "replace")
    spot: dict[str, dict] = {}
    for line in text.splitlines():
        m = re.match(r'v_(sz|sh)(\d+)="(.*)"', line.strip().rstrip(";"))
        if not m:
            continue
        fields = m.group(3).split("~")
        if len(fields) < 4 or not fields[3]:
            continue
        code = m.group(2)
        stamp = fields[30] if len(fields) > 30 else ""
        spot[code] = {
            "name": fields[1],
            "price": float(fields[3]),
            "date": stamp[:8],
            "time": stamp[8:14],
        }
    if len(spot) < 3:
        raise RuntimeError("腾讯行情不足")
    print("Tencent spot:", ",".join(sorted(spot)))
    return spot


def fetch_spot(codes: list[str]) -> dict[str, dict]:
    for fn in (fetch_sina_spot, fetch_tencent_spot):
        try:
            return fn(codes)
        except Exception as e:  # noqa: BLE001
            print("现价失败:", fn.__name__, e)
    return {}


def mom_nav_change(nav_sorted: list[dict], latest_date: str, latest_nav: float):
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


def build_payload(
    premium_rows: list[dict],
    nav_rows: list[dict],
    closes: list[dict],
    peers_static: list[dict],
    spot: dict[str, dict],
    ref_date: str | None,
    warnings: list[str],
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

    peers = []
    for s in peers_static:
        live = spot.get(s["code"], {})
        ref = s.get("ref_premium_pct")
        peers.append(
            {
                "code": s["code"],
                "name": s.get("name") or s["code"],
                "close": s.get("close") if s.get("close") is not None else live.get("price"),
                "iopv": s.get("iopv"),
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
    dc, dlabel = drawdown_coef(mom_pct)
    pc, plabel, is_expensive = premium_coef(p_for_calc, p60, peer_min)
    base = 4000.0
    raw = base * dc * pc
    amount = min(raw, base * 2)
    suggest_sell = bool(is_nav_60_high and is_expensive)
    if is_expensive and peer_min_fund:
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
        f"较上月净值{mom_pct:+.2f}%→回撤系数{dc:.2f}" if mom_pct is not None else "较上月净值缺失",
        f"溢价{p_for_calc:.2f}%（{plabel}）→溢价系数{pc:.2f}",
    ]
    if buy_target["code"] != PRIMARY:
        parts.append(f"建议买{buy_target['code']}（同类更低溢价{buy_target['premium']:.2f}%）")
    parts.append("净值近60日新高且明显贵→可卖战术仓5–8%" if suggest_sell else "暂不触发卖出")

    payload = {
        "asof": {
            "built_at": now_cst().strftime("%Y-%m-%d %H:%M:%S 北京时间"),
            "price_date": latest_close_date,
            "nav_date": latest_nav_date,
            "premium_method": "历史：收盘价/单位净值-1；今日展示溢价优先用 QDII Lens 收盘参考溢价（价/IOPV-1）",
            "sources": [
                "eastmoney pingzhongdata / lsjz NAV",
                "sina or tencent daily K-line",
                "qdiilens.com nasdaq-100 premium snapshot",
            ],
            "warnings": warnings,
        },
        "primary": {
            "code": PRIMARY,
            "name": "嘉实纳斯达克100ETF",
            "close": round(latest_close, 4),
            "close_date": latest_close_date,
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
        },
        "briefing": {
            "baseline": base,
            "P": round(p_for_calc, 4),
            "P60": round(p60, 4),
            "mom_pct": round(mom_pct, 4) if mom_pct is not None else None,
            "drawdown_coef": round(dc, 4),
            "drawdown_label": dlabel,
            "premium_coef": pc,
            "premium_label": plabel,
            "is_obviously_expensive": is_expensive,
            "suggested_amount": round(amount, 2),
            "suggested_raw": round(raw, 2),
            "buy_target": buy_target,
            "suggest_sell_tactical": suggest_sell,
            "sell_range_pct": [5, 8],
            "reason": "；".join(parts),
            "peer_min_premium": peer_min,
        },
        "history": premium_rows,
        "peers": peers_sorted,
    }
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

    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_nav = pool.submit(fetch_nav, PRIMARY)
        fut_close = pool.submit(fetch_closes)
        fut_lens = pool.submit(fetch_qdiilens_peers)
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
            peers, ref_date = fut_lens.result()
        except Exception as e:  # noqa: BLE001
            print("QDII Lens 失败:", e)
            peers = _load_peers_csv() or [dict(p) for p in BAKED_PEERS]
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

    payload = build_payload(premium_rows, nav_rows, closes, peers, spot, ref_date, warnings)
    b = payload["briefing"]
    print(
        f"建议 ¥{b['suggested_amount']:.0f} 买入 {b['buy_target']['code']} "
        f"| 卖战术仓={b['suggest_sell_tactical']} | warnings={warnings or '无'}"
    )


if __name__ == "__main__":
    main()

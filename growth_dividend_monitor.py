#!/usr/bin/env python3
"""
成长/红利风格轮动 — 缓冲均线策略 每日监控工具
基于 ZF1Huang/growth_dividend_rotation_research 的均线趋势策略 + 缓冲区

策略逻辑:
  1. Ratio = 成长指数收盘价 / 红利指数收盘价
  2. MA   = Ratio 的 20日简单移动平均
  3. 上轨 = MA × (1 + band), 下轨 = MA × (1 - band)
  4. Ratio >= 上轨  → 成长期 (切入成长)
  5. Ratio <= 下轨  → 红利期 (切入红利)
  6. 上下轨之间     → 维持原仓位 (缓冲区, 不调仓)

默认 band = 1% (0.01), 来自原研究中收益回撤最优的参数

数据源: 腾讯财经 → 新浪财经 → 东方财富 (三层容灾)
指数: 创业板指 399006 / 中证红利 000922
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, date
from pathlib import Path

import requests

# ─── 配置 ────────────────────────────────────────────
INDICES = {
    "growth_cyb":  {"name": "创业板指",  "code": "399006", "market": "sz"},
    "dividend":    {"name": "中证红利", "code": "000922", "market": "sh"},
}

MA_WINDOW = 20          # 均线窗口
MA_BAND = 0.01          # 缓冲区带宽 1% (原研究最优参数)
DATA_DAYS = 2000        # 约8年交易日(腾讯API上限)，支持长期收益对比
ONE_WAY_FEE = 0.001     # 单边交易费率 0.1%
CACHE_VERSION = 1       # 价格缓存版本锁: 改 MA_WINDOW/MA_BAND/买卖逻辑/DATA_DAYS 时 +1, 自动失效重抓

CACHE_DIR = Path.cwd() / ".growth_dividend_cache"
CACHE_DIR.mkdir(exist_ok=True)

# ─── 飞书推送配置 (GitHub Actions 通过 Secrets 注入) ───
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aae07cacb1785bdb")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_228f22564f13ddf89372bcbfb0513921")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

EASTMONEY_SECID = {"399006": "0.399006", "000922": "1.000922"}

# ─── 篮子策略配置 (创业板50 TOP3 × 红利信号轮动) ─────────
# 创红方案: 用「创业板指/红利 缓冲均线信号」作总开关, 成长期开盘买入当期
# 创业板50 前3权重等权篮子(1/3), 红利期空仓; 半年官方生效日调仓.
BASKET_CACHE_DIR = CACHE_DIR / "basket_cache"
BASKET_CACHE_DIR.mkdir(exist_ok=True)
BASKET_CACHE_VERSION = 1          # 篮子个股数据版本锁: 改 MA_WINDOW/调仓名单/买卖逻辑时 +1
BASKET_INDEX = {"code": "399673", "market": "sz"}   # 创业板50 (基准/调仓来源)

# 13 只曾进入 TOP3 的个股 (覆盖 2014-06 ~ 2026-06 全部 21 期)
STOCK_CODES = ["300104", "300059", "300070", "300033", "300433", "300136",
               "300015", "300750", "300760", "300274", "300124", "300308", "300502"]

# 21 期 TOP3 (用户提供的国证创业板50官方调样; 末期为当前期, 下次 2026-12-14 生效)
SCHEDULE = {
    "2014-06-18": ["300104", "300059", "300070"], "2014-10-08": ["300104", "300059", "300033"],
    "2015-04-01": ["300059", "300104", "300033"], "2015-10-08": ["300104", "300059", "300433"],
    "2016-04-01": ["300059", "300104", "300433"], "2016-10-10": ["300059", "300433", "300136"],
    "2017-04-05": ["300059", "300136", "300433"], "2017-10-09": ["300059", "300433", "300136"],
    "2018-04-02": ["300059", "300433", "300015"], "2018-10-08": ["300750", "300059", "300015"],
    "2019-06-10": ["300059", "300760", "300015"], "2019-12-09": ["300760", "300059", "300750"],
    "2020-06-15": ["300750", "300760", "300059"], "2020-12-14": ["300750", "300760", "300274"],
    "2021-06-15": ["300750", "300274", "300760"], "2021-12-13": ["300750", "300274", "300124"],
    "2022-06-13": ["300750", "300274", "300124"], "2022-12-12": ["300750", "300760", "300059"],
    "2023-06-12": ["300750", "300308", "300760"], "2023-12-11": ["300750", "300308", "300274"],
    "2024-06-11": ["300750", "300308", "300274"], "2024-12-09": ["300750", "300308", "300502"],
    "2025-06-16": ["300750", "300308", "300502"], "2025-12-15": ["300750", "300308", "300502"],
    "2026-06-15": ["300750", "300308", "300502"],   # 当前期 (与国证官网 2026-07-31 快照一致)
}

# 个股简称 (报告文字用)
STOCK_NAMES = {"300104": "乐视网", "300059": "东方财富", "300070": "碧水源", "300033": "同花顺",
               "300433": "蓝思科技", "300136": "信维通信", "300015": "爱尔眼科", "300750": "宁德时代",
               "300760": "迈瑞医疗", "300274": "阳光电源", "300124": "汇川技术", "300308": "中际旭创",
               "300502": "新易盛"}

# 国证创业板50 半年度调仓规则: 每年6月/12月第二个星期五的下一交易日生效
CNINDEX_TOP3_URL = "https://www.cnindex.com.cn/sample-detail/download-history?indexcode=399673"


# ─── 数据获取 (四层容灾) ──────────────────────────────

def fetch_tencent_kline(market: str, code: str, days: int) -> list[dict] | None:
    """从腾讯财经获取日K线"""
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{market}{code},day,,,{days},qfq"}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        klines = data.get("data", {}).get(f"{market}{code}", {}).get("qfqday", []) or \
                 data.get("data", {}).get(f"{market}{code}", {}).get("day", [])
        if not klines:
            return None
        return [{"date": str(k[0]), "open": float(k[1]), "close": float(k[2])} for k in klines]
    except Exception as e:
        print(f"  腾讯 {market}{code} 失败: {e}")
        return None


def fetch_sina_kline(market: str, code: str, days: int) -> list[dict] | None:
    """从新浪财经获取日K线"""
    sina_code = f"{market}{code}"
    url = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
    params = {"symbol": sina_code, "scale": str(min(days, 240)), "ma": "no", "datalen": str(days)}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if not data or not isinstance(data, list):
            return None
        records = []
        for k in data:
            if "day" in k and "close" in k:
                rec = {"date": str(k["day"]), "close": float(k["close"])}
                if k.get("open") not in (None, ""):
                    rec["open"] = float(k["open"])
                records.append(rec)
        return records if len(records) >= 15 else None
    except Exception as e:
        print(f"  新浪 {sina_code} 失败: {e}")
        return None


def fetch_eastmoney(code: str, days: int) -> list[dict] | None:
    """从东方财富获取日K线"""
    secid = EASTMONEY_SECID.get(code, "")
    if not secid:
        return None
    url = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
    params = {
        "secid": secid,
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101", "fqt": "1", "end": "20500101", "lmt": str(days),
    }
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if not klines:
            return None
        records = []
        for line in klines:
            parts = line.split(",")
            records.append({"date": parts[0], "open": float(parts[1]), "close": float(parts[2])})
        return records
    except Exception as e:
        print(f"  东方财富 {code} 失败: {e}")
        return None


def load_cache(index_code: str) -> list[dict] | None:
    cache_file = CACHE_DIR / f"{index_code}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        # 版本锁: 策略参数变更后旧缓存自动失效, 强制重抓
        if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
            return None
        records = data.get("records")
        if isinstance(records, list) and len(records) >= 15:
            return records
    except Exception:
        pass
    return None


def save_cache(index_code: str, records: list[dict]):
    payload = {"version": CACHE_VERSION, "records": records}
    (CACHE_DIR / f"{index_code}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def load_long_csv_data() -> dict[str, list[dict]] | None:
    """从博主15年CSV数据加载更长历史 (用于累计收益图扩展时间轴)
    
    CSV 来源: ZF1Huang/growth_dividend_rotation_research data_cache/
    数据范围:
      - 399006 (创业板指): 2010-06-01 起 (~16年)
      - 000922 (中证红利):  2007-05-28 起 (~19年)  
    
    返回: {code: [{"date":..., "close":...}, ...]} 或 None
    """
    csv_dir = Path(__file__).parent / "long_data"
    codes = ["399006", "000922"]
    result = {}
    for code in codes:
        csv_path = csv_dir / f"{code}.csv"
        if not csv_path.exists():
            return None
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader)
                ci = header.index("close") if "close" in header else 2
                oi = header.index("open") if "open" in header else 1
                rows = []
                for r in reader:
                    if len(r) > ci:
                        rec = {"date": r[0], "close": float(r[ci])}
                        if len(r) > oi:
                            try:
                                rec["open"] = float(r[oi])
                            except ValueError:
                                pass
                        rows.append(rec)
                result[code] = rows
        except Exception as e:
            print(f"  加载CSV {code} 失败: {e}")
            return None
    return result


def get_index_data(index_info: dict) -> list[dict] | None:
    """获取指数日线数据，多源容灾 + 本地缓存"""
    code = index_info["code"]
    market = index_info["market"]
    days = DATA_DAYS

    cached = load_cache(code)
    if cached and len(cached) >= 15:
        latest_date = max(r["date"] for r in cached)
        if latest_date >= (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d"):
            return cached
        print(f"  缓存过期 ({latest_date})，刷新...")

    sources = [
        ("腾讯财经", lambda: fetch_tencent_kline(market, code, days)),
        ("新浪财经", lambda: fetch_sina_kline(market, code, days)),
        ("东方财富", lambda: fetch_eastmoney(code, days)),
    ]

    for name, fetcher in sources:
        try:
            records = fetcher()
            if records and len(records) >= 15:
                save_cache(code, records)
                return records
            print(f"  [{name}] 数据不足, 尝试下一源")
        except Exception as e:
            print(f"  [{name}] 异常: {e}")
        time.sleep(0.5)

    if cached:
        print(f"  使用过期缓存")
        return cached
    return None


# ─── 篮子个股数据抓取 (hfq, 乐视特例 qfq) ────────────────

def _fetch_one_chunk_basket(market, code, start, end, adj):
    """抓取单块日K线; adj='hfq'|'qfq'; 解析 hfqday/qfqday/day"""
    url = "http://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    params = {"param": f"{market}{code},day,{start},{end},800,{adj}"}
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        node = data.get("data", {}).get(f"{market}{code}", {})
        if not isinstance(node, dict):
            return []
        klines = (node.get("hfqday") or node.get("qfqday") or node.get("day") or [])
        if not klines:
            return []
        return [{"date": str(k[0]), "open": float(k[1]), "close": float(k[2])} for k in klines]
    except Exception as e:
        print(f"  篮子 {market}{code}({adj}) 抓取失败: {e}")
        return []


def fetch_kline_basket(market, code, start="2014-06-18", end="2026-12-31", adj="hfq"):
    """按 400 天分块抓取个股全历史 (避免腾讯单笔上限)"""
    from datetime import timedelta
    cur = datetime.strptime(start, "%Y-%m-%d")
    end_d = datetime.strptime(end, "%Y-%m-%d")
    recs = []
    seen = set()
    while cur < end_d:
        nxt = min(cur + timedelta(days=400), end_d)
        cs, ce = cur.strftime("%Y-%m-%d"), nxt.strftime("%Y-%m-%d")
        chunk = _fetch_one_chunk_basket(market, code, cs, ce, adj)
        for r in chunk:
            if r["date"] not in seen:
                seen.add(r["date"])
                recs.append(r)
        cur = nxt
    recs.sort(key=lambda r: r["date"])
    return recs


def load_or_fetch_basket(market, code, adj):
    """版本锁缓存个股 hfq/qfq 数据到 BASKET_CACHE_DIR"""
    cf = BASKET_CACHE_DIR / f"{market}{code}_{adj}.json"
    if cf.exists():
        try:
            d = json.loads(cf.read_text(encoding="utf-8"))
            if isinstance(d, dict) and d.get("version") == BASKET_CACHE_VERSION and d.get("records"):
                return d["records"]
        except Exception:
            pass
    recs = fetch_kline_basket(market, code, adj=adj)
    if recs:
        cf.write_text(json.dumps({"version": BASKET_CACHE_VERSION, "records": recs},
                                 ensure_ascii=False), encoding="utf-8")
    return recs


# ─── 篮子对齐 / 调仓 / 模拟 ─────────────────────────────

def get_bucket_codes(date_str):
    """返回生效日 <= date_str 的最新一期 TOP3 (字符串日期可比)"""
    best = None
    for sd in SCHEDULE:
        if sd <= date_str:
            best = sd
    return SCHEDULE[best]


def align_to_master_basket(recs, master_dates, zero_after_last=True):
    """把个股/指数日线对齐到 master_dates; 历史缺口前Fill, 末日之后→0(退市)."""
    by = {r["date"]: r for r in recs}
    opens, closes = [], []
    last_o = last_c = None
    last_idx = -1
    for i, d in enumerate(master_dates):
        rec = by.get(d)
        if rec:
            o = rec.get("open", rec["close"])
            c = rec["close"]
            last_o, last_c, last_idx = o, c, i
            opens.append(o)
            closes.append(c)
        else:
            if last_idx >= 0:
                opens.append(last_o)
                closes.append(last_c)
            else:
                opens.append(None)
                closes.append(None)
    if zero_after_last:
        for i in range(last_idx + 1, len(master_dates)):
            opens[i] = 0.0
            closes[i] = 0.0
    return opens, closes


def simulate_enhanced(master_dates, sig_by_date, stock_open, stock_close,
                      div_open, div_close, div_on):
    """创红方案净值模拟: 开盘价执行, T-1收盘信号→T开盘; 含 ONE_WAY_FEE; 退市→末日后0。

    - div_on=False: 红利期空仓(=1.0)  ← 创红方案采用此口径
    - div_on=True:  红利期持有中证红利
    返回值 (nav, positions), 长度=len(master_dates)。
    """
    n = len(master_dates)

    def basket_oc(i, codes):
        o = sum(stock_open[c][i] for c in codes) / len(codes)
        c = sum(stock_close[c][i] for c in codes) / len(codes)
        return o, c

    first_idx = next((i for i in range(n)
                      if sig_by_date.get(master_dates[i]) is not None), None)
    if first_idx is None or first_idx + 1 >= n:
        return [1.0] * n, [None] * n
    trade_date = first_idx + 1
    init = sig_by_date[master_dates[first_idx]]

    nav = [1.0] * n
    positions = [None] * n
    if init == 1:
        codes = get_bucket_codes(master_dates[trade_date])
        o0, c0 = basket_oc(trade_date, codes)
        nav0 = (c0 / o0) * (1 - ONE_WAY_FEE)
        cur_codes = codes
        pos = 1
    else:
        if div_on:
            do = div_open[trade_date]
            dc = div_close[trade_date]
            nav0 = (dc / do) * (1 - ONE_WAY_FEE) if (do and do > 0) else 1.0
        else:
            nav0 = 1.0
        cur_codes = None
        pos = 0
    nav[trade_date] = nav0
    positions[trade_date] = init

    for i in range(trade_date + 1, n):
        sig_prev = sig_by_date.get(master_dates[i - 1])
        if sig_prev is None:
            sig_prev = pos
        tgt = sig_prev
        if pos == 1:
            o_prev, c_prev = basket_oc(i - 1, cur_codes)
            o_cur, c_cur = basket_oc(i, cur_codes)
            ov = o_cur / c_prev
            if tgt == 1:
                new_codes = get_bucket_codes(master_dates[i])
                if new_codes != cur_codes:
                    o1n, c1n = basket_oc(i, new_codes)
                    idr = (1 - ONE_WAY_FEE) * (c1n / o1n)
                    cur_codes = new_codes
                else:
                    idr = c_cur / o_cur
                dayfac = ov * idr
            else:
                if div_on:
                    do = div_open[i]
                    dc = div_close[i]
                    idr = (1 - ONE_WAY_FEE) * (dc / do) if (do and do > 0) else (1 - ONE_WAY_FEE)
                else:
                    idr = (1 - ONE_WAY_FEE)
                dayfac = ov * idr
                cur_codes = None
        else:
            if div_on:
                dc_prev = div_close[i - 1]
                do_cur = div_open[i]
                dc_cur = div_close[i]
                ov = do_cur / dc_prev if (dc_prev and dc_prev > 0) else 1.0
            else:
                ov = 1.0
            if tgt == 0:
                if div_on:
                    dc_cur = div_close[i]
                    do_cur = div_open[i]
                    idr = dc_cur / do_cur if (do_cur and do_cur > 0) else 1.0
                else:
                    idr = 1.0
                dayfac = ov * idr
            else:
                codes = get_bucket_codes(master_dates[i])
                o1, c1 = basket_oc(i, codes)
                dayfac = ov * (1 - ONE_WAY_FEE) * (c1 / o1)
                cur_codes = codes
        nav[i] = nav[i - 1] * dayfac
        positions[i] = tgt
        pos = tgt
    return nav, positions


# ─── 策略计算 ──────────────────────────────────────────

def align_data(series_a: list[dict], series_b: list[dict]) -> tuple[list, list, list]:
    """对齐两组数据的日期"""
    lookup_a = {r["date"]: r["close"] for r in series_a}
    lookup_b = {r["date"]: r["close"] for r in series_b}
    common_dates = sorted(set(lookup_a.keys()) & set(lookup_b.keys()))
    return common_dates, [lookup_a[d] for d in common_dates], [lookup_b[d] for d in common_dates]


def calc_ma(values: list[float], window: int) -> list[float | None]:
    """计算简单移动平均"""
    ma = []
    for i in range(len(values)):
        if i < window - 1:
            ma.append(None)
        else:
            ma.append(sum(values[i - window + 1: i + 1]) / window)
    return ma


def calc_ma_band_signal(ratios: list[float], mas: list[float | None],
                        band: float) -> list[dict]:
    """
    计算缓冲均线信号

    规则:
      - Ratio >= MA × (1 + band)  → state = 1 (成长期, 持成长)
      - Ratio <= MA × (1 - band)  → state = 0 (红利期, 持红利)
      - 缓冲区内 → state 延续上一日 (由信号链的上一日决定，完全由数据确定)

    返回: [{"date", "ratio", "ma", "upper", "lower", "state", "signal", "zone"}, ...]
    """
    signals = []
    last_state = None

    for i in range(len(ratios)):
        if mas[i] is None:
            continue

        r = ratios[i]
        m = mas[i]
        upper = m * (1 + band)
        lower = m * (1 - band)

        if r >= upper:
            state = 1
            zone = "突破上轨"
        elif r <= lower:
            state = 0
            zone = "跌破下轨"
        else:
            # 缓冲区内
            if last_state is None:
                state = 1 if r >= m else 0   # 初始化: 按均线判断
                zone = "缓冲区内(初始)"
            else:
                state = last_state
                zone = "缓冲区内(维持)"

        last_state = state
        signals.append({
            "date": "",
            "ratio": round(r, 4),
            "ma": round(m, 4),
            "upper": round(upper, 4),
            "lower": round(lower, 4),
            "state": state,
            "signal": "成长" if state == 1 else "红利",
            "zone": zone,
        })

    return signals


def calc_strategy_returns(growth_closes: list[float], div_closes: list[float],
                          growth_opens: list[float | None], div_opens: list[float | None],
                          dates_list: list[str]) -> tuple[list, list, list, list]:
    """计算策略轮动收益 (无前视偏差, 开盘价执行)。

    方法论与 growth_dividend_backtest.py 完全一致:
      - Day T-1 收盘信号 → Day T 开盘执行调仓
      - 隔夜收益: 前仓位 × (T开盘 / T-1收盘)
      - 日内收益: 新仓位 × (T收盘 / T开盘)，调仓日扣单边费 ONE_WAY_FEE
      - 红利贡献净值只在持红利期间累积红利资产收益；成长贡献同理
      - 恒等式: 策略终值 = 红利贡献 × 成长贡献 (误差≈0)

    返回 (nav, positions, div_nav, growth_nav)，四者长度均等于 len(dates_list)，
    nav[i]/positions[i] 对应 dates_list[i]；建仓前的日期 nav=1.0, position=None。
    opens 为与 dates_list 对齐的列表，缺失(None)时回退为同日的收盘价(隔夜收益=1)。
    """
    n = len(dates_list)
    ratios = [g / d for g, d in zip(growth_closes, div_closes)]
    mas = calc_ma(ratios, MA_WINDOW)
    signals = calc_ma_band_signal(ratios, mas, MA_BAND)
    sig_by_date: dict = {}
    valid_start = len(ratios) - len(signals)
    for j, s in enumerate(signals):
        sig_by_date[dates_list[valid_start + j]] = s["state"]

    def _open(opens, i, closes):
        o = opens[i] if i < len(opens) and opens[i] is not None else None
        return o if o is not None else closes[i]

    # 第一个有效信号日 → 次日开盘建仓
    first_idx = next((i for i in range(n) if sig_by_date.get(dates_list[i]) is not None), None)
    if first_idx is None or first_idx + 1 >= n:
        return [1.0] * n, [None] * n, [1.0] * n, [1.0] * n
    trade_date = first_idx + 1
    init = sig_by_date[dates_list[first_idx]]

    g_open = _open(growth_opens, trade_date, growth_closes)
    g_close = growth_closes[trade_date]
    d_open = _open(div_opens, trade_date, div_closes)
    d_close = div_closes[trade_date]
    if init == 1:
        nav0 = (g_close / g_open) * (1 - ONE_WAY_FEE)
        growth_nav0, div_nav0 = nav0, 1.0
    else:
        nav0 = (d_close / d_open) * (1 - ONE_WAY_FEE)
        div_nav0, growth_nav0 = nav0, 1.0

    nav = [1.0] * n
    div_nav = [1.0] * n
    growth_nav = [1.0] * n
    positions = [None] * n
    nav[trade_date] = nav0
    div_nav[trade_date] = div_nav0
    growth_nav[trade_date] = growth_nav0
    positions[trade_date] = init
    pos = init

    for i in range(trade_date + 1, n):
        sig_prev = sig_by_date.get(dates_list[i - 1])
        if sig_prev is None:
            sig_prev = pos

        go = _open(growth_opens, i, growth_closes)
        gc = growth_closes[i]
        do = _open(div_opens, i, div_closes)
        dc = div_closes[i]
        gcp = growth_closes[i - 1]
        dcp = div_closes[i - 1]

        # 隔夜腿 (前一日仓位): close[T-1] -> open[T]
        if pos == 1:
            ov_g, ov_d = go / gcp, 1.0
        else:
            ov_d, ov_g = do / dcp, 1.0
        # 日内腿 (切换后仓位): open[T] -> close[T]
        switched = (sig_prev != pos)
        if sig_prev == 1:
            id_g = ((1 - ONE_WAY_FEE) * (gc / go)) if switched else (gc / go)
            id_d = 1.0
        else:
            id_d = ((1 - ONE_WAY_FEE) * (dc / do)) if switched else (dc / do)
            id_g = 1.0

        nav[i] = nav[i - 1] * ov_g * ov_d * id_g * id_d
        growth_nav[i] = growth_nav[i - 1] * ov_g * id_g
        div_nav[i] = div_nav[i - 1] * ov_d * id_d
        positions[i] = sig_prev
        pos = sig_prev

    return nav, positions, div_nav, growth_nav


def compute_annual_returns(dates: list, nav: list) -> list:
    """计算策略净值在每一自然年的涨幅。

    nav[i] 对应 dates[i] 收盘后的净值（开盘价执行，nav[0]=1.0 基准）。
    对每自然年 Y，取该年最后一个交易日的 nav 作为年末值，取上一年最后一个
    交易日的 nav 作为年初值；首年年初 = nav[0]（=1.0，资金起点）。
    返回 [(year, ret), ...]，ret 为小数（+0.231 表示 +23.1%）。
    注意: 若数据起点非 1 月 1 日（如 2010-07 起），首年涨幅实为部分年涨幅；
    若数据截止非 12 月 31 日（如年中），末年涨幅为年内至今涨幅。
    """
    if not dates or not nav:
        return []
    year_end_idx = {}
    for i, d in enumerate(dates):
        year_end_idx[d.year] = i  # 覆盖为当年最后一个交易日的索引
    out = []
    prev_nav = None
    for y in sorted(year_end_idx.keys()):
        end_nav = nav[year_end_idx[y]]
        start_nav = nav[0] if prev_nav is None else prev_nav
        out.append((y, end_nav / start_nav - 1.0))
        prev_nav = end_nav
    return out


def compute_buyhold_nav(opens: list) -> list:
    """满仓持有(只买成长)净值: 每日用开盘价比值累乘, nav[0]=1.0。

    用于年度表 '只买成长' 对比列——把红利视为风险指标, 全程只持成长, 不做切换。
    opens 为与日期对齐的列表, 缺失(None)时当日收益记为 0(净值不变)。
    """
    nav = [1.0]
    for i in range(1, len(opens)):
        o0, o1 = opens[i - 1], opens[i]
        if o0 and o1 and o0 > 0:
            nav.append(nav[-1] * (o1 / o0))
        else:
            nav.append(nav[-1])
    return nav


# ─── 主分析 ────────────────────────────────────────────

def analyze(growth_key: str) -> dict:
    """分析给定成长指数的缓冲均线信号"""
    growth_info = INDICES[growth_key]
    div_info = INDICES["dividend"]

    print(f"\n{'='*50}")
    print(f"获取 {growth_info['name']}({growth_info['code']}) 数据...")
    growth_data = get_index_data(growth_info)

    print(f"获取 {div_info['name']}({div_info['code']}) 数据...")
    div_data = get_index_data(div_info)

    if growth_data is None or div_data is None:
        return {"error": "数据获取失败，请检查网络连接"}

    dates, growth_closes, div_closes = align_data(growth_data, div_data)

    if len(growth_closes) < MA_WINDOW:
        return {"error": f"共同交易日不足 {MA_WINDOW} 天 (只有 {len(growth_closes)} 天)"}

    # 计算比值和均线
    ratios = [g / d for g, d in zip(growth_closes, div_closes)]
    mas = calc_ma(ratios, MA_WINDOW)

    # 计算缓冲均线信号 (基于完整数据窗口重算，结果完全由数据决定，无外部状态依赖)
    all_signals = calc_ma_band_signal(ratios, mas, MA_BAND)

    # 关联日期
    valid_start = len(ratios) - len(all_signals)
    for j, s in enumerate(all_signals):
        s["date"] = dates[valid_start + j]
        s["growth_close"] = round(growth_closes[valid_start + j], 2)
        s["dividend_close"] = round(div_closes[valid_start + j], 2)

    # 最近10个信号
    recent = all_signals[-10:]

    current = recent[-1]

    # 检查是否有信号切换 (上轨/下轨 触发)
    signal_change = None
    if len(recent) >= 2:
        prev = recent[-2]
        if prev["signal"] != current["signal"] and current["zone"] != "缓冲区内(维持)":
            signal_change = {
                "from": prev["signal"], "to": current["signal"],
                "date": current["date"], "trigger": current["zone"],
            }

    result = {
        "index_pair": f"{growth_info['name']} vs {div_info['name']}",
        "growth_code": growth_info["code"],
        "dividend_code": div_info["code"],
        "strategy": f"缓冲均线策略 (带宽={MA_BAND*100:.1f}%, 均线={MA_WINDOW}日)",
        "current_signal": current["signal"],
        "current_zone": current["zone"],
        "current_ratio": current["ratio"],
        "current_ma": current["ma"],
        "current_upper": current["upper"],
        "current_lower": current["lower"],
        "latest_date": current["date"],
        "ratio_ma_diff": round(current["ratio"] - current["ma"], 4),
        "ratio_ma_diff_pct": round((current["ratio"] / current["ma"] - 1) * 100, 2),
        "distance_to_upper": round((current["ratio"] / current["upper"] - 1) * 100, 2),
        "distance_to_lower": round((current["ratio"] / current["lower"] - 1) * 100, 2),
        "signal_change": signal_change,
        "recent_10d": recent,
        "fetch_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 生成可视化图表 (长期趋势、短期细节)
    date_str = datetime.now().strftime("%Y%m%d")
    code = growth_info["code"]
    chart_paths = []

    # --- 短期细节图 (2个月) — 先放上面，重点 ---
    chart_short = str(CACHE_DIR / f"chart_{code}_short_{date_str}.png")
    try:
        plot_chart_short(all_signals, result, chart_short)
        chart_paths.append(("short", chart_short))
    except Exception as e:
        print(f"  短期图表生成失败: {e}")

    # --- 长期趋势图 (3年) — 放下面，参考 ---
    chart_long = str(CACHE_DIR / f"chart_{code}_long_{date_str}.png")
    try:
        plot_chart_long(all_signals, result, chart_long)
        chart_paths.append(("long", chart_long))
    except Exception as e:
        print(f"  长期图表生成失败: {e}")

    result["chart_paths"] = chart_paths
    return result


# ─── 报告格式化 ────────────────────────────────────────

def format_report(result: dict) -> str:
    """格式化输出报告"""
    if "error" in result:
        return f"ERROR: {result['error']}"

    signal = result["current_signal"]
    emoji = ">>" if signal == "成长" else "<<"
    zone = result["current_zone"]
    diff_pct = result["ratio_ma_diff_pct"]
    direction = "上方" if diff_pct > 0 else "下方"

    lines = [
        "=" * 62,
        f"  成长/红利风格轮动 — {result['strategy']}",
        "=" * 62,
        f"  指数对:    {result['index_pair']}",
        f"  数据时间:  {result['fetch_time']}",
        f"  最新交易日:{result['recent_10d'][-1]['date']}",
        "",
        f"  | 当前信号:  {emoji}  {signal.upper()}期",
        f"  | 触发区域:  {zone}",
        "",
        f"  比值 (成长/红利): {result['current_ratio']:.4f}",
        f"  20日均线:          {result['current_ma']:.4f}",
        f"  上轨 (均线+1%):     {result['current_upper']:.4f}",
        f"  下轨 (均线-1%):     {result['current_lower']:.4f}",
        f"  与均线偏离:        {diff_pct:+.2f}%  (比值在均线{direction})",
        f"  距上轨:            {result['distance_to_upper']:+.2f}%",
        f"  距下轨:            {result['distance_to_lower']:+.2f}%",
    ]

    if result["signal_change"]:
        sc = result["signal_change"]
        lines.append(f"\n  ⚡ 信号切换: {sc['from']} -> {sc['to']} ({sc['date']})  触发:{sc['trigger']}")

    lines.append(f"\n  最近走势:")
    lines.append(f"  {'日期':<12} {'成长':>10} {'红利':>10} {'比值':>8} {'均线':>8} {'信号':>6} {'区域'}")
    lines.append(f"  {'-'*70}")
    for r in result["recent_10d"]:
        tag = "<-" if r == result["recent_10d"][-1] else "  "
        lines.append(
            f"{tag}{r['date']:<11} {r['growth_close']:>10.2f} {r['dividend_close']:>10.2f} "
            f"{r['ratio']:>8.4f} {r['ma']:>8.4f} {r['signal']:>6} {r['zone']}"
        )

    lines.append(f"\n  规则:")
    lines.append(f"    比值 >= 均线×1.01 (上轨) → 持成长")
    lines.append(f"    比值 <= 均线×0.99 (下轨) → 持红利")
    lines.append(f"    上下轨之间 → 维持原仓位 (避免频繁换仓)")
    if result.get("chart_paths"):
        lines.append(f"\n  📊 趋势图:")
        for ctype, cpath in result["chart_paths"]:
            label = {"long": "长期(3年)", "short": "短期(2月)"}
            lines.append(f"     {label.get(ctype, ctype)}: {cpath}")
    lines.append(f"\n  仅供研究参考, 不构成投资建议")
    lines.append("=" * 62)
    return "\n".join(lines)


def format_brief(result: dict, basket_info: dict | None = None) -> str:
    """简洁版报告 (用于消息推送/保存)"""
    if "error" in result:
        return f"[轮动监控] 错误: {result['error']}"

    signal = result["current_signal"]
    emoji = "📈" if signal == "成长" else "💰"
    diff_pct = result["ratio_ma_diff_pct"]

    lines = [
        f"{emoji} 创红方案每日监控 {result['index_pair']}",
        f"信号: {signal}期",
        f"区域: {result['current_zone']}",
        f"比值: {result['current_ratio']:.4f} | 均线: {result['current_ma']:.4f}",
        f"偏离: {diff_pct:+.2f}%",
        f"距上轨: {result['distance_to_upper']:+.2f}% | 距下轨: {result['distance_to_lower']:+.2f}%",
    ]

    if result["signal_change"]:
        sc = result["signal_change"]
        lines.insert(2, f"⚡ 切换: {sc['from']}→{sc['to']} 触发:{sc['trigger']}")

    lines.append(f"\n{result['fetch_time']} | 策略: {result['strategy']}")
    if result.get("chart_paths"):
        lines.append(f"📊 图表: {len(result['chart_paths'])} 张 (长期/短期)")

    # 创红方案篮子信息 (资讯联动)
    if basket_info:
        top3 = basket_info["top3"]
        names = "、".join(t["name"] for t in top3)
        lines.append(f"\n🧺 当前篮子: {names}（创业板50 TOP3）")
        lines.append(f"   下次调仓: {basket_info['next_rebalance']}（剩 {basket_info['days_left']} 天）")
        lines.append(f"   数据源: {basket_info['source']}{basket_info['note']}")
    return "\n".join(lines)


# ─── 可视化图表 (长期 / 短期 / 累计收益) ──────────────────

CHART_LONG_DAYS = 750    # 长期图: 约3年交易日，把握大趋势
CHART_SHORT_DAYS = 45    # 短期图: 约2个月交易日，观察近期拐点


def setup_chinese_font():
    """配置中文字体 —— 直接按文件路径查找, 不依赖 matplotlib 字体名解析

    策略: 找到字体 .ttc/.ttf 物理文件 → FontProperties(fname=路径) 提取真名
    → 直接设为 font.family (跳过 sans-serif 回退链, 避免 DejaVu Sans 拦截 CJK)
    """
    import matplotlib
    import matplotlib.font_manager as fm
    import glob as _glob

    font_file = None

    # ── 1. Linux / GitHub Actions 字体路径 ──
    linux_bases = ["/usr/share/fonts", "/usr/local/share/fonts"]
    linux_patterns = [
        "NotoSans*CJK*.ttc", "NotoSans*CJK*.ttf",
        "NotoSans*SC*.ttc", "NotoSans*SC*.ttf",
        "wqy*.ttc", "wqy*.ttf",
        "WenQuanYi*.ttc", "WenQuanYi*.ttf",
    ]
    for base in linux_bases:
        if not os.path.exists(base):
            continue
        for pattern in linux_patterns:
            found = sorted(_glob.glob(os.path.join(base, "**", pattern), recursive=True))
            if found:
                font_file = found[0]
                break
        if font_file:
            break

    # ── 2. Windows 字体路径 ──
    if not font_file and os.name == "nt":
        win_font_dir = os.environ.get("WINDIR", r"C:\Windows") + r"\Fonts"
        win_candidates = [
            "msyh.ttc",       # 微软雅黑
            "msyhbd.ttc",     # 微软雅黑粗体
            "simhei.ttf",     # 黑体
            "simsun.ttc",     # 宋体
            "simkai.ttf",     # 楷体
            "msjh.ttc",       # 微软正黑体
            "msjhbd.ttc",
        ]
        for cand in win_candidates:
            path = os.path.join(win_font_dir, cand)
            if os.path.exists(path):
                font_file = path
                break
        # 也尝试通过 matplotlib fontManager 搜索 Windows 已注册字体
        if not font_file:
            try:
                for cand_name in ["Microsoft YaHei", "SimHei", "SimSun", "NSimSun"]:
                    try:
                        fp = fm.FontProperties(family=cand_name)
                        path = fm.findfont(fp)
                        if path and "DejaVu" not in path and os.path.exists(path):
                            font_file = path
                            break
                    except Exception:
                        continue
            except Exception:
                pass

    if not font_file:
        print("  [字体] ❌ 未找到 CJK 字体文件, 中文将显示方框", file=sys.stderr)
        return "sans-serif"

    print(f"  [字体] 找到字体文件: {font_file}", file=sys.stderr)

    # ── 核心: 用 FontProperties(fname=) 直取字体真名 ──
    try:
        fp = fm.FontProperties(fname=font_file)
        font_name = fp.get_name()
        print(f"  [字体] FontProperties 解析 → 真实名称: '{font_name}'", file=sys.stderr)
    except Exception as e:
        print(f"  [字体] FontProperties 失败: {e}, 回退", file=sys.stderr)
        return "sans-serif"

    # 注册到 fontManager + 清除缓存以防万一
    try:
        fm.fontManager.addfont(font_file)
        print(f"  [字体] 已注册到 fontManager", file=sys.stderr)
    except Exception:
        pass

    # 清理缓存
    cache_dir = matplotlib.get_cachedir()
    for pat in ["fontlist*.json", "fontlist-v*.json"]:
        for cf in _glob.glob(os.path.join(cache_dir, pat)):
            try:
                os.remove(cf)
            except Exception:
                pass

    return font_name


def _setup_dark_style():
    """统一深色主题配置"""
    import matplotlib.pyplot as plt
    font_name = setup_chinese_font()

    # 抑制 matplotlib 启动期噪音(font cache 构建/字体查找 DEBUG/INFO 日志),
    # 减少每日运行日志体积, 让输出更干净、token 消耗更低
    try:
        import logging as _logging
        import matplotlib as _mpl
        _logging.getLogger("matplotlib").setLevel(_logging.ERROR)
        _mpl.set_loglevel("ERROR")
    except Exception:
        pass

    # ── 关键: font.family 直接设为 CJK 字体名, 不走 sans-serif 回退链 ──
    # 原因: font.family='sans-serif' → DejaVu Sans 在回退链最前会拦截渲染
    #   → CJK 字符缺字时 matplotlib 某些版本不自动回退 → 方框/乱码
    # 解决: font.family 设为 CJK 字体真实名称, 它本身包含拉丁字符 → 全正常
    if font_name != "sans-serif":
        plt.rcParams["font.family"] = font_name
    else:
        # Linux 常见 CJK 字体名称 (最后的回退)
        for fallback in ["Noto Sans CJK SC", "WenQuanYi Micro Hei",
                          "Noto Sans CJK TC", "Noto Sans CJK"]:
            from matplotlib.font_manager import findfont
            try:
                findfont(fallback, fallback_to_default=False)
                plt.rcParams["font.family"] = fallback
                break
            except Exception:
                continue

    plt.rcParams["font.size"] = 14
    plt.rcParams["axes.labelsize"] = 14
    plt.rcParams["xtick.labelsize"] = 12
    plt.rcParams["ytick.labelsize"] = 12
    plt.rcParams["legend.fontsize"] = 13
    plt.rcParams["axes.titlesize"] = 16
    plt.rcParams["axes.unicode_minus"] = False
    print(f"  [字体] rcParams['font.family'] = '{plt.rcParams['font.family']}'", file=sys.stderr)


def _style_ax(ax, ylabel: str = ""):
    """统一坐标轴美化"""
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.spines["left"].set_color("#30363d")
    ax.spines["bottom"].set_color("#30363d")
    ax.tick_params(colors="#8b949e", labelsize=12)
    ax.grid(True, alpha=0.12, color="#8b949e")
    if ylabel:
        ax.set_ylabel(ylabel, color="#8b949e", fontsize=13)


def _draw_signal_zones(ax, dates, sig_labels, alpha_growth=0.06, alpha_div=0.04):
    """绘制成长期/红利期背景着色"""
    in_growth = False
    zone_start = 0
    for i in range(len(dates)):
        is_growth = sig_labels[i] == "成长"
        if i == 0:
            in_growth = is_growth
            zone_start = 0
        elif is_growth != in_growth:
            if in_growth:
                ax.axvspan(dates[zone_start], dates[i], alpha=alpha_growth, color="#1a3a6e")
            else:
                ax.axvspan(dates[zone_start], dates[i], alpha=alpha_div, color="#3d1a1a")
            in_growth = is_growth
            zone_start = i
    if in_growth:
        ax.axvspan(dates[zone_start], dates[-1], alpha=alpha_growth, color="#1a3a6e")
    else:
        ax.axvspan(dates[zone_start], dates[-1], alpha=alpha_div, color="#3d1a1a")


def _add_signal_markers(ax, dates, ratios, sig_labels, zones, show_labels=True):
    """标注信号切换点"""
    for i in range(1, len(dates)):
        if zones[i] != "缓冲区内(维持)" and sig_labels[i] != sig_labels[i - 1]:
            color = "#58a6ff" if sig_labels[i] == "成长" else "#ff4444"
            marker = "▲" if sig_labels[i] == "成长" else "▼"
            ax.scatter(dates[i], ratios[i], color=color, s=50, zorder=10,
                       edgecolors="white", linewidths=0.8)
            if show_labels:
                ax.annotate(marker, (dates[i], ratios[i]),
                            textcoords="offset points",
                            xytext=(0, 15 if sig_labels[i] == "成长" else -18),
                            fontsize=10, color=color, ha="center", fontweight="bold")


def _add_info_box(ax, result: dict):
    """添加信号信息框"""
    info = (
        f"信号: {result['current_signal']}期\n"
        f"比值: {result['current_ratio']:.4f} | 均线: {result['current_ma']:.4f}\n"
        f"偏离: {result['ratio_ma_diff_pct']:+.2f}% | "
        f"距上轨: {result['distance_to_upper']:+.2f}% | 距下轨: {result['distance_to_lower']:+.2f}%"
    )
    ax.text(0.02, 0.97, info, transform=ax.transAxes,
            fontsize=12, color="#8b949e", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.9))


def _add_legend(ax, lines: list):
    """添加图例 (深色主题) — 图表下方两列，永不遮挡曲线"""
    legend = ax.legend(lines, [l.get_label() for l in lines],
                       loc="upper center", bbox_to_anchor=(0.5, -0.10),
                       framealpha=0.85, fontsize=11, ncol=2,
                       facecolor="#161b22", edgecolor="#30363d",
                       labelcolor="#e6edf3")
    for text in legend.get_texts():
        text.set_color("#e6edf3")


# ── 图表 1: 长期趋势 (约1年) ──────────────────────────────

def plot_chart_long(signals: list[dict], result: dict, save_path: str):
    """长期趋势图 — 近3年的比值+均线+上下轨走势，把握大趋势"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime as dt

    _setup_dark_style()

    display = signals[-CHART_LONG_DAYS:] if len(signals) > CHART_LONG_DAYS else signals
    dates = [dt.strptime(s["date"], "%Y-%m-%d") for s in display]
    ratios = [s["ratio"] for s in display]
    mas = [s["ma"] for s in display]
    uppers = [s["upper"] for s in display]
    lowers = [s["lower"] for s in display]
    sig_labels = [s["signal"] for s in display]
    zones = [s["zone"] for s in display]

    fig, ax = plt.subplots(figsize=(18, 7))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    _draw_signal_zones(ax, dates, sig_labels, alpha_growth=0.05, alpha_div=0.03)

    l1 = ax.plot(dates, uppers, color="#58a6ff", linewidth=0.8, linestyle="--",
                 alpha=0.6, label="上轨(MAx1.01)")[0]
    l2 = ax.plot(dates, lowers, color="#ff6b6b", linewidth=0.8, linestyle="--",
                 alpha=0.6, label="下轨(MAx0.99)")[0]
    l3 = ax.plot(dates, mas, color="#f08d49", linewidth=1.5, alpha=0.85,
                 label="20日均线")[0]
    l4 = ax.plot(dates, ratios, color="#58a6ff", linewidth=1.8, alpha=1.0,
                 label="成长/红利 比值")[0]

    _add_signal_markers(ax, dates, ratios, sig_labels, zones, show_labels=False)

    # 最新信号点
    current_signal = sig_labels[-1]
    signal_color = "#58a6ff" if current_signal == "成长" else "#ff4444"
    ax.scatter(dates[-1], ratios[-1], color=signal_color, s=120, zorder=15,
               edgecolors="white", linewidths=1.5)
    ax.annotate(f"  {current_signal}期", (dates[-1], ratios[-1]),
                textcoords="offset points", xytext=(8, 0),
                fontsize=10, color=signal_color, fontweight="bold", va="center")

    _add_info_box(ax, result)
    _style_ax(ax, ylabel="成长/红利 比值")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y/%m"))
    ax.xaxis.set_major_locator(mdates.MonthLocator(interval=2))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.4f}"))
    fig.autofmt_xdate(rotation=30)

    plt.suptitle(f"{result['index_pair']} — 长期趋势 (近3年)",
                 fontsize=20, color="#e6edf3", fontweight="bold", y=0.98)
    _add_legend(ax, [l4, l3, l1, l2])

    fig.tight_layout(rect=[0, 0.12, 1, 0.93])
    fig.savefig(save_path, dpi=150, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)


# ── 图表 2: 短期细节 (约2个月) ──────────────────────────

def plot_chart_short(signals: list[dict], result: dict, save_path: str):
    """短期细节图 — 近2个月的比值走势+精确信号标注，聚焦短期拐点"""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime as dt

    _setup_dark_style()

    display = signals[-CHART_SHORT_DAYS:] if len(signals) > CHART_SHORT_DAYS else signals
    dates = [dt.strptime(s["date"], "%Y-%m-%d") for s in display]
    ratios = [s["ratio"] for s in display]
    mas = [s["ma"] for s in display]
    uppers = [s["upper"] for s in display]
    lowers = [s["lower"] for s in display]
    sig_labels = [s["signal"] for s in display]
    zones = [s["zone"] for s in display]

    fig, ax = plt.subplots(figsize=(16, 7))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    _draw_signal_zones(ax, dates, sig_labels, alpha_growth=0.08, alpha_div=0.06)

    l1 = ax.plot(dates, uppers, color="#58a6ff", linewidth=1.0, linestyle="--",
                 alpha=0.7, label="上轨(MAx1.01)")[0]
    l2 = ax.plot(dates, lowers, color="#ff6b6b", linewidth=1.0, linestyle="--",
                 alpha=0.7, label="下轨(MAx0.99)")[0]
    l3 = ax.plot(dates, mas, color="#f08d49", linewidth=1.8, alpha=0.9,
                 label="20日均线")[0]
    l4 = ax.plot(dates, ratios, color="#e6edf3", linewidth=2.5, alpha=1.0,
                 marker="o", markersize=3, markerfacecolor="#e6edf3",
                 label="成长/红利 比值")[0]

    _add_signal_markers(ax, dates, ratios, sig_labels, zones, show_labels=True)

    current_signal = sig_labels[-1]
    signal_color = "#58a6ff" if current_signal == "成长" else "#ff4444"
    ax.scatter(dates[-1], ratios[-1], color=signal_color, s=180, zorder=15,
               edgecolors="white", linewidths=2)
    ax.annotate(f"<- {current_signal}期 ({result['current_zone']})",
                (dates[-1], ratios[-1]),
                textcoords="offset points", xytext=(12, 0),
                fontsize=10, color=signal_color, fontweight="bold", va="center")

    _add_info_box(ax, result)
    _style_ax(ax, ylabel="成长/红利 比值")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    ax.xaxis.set_major_locator(mdates.WeekdayLocator(interval=2))
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.4f}"))
    fig.autofmt_xdate(rotation=30)

    plt.suptitle(f"{result['index_pair']} — 短期细节 (近2月)",
                 fontsize=20, color="#e6edf3", fontweight="bold", y=0.98)
    _add_legend(ax, [l4, l3, l1, l2])

    fig.tight_layout(rect=[0, 0.12, 1, 0.93])
    fig.savefig(save_path, dpi=150, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)


# ── 图表 3: 合并累计收益曲线 (5条曲线) ──────────────────

def _add_position_markers(ax, dates, nav_values, positions, marker_size=95):
    """在策略净值曲线上叠加持仓标记点 — 只在切换点标记
    - 红利 (position=0): 红色圆点  ●
    - 成长 (position=1): 蓝色三角 ▲
    - 仅在持仓发生变化时标记，自然交替出现
    """
    prev_pos = None
    for i in range(1, len(dates)):
        pos = positions[i]
        if pos is None:
            continue
        if pos != prev_pos:
            # 切换点：标记
            color = "#ff6b6b" if pos == 0 else "#58a6ff"
            marker = "o" if pos == 0 else "^"
            ax.scatter(dates[i], nav_values[i], color=color, marker=marker,
                       s=marker_size, zorder=10, alpha=0.95,
                       edgecolors="white", linewidths=1.0)
        prev_pos = pos


def _merge_price_maps(csv_rows, api_rows):
    """CSV长数据 + API最新数据合并, CSV优先(覆盖更早日期)。
    返回 (close_map, open_map); open 缺失记为 None(策略计算时回退收盘价)。"""
    close_map, open_map = {}, {}
    if csv_rows:
        for r in csv_rows:
            close_map[r["date"]] = r["close"]
            if r.get("open") is not None:
                open_map[r["date"]] = r["open"]
    if api_rows:
        for r in api_rows:
            if r["date"] not in close_map:
                close_map[r["date"]] = r["close"]
            if r["date"] not in open_map and r.get("open") is not None:
                open_map[r["date"]] = r["open"]
    return close_map, open_map


def prepare_backtest_data() -> dict | None:
    """一次性加载并合并长数据 + 腾讯API最新日, 计算创业板指/红利策略 NAV/信号/逐年收益。

    供 plot_combined_returns_chart / plot_growth_only_curve_chart 共用,
    消除原设计中创业板对 NAV 被算两遍、API 被调 6 次的冗余。
    返回 None 表示数据不全, 调用方应跳过累计收益相关图。
    """
    from datetime import datetime as dt

    div_info = INDICES["dividend"]
    cyb_info = INDICES["growth_cyb"]

    csv_data = load_long_csv_data()
    api_div = get_index_data(div_info)
    api_cyb = get_index_data(cyb_info)
    if not api_div or not api_cyb:
        print("  [数据] 腾讯API获取不全, 跳过往期收益图")
        return None

    div_map, div_open = _merge_price_maps(csv_data["000922"] if csv_data else None, api_div)
    cyb_map, cyb_open = _merge_price_maps(csv_data["399006"] if csv_data else None, api_cyb)

    cyb_div_dates = sorted(set(div_map.keys()) & set(cyb_map.keys()))
    if len(cyb_div_dates) < MA_WINDOW + 2:
        print(f"  [数据] 创板+红利交易日不足 ({len(cyb_div_dates)}天)")
        return None

    # 创业板指 vs 红利
    cyb_strat_nav, cyb_strat_pos, cyb_div_nav, cyb_growth_nav = calc_strategy_returns(
        [cyb_map[d] for d in cyb_div_dates], [div_map[d] for d in cyb_div_dates],
        [cyb_open.get(d) for d in cyb_div_dates], [div_open.get(d) for d in cyb_div_dates],
        cyb_div_dates)
    cyb_strat_dates = [dt.strptime(d, "%Y-%m-%d") for d in cyb_div_dates[-len(cyb_strat_nav):]]
    cyb_annual = compute_annual_returns(cyb_strat_dates, cyb_strat_nav)
    cyb_go_annual = compute_annual_returns(cyb_strat_dates, cyb_growth_nav)

    return {
        "div_map": div_map, "div_open": div_open,
        "cyb_map": cyb_map, "cyb_open": cyb_open,
        "cyb_div_dates": cyb_div_dates,
        "cyb_strat_nav": cyb_strat_nav, "cyb_strat_pos": cyb_strat_pos,
        "cyb_div_nav": cyb_div_nav, "cyb_growth_nav": cyb_growth_nav,
        "cyb_strat_dates": cyb_strat_dates, "cyb_annual": cyb_annual, "cyb_go_annual": cyb_go_annual,
    }


def plot_combined_returns_chart(prep: dict, results: list[dict], save_path: str):
    """合并收益对比图 — 归一化收益曲线

    曲线:
      1. 一直持有中证红利 (红利基准)
      2. 一直持有创业板指 (成长)
      3. 策略轮动(创业板指 vs 红利)
      4. 策略-红利贡献 / 5. 策略-成长贡献 (收益分解)

    策略轮动逻辑:
      - Day T 收盘生成信号 → Day T+1 开盘执行 (避免前视偏差)
      - 信号='成长'时持有成长指数, 信号='红利'时持有红利指数
      - 扣除单边交易费 0.1%
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from datetime import datetime as dt

    _setup_dark_style()

    # ── 从预计算数据包读取 (main() 已一次性加载并计算, 此处不再重复) ──
    cyb_div_dates = prep["cyb_div_dates"]
    cyb_strat_nav, cyb_strat_pos = prep["cyb_strat_nav"], prep["cyb_strat_pos"]
    cyb_div_nav, cyb_growth_nav = prep["cyb_div_nav"], prep["cyb_growth_nav"]
    cyb_strat_dates = prep["cyb_strat_dates"]
    cyb_annual, cyb_go_annual = prep["cyb_annual"], prep["cyb_go_annual"]

    # Buy & Hold 归一化曲线 (开盘价口径, 与策略执行/年度表一致)
    div_open, div_map = prep["div_open"], prep["div_map"]
    cyb_open, cyb_map = prep["cyb_open"], prep["cyb_map"]
    div_bh_dt = [dt.strptime(d, "%Y-%m-%d") for d in cyb_div_dates]
    div_bh_norm = compute_buyhold_nav([div_open.get(d) for d in cyb_div_dates])
    cyb_bh_norm = compute_buyhold_nav([cyb_open.get(d) for d in cyb_div_dates])

    # ── 绘图 ──
    fig, ax = plt.subplots(figsize=(20, 8))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    # 曲线 — 各自从最早可用日期开始
    l_div = ax.plot(div_bh_dt, div_bh_norm, color="#ff6b6b", linewidth=1.8, alpha=0.9,
                     label="中证红利 Buy&Hold")[0]
    l_cyb = ax.plot(div_bh_dt, cyb_bh_norm, color="#58a6ff", linewidth=1.5, alpha=0.8, linestyle="--",
                     label="创业板指 Buy&Hold")[0]

    l_cyb_s = ax.plot(cyb_strat_dates, cyb_strat_nav, color="#ffa94d", linewidth=2.5, alpha=1.0,
                       label="策略轮动(创业板指)")[0]

    # ── 策略收益分解: 红利贡献 & 成长贡献 (创业板策略) ──
    l_div_part = ax.plot(cyb_strat_dates, cyb_div_nav, color="#ff6b6b", linewidth=1.4, alpha=0.7, linestyle=":",
                          label="策略-红利贡献")[0]
    l_growth_part = ax.plot(cyb_strat_dates, cyb_growth_nav, color="#58a6ff", linewidth=1.4, alpha=0.7, linestyle=":",
                             label="策略-成长贡献")[0]

    # ── 策略轮动持仓标记 (红=红利, 蓝=成长) ──
    _add_position_markers(ax, cyb_strat_dates, cyb_strat_nav, cyb_strat_pos, marker_size=95)

    # 基准线 1.0
    ax.axhline(y=1.0, color="#8b949e", linewidth=0.6, linestyle=":", alpha=0.5)

    # ── 信息框 (左上角) ──
    cyb_result = next((r for r in results if "创业板" in r.get("index_pair", "")), {})

    info_lines = [
        f"红利/创业板: {cyb_div_dates[0]} ~ {cyb_div_dates[-1]} ({len(cyb_div_dates)}天)",
        f"策略: 缓冲均线 (带宽=1%, MA=20日, 单边费=0.1%)",
        f"当前信号: 创→{cyb_result.get('current_signal','?')}期",
    ]
    info_text = "\n".join(info_lines)
    ax.text(0.02, 0.97, info_text, transform=ax.transAxes,
            fontsize=12, color="#8b949e", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.9))

    # ── 策略收益分解 (放在信息框下方) ──
    # 恒等式守卫: 策略终值 = 红利贡献 × 成长贡献 (误差应≈0)。
    # 若恒等式不成立，说明收益分解计算异常 —— 宁可不出累计收益图，
    # 也绝不推送错误的拆分数字 (涉及真金白银)。
    if abs(cyb_strat_nav[-1] - cyb_div_nav[-1] * cyb_growth_nav[-1]) >= 1e-6:
        print(f"  [累计收益图] ⚠️ 严重: 策略收益分解恒等式不成立 "
              f"({cyb_strat_nav[-1]} vs {cyb_div_nav[-1]*cyb_growth_nav[-1]})，"
              f"跳过累计收益图以避免推送错误数字")
        return None
    div_pct = (cyb_div_nav[-1] - 1) * 100
    growth_pct = (cyb_growth_nav[-1] - 1) * 100
    strat_pct = (cyb_strat_nav[-1] - 1) * 100
    contribution_lines = [
        f"策略收益分解 (创业板, 开盘价无前视偏差):",
        f"  策略总收益: {cyb_strat_nav[-1]:.2f}x ({strat_pct:+.0f}%)",
        f"  红利期贡献: {cyb_div_nav[-1]:.2f}x ({div_pct:+.0f}%)  ← 负=避险舱",
        f"  成长期贡献: {cyb_growth_nav[-1]:.2f}x ({growth_pct:+.0f}%)",
    ]
    contribution_text = "\n".join(contribution_lines)
    ax.text(0.02, 0.74, contribution_text, transform=ax.transAxes,
            fontsize=11, color="#e6edf3", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.92))

    # ── 最终收益排行 (放在左侧信息框下方, 不遮挡曲线) ──
    rankings = [
        ("中证红利 Buy&Hold", div_bh_norm[-1], "#ff6b6b"),
        ("创业板指 Buy&Hold", cyb_bh_norm[-1], "#58a6ff"),
        ("策略轮动(创业板指)", cyb_strat_nav[-1], "#ffa94d"),
    ]
    rankings.sort(key=lambda x: x[1], reverse=True)  # 收益从高到低

    rank_lines = ["最终收益排行 (投入1元 → ?)"]
    medal = ["1st", "2nd", "3rd", "4th", "5th"]
    for i, (name, val, color) in enumerate(rankings):
        pct = (val - 1) * 100
        rank_lines.append(f"{medal[i]} {name}: {val:.2f}元 (+{pct:+.0f}%)")

    rank_text = "\n".join(rank_lines)
    ax.text(0.02, 0.50, rank_text, transform=ax.transAxes,
            fontsize=11, color="#e6edf3", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.92))

    _style_ax(ax, ylabel="归一化收益 (起点=1.0)")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.xaxis.set_major_locator(mdates.YearLocator())
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda v, _: f"{v:.2f}x"))
    fig.autofmt_xdate(rotation=30)

    data_start = cyb_div_dates[0]
    end_date_str = cyb_div_dates[-1]
    end_dt = dt.strptime(end_date_str, "%Y-%m-%d")
    start_dt = dt.strptime(data_start, "%Y-%m-%d")
    coverage_years = round((end_dt - start_dt).days / 365.25, 1)
    plt.suptitle(f"成长/红利 风格轮动 — 累计收益对比 (自 {data_start}, ~{coverage_years}年)",
                 fontsize=20, color="#e6edf3", fontweight="bold", y=0.98)

    # 图例 (放在图表下方, 永不遮挡曲线)
    legend = ax.legend(loc="upper center", bbox_to_anchor=(0.5, -0.10),
                       framealpha=0.88, fontsize=11, ncol=3,
                       facecolor="#161b22", edgecolor="#30363d",
                       labelcolor="#e6edf3")
    for text in legend.get_texts():
        text.set_color("#e6edf3")

    fig.tight_layout(rect=[0, 0.12, 1, 0.95])
    fig.savefig(save_path, dpi=150, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)
    print(f"  合并收益图: {save_path}")
    return save_path, cyb_annual, cyb_go_annual


def save_report(result: dict, report_text: str):
    """保存报告"""
    date_str = datetime.now().strftime("%Y%m%d")
    (CACHE_DIR / f"report_{date_str}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (CACHE_DIR / f"report_{date_str}.txt").write_text(report_text, encoding="utf-8")


def plot_growth_only_curve_chart(prep: dict, save_path: str) -> str | None:
    """生成『创业板只做成长』单图合成版: 左轴累计净值曲线 + 右轴逐年收益柱。

    口径: 信号说买成长 → 持成长; 说买红利 → 卖出成长、空仓(=1.0)。这正是策略的
    成长贡献分量 (calc_strategy_returns 返回的 growth_nav)。
    复用 calc_strategy_returns / compute_buyhold_nav / compute_annual_returns,
    口径与每日推送、年度表完全一致。
    """
    import datetime as _dt
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    from matplotlib.patches import Patch

    _setup_dark_style()

    # ── 从预计算数据包读取 (创业板对) ──
    cyb_div_dates = prep["cyb_div_dates"]
    cyb_open = prep["cyb_open"]
    growth_nav = prep["cyb_growth_nav"]
    div_nav = prep["cyb_div_nav"]
    positions = prep["cyb_strat_pos"]
    bh_nav = compute_buyhold_nav([cyb_open.get(d) for d in cyb_div_dates])
    dates = [_dt.datetime.strptime(d, "%Y-%m-%d") for d in cyb_div_dates]
    annual = compute_annual_returns(dates, growth_nav)

    final_g = growth_nav[-1]
    final_bh = bh_nav[-1]

    # ── 双Y轴: 左曲线 + 右逐年柱 ──
    fig, ax1 = plt.subplots(figsize=(18, 9))
    fig.patch.set_facecolor("#0d1117")
    ax1.set_facecolor("#0d1117")

    # 红利期(空仓)阴影
    spans, s, e = [], None, None
    for i, p in enumerate(positions):
        if p == 0:
            if s is None:
                s = i
            e = i
        elif s is not None:
            spans.append((s, e))
            s = None
    if s is not None:
        spans.append((s, e))
    for (s, e) in spans:
        ax1.axvspan(dates[s], dates[e], color="#ff6b6b", alpha=0.10, lw=0, zorder=0)

    ax1.axhline(1.0, color="#8b949e", linewidth=1, linestyle="--", alpha=0.6, zorder=2)
    ax1.plot(dates, bh_nav, color="#8b949e", linewidth=1.6, linestyle="--", alpha=0.8,
             zorder=3, label=f"满仓成长全程(不择时)  {final_bh:.2f}x")
    ax1.plot(dates, growth_nav, color="#58a6ff", linewidth=2.6, alpha=0.95,
             zorder=4, label=f"创业板只做成长(红利期空仓)  {final_g:.2f}x")

    ax1.annotate(f"{final_g:.2f}x\n(+{(final_g-1)*100:.0f}%)",
                 xy=(dates[-1], final_g), xytext=(dates[-1], final_g * 0.6),
                 color="#58a6ff", fontsize=14, fontweight="bold", ha="right", va="top", zorder=6,
                 arrowprops=dict(arrowstyle="->", color="#58a6ff", lw=1.5))

    ax1.set_ylabel("累计净值 (倍, 本金=1.0)", color="#e6edf3", fontsize=14)
    ax1.tick_params(colors="#e6edf3")
    ax1.set_ylim(0.9, max(final_g, final_bh) * 1.15)

    ax2 = ax1.twinx()
    ax2.set_facecolor("none")
    years = [y for y, r in annual]
    rets = [r for y, r in annual]
    bar_x = [_dt.datetime(y, 7, 1) for y in years]
    colors = ["#ff6b6b" if r >= 0 else "#2ea043" for r in rets]  # 涨红跌绿
    bars = ax2.bar(bar_x, [r * 100 for r in rets], width=_dt.timedelta(days=300),
                   color=colors, alpha=0.45, zorder=1)
    for bx, r in zip(bar_x, rets):
        v = r * 100
        ax2.text(bx, v + (3.0 if v >= 0 else -3.0), f"{v:+.1f}%",
                 ha="center", va="bottom" if v >= 0 else "top",
                 color="#e6edf3", fontsize=8.5, zorder=5)
    ax2.set_ylabel("单年收益 (%)", color="#e6edf3", fontsize=14)
    ax2.tick_params(colors="#e6edf3")
    maxabs = max(abs(r * 100) for r in rets) * 1.18
    ax2.set_ylim(-maxabs, maxabs)

    ax1.xaxis.set_major_locator(mdates.YearLocator(1))
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax1.grid(True, color="#21262d", linewidth=0.8, zorder=0)
    ax1.set_title("创业板『只做成长』累计净值 + 逐年收益 (2010 ~ 2026)",
                  color="#e6edf3", fontsize=18, fontweight="bold", pad=12)

    legend_handles = [
        Patch(color="#58a6ff", label=f"创业板只做成长  {final_g:.2f}x"),
        Patch(color="#8b949e", label=f"满仓成长全程(不择时)  {final_bh:.2f}x"),
        Patch(color="#ff6b6b", alpha=0.45, label="逐年收益(涨红跌绿, 右轴)"),
        Patch(color="#ff6b6b", alpha=0.10, label="红利期空仓避险"),
    ]
    ax1.legend(handles=legend_handles, loc="upper left", frameon=False, facecolor="none", fontsize=11)

    fig.text(0.5, 0.012,
             "蓝线=只做成长(信号买成长持成长、说买红利则空仓=1.0); 灰虚线=满仓成长不择时; "
             "柱=逐年单年收益(右轴, 涨红跌绿); 红色阴影=红利期空仓避险。数据: 创业板指 vs 中证红利, 开盘价执行, 无前视偏差。",
             ha="center", color="#8b949e", fontsize=10.5)

    plt.savefig(save_path, dpi=130, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)
    print(f"  已保存: {save_path}")
    return save_path


# ─── 创红方案计算 (篮子 × 信号) ─────────────────────────

def compute_combined_strategy() -> dict | None:
    """创红方案(创业板50 TOP3 篮子 × 红利信号)净值/逐年收益/逐年持仓。

    信号源: 创业板指(399006) vs 中证红利(000922) 缓冲均线 (与线上 24.06x 基线同口径)。
    资产: 成长期持当期创业板50 TOP3 等权, 红利期空仓 (regime B)。
    失败返回 None (降级, 不影响原报告)。
    """
    try:
        from datetime import datetime as dt
        from collections import defaultdict

        # 指数长数据: 腾讯 qfq 分块抓 2014-06 起 (不依赖 long_data, 自洽)
        cyb_recs = load_or_fetch_basket("sz", "399006", "qfq")
        div_recs = load_or_fetch_basket("sh", "000922", "qfq")
        if not cyb_recs or not div_recs:
            print("  [创红方案] 指数数据不足, 跳过")
            return None
        cyb_map = {r["date"]: r["close"] for r in cyb_recs}
        div_map = {r["date"]: r["close"] for r in div_recs}
        master_dates = sorted(set(cyb_map) & set(div_map))
        if len(master_dates) < MA_WINDOW + 2:
            print(f"  [创红方案] 共同交易日不足 ({len(master_dates)})")
            return None

        cyb_closes = [cyb_map[d] for d in master_dates]
        div_closes = [div_map[d] for d in master_dates]
        ratios = [g / d for g, d in zip(cyb_closes, div_closes)]
        mas = calc_ma(ratios, MA_WINDOW)
        sigs = calc_ma_band_signal(ratios, mas, MA_BAND)
        valid_start = len(ratios) - len(sigs)
        sig_by_date = {master_dates[valid_start + j]: s["state"] for j, s in enumerate(sigs)}

        # 个股 hfq (乐视退市特例 qfq)
        stock_open, stock_close = {}, {}
        for code in STOCK_CODES:
            adj = "qfq" if code == "300104" else "hfq"
            recs = load_or_fetch_basket("sz", code, adj)
            if not recs:
                print(f"  [创红方案] 个股 {code} 无数据, 跳过")
                return None
            o, c = align_to_master_basket(recs, master_dates, zero_after_last=True)
            stock_open[code] = o
            stock_close[code] = c

        nav, positions = simulate_enhanced(master_dates, sig_by_date,
                                           stock_open, stock_close, None, None, False)

        dates_dt = [dt.strptime(d, "%Y-%m-%d") for d in master_dates]
        combined_annual = compute_annual_returns(dates_dt, nav)

        # 逐年持仓: 成长期篮子集合 + 成长/空仓天数
        year_growth = defaultdict(int)
        year_empty = defaultdict(int)
        year_buckets = defaultdict(set)
        for i, d in enumerate(master_dates):
            if positions[i] is None:
                continue
            yr = int(d[:4])
            if positions[i] == 1:
                year_growth[yr] += 1
                year_buckets[yr].add(tuple(get_bucket_codes(d)))
            else:
                year_empty[yr] += 1

        def full_names(codes):
            return "/".join(STOCK_NAMES.get(c, c) for c in codes)

        combined_holdings = {}
        for yr, _ret in combined_annual:
            buckets = year_buckets.get(yr, set())
            gi = year_growth.get(yr, 0)
            ei = year_empty.get(yr, 0)
            # 多年份有多期篮子: 年度表只列代表篮子, 完整明细见"调仓历史"表
            if not buckets:
                combined_holdings[yr] = "空仓"
            elif len(buckets) == 1:
                btxt = full_names(list(buckets)[0])
                combined_holdings[yr] = f"{btxt}（含空仓）" if ei else btxt
            else:
                combined_holdings[yr] = f"{len(buckets)}期调仓·见历史表" + ("（含空仓）" if ei else "")

        # 指标
        mdd = 0.0
        peak = nav[0]
        for v in nav:
            if v > peak:
                peak = v
            dd = v / peak - 1
            if dd < mdd:
                mdd = dd
        start_dt = dates_dt[0]
        end_dt = dates_dt[-1]
        years = (end_dt - start_dt).days / 365.25
        total = nav[-1] / nav[0] - 1
        ann = (nav[-1] / nav[0]) ** (1 / years) - 1 if nav[0] > 0 else 0

        return {
            "nav": nav, "dates": master_dates, "positions": positions,
            "combined_annual": combined_annual, "combined_holdings": combined_holdings,
            "final_nav": nav[-1], "start_date": master_dates[0], "end_date": master_dates[-1],
            "max_dd": mdd, "total_ret": total, "ann_ret": ann,
        }
    except Exception as e:
        print(f"  [创红方案] 计算异常: {e}")
        import traceback
        traceback.print_exc()
        return None


# ─── 资讯联动: 国证创业板50 实时 TOP3 + 下次调仓 ───────────

def fetch_cyb50_top3() -> list | None:
    """联网抓国证创业板50 前3权重股(含权重%)。失败返回 None。纯标准库解析 xlsx。"""
    import io as _io, re as _re, html as _html, zipfile as _zip, urllib.request as _urllib
    try:
        req = _urllib.Request(
            CNINDEX_TOP3_URL,
            headers={"User-Agent": "Mozilla/5.0",
                     "Referer": "https://www.cnindex.com.cn/"})
        data = _urllib.urlopen(req, timeout=30).read()
        z = _zip.ZipFile(_io.BytesIO(data))
        sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8", "ignore")
        rows = _re.findall(r"<row[^>]*>(.*?)</row>", sheet, _re.S)

        def cval(c):
            m = _re.search(r"<t[^>]*>(.*?)</t>", c, _re.S)
            if m:
                return _html.unescape(m.group(1))
            m = _re.search(r"<v>(.*?)</v>", c, _re.S)
            return m.group(1) if m else ""

        recs = []
        for r in rows[1:]:
            cells = _re.findall(r"<c[^>]*>(.*?)</c>", r, _re.S)
            v = [cval(c) for c in cells]
            if len(v) >= 6 and v[1]:
                try:
                    recs.append({"date": v[0], "code": v[1].zfill(6),
                                 "name": v[2], "weight": float(v[5])})
                except ValueError:
                    continue
        if not recs:
            return None
        return sorted(recs, key=lambda x: x["weight"], reverse=True)[:3]
    except Exception as e:
        print(f"  [资讯联动] 国证官网抓取失败: {e}")
        return None


def _second_friday(year: int, month: int) -> date:
    """某年某月第二个星期五"""
    first = date(year, month, 1)
    return first + timedelta(days=(4 - first.weekday()) % 7 + 7)


def _next_trading_day(d: datetime.date, holidays=None) -> datetime.date:
    """d 之后第一个交易日 (跳过周末/节假日)"""
    holidays = holidays or set()
    nd = d + timedelta(days=1)
    while nd.weekday() >= 5 or nd in holidays:
        nd += timedelta(days=1)
    return nd


def compute_next_rebalance(today=None):
    """返回 (下次生效日 date, 距今天数 int)"""
    if today is None:
        today = datetime.date.today()
    cands = []
    for y in (today.year, today.year + 1):
        for m in (6, 12):
            eff = _next_trading_day(_second_friday(y, m))
            if eff > today:
                cands.append(eff)
    eff = min(cands)
    return eff, (eff - today).days


def get_current_basket_info() -> dict:
    """当前篮子 TOP3 + 下次调仓 + 关键日期; 联网抓取并与硬编码名单交叉核对。"""
    today = date.today()
    tstr = today.strftime("%Y-%m-%d")
    live = fetch_cyb50_top3()
    hard = get_bucket_codes(tstr)
    live_codes = [x["code"] for x in live] if live else []
    match = (set(live_codes) == set(hard)) if live else False

    if live:
        top3 = live
        source = f"国证官网 {live[0]['date']}"
        note = "（与名单一致）" if match else "（⚠联网与名单不符，以官方为准）"
    else:
        top3 = [{"code": c, "name": STOCK_NAMES.get(c, c), "weight": None} for c in hard]
        source = "硬编码兜底（未联网）"
        note = ""
    eff, days_left = compute_next_rebalance(today)
    period_start = max((sd for sd in SCHEDULE if sd <= tstr), default="2026-06-15")
    key_dates = [
        f"本期生效 {period_start}",
        f"下次生效 {eff.isoformat()}（还剩 {days_left} 天）",
    ]
    if today.month == 11 and today.day >= 20:
        key_dates.append("关注国证调样公告（通常生效前约2周发布）")
    return {
        "top3": top3, "source": source, "note": note, "match": match,
        "next_rebalance": eff.isoformat(), "days_left": days_left,
        "period_start": period_start, "key_dates": key_dates, "live": bool(live),
    }


# ─── 合成图: 所有图表纵向拼接为一张大图 ─────────────────

def compose_all_charts(chart_files: list[tuple[str, str]], save_path: str,
                       results: list[dict] | None = None,
                       annual_returns: dict | None = None,
                       combined: dict | None = None,
                       basket_info: dict | None = None):
    """将多张图表纵向拼接为一张大图，每张图前加详细数据说明，顶部加摘要

    针对手机端阅读优化：使用大字号、每张图前提供数据驱动的多行描述
    创红方案(创业板50 TOP3 篮子 × 红利信号)作头条: 顶部持仓指令 + 篮子信息块,
    底部年度表扩为 5 列(追加 创红方案收益 / 创红方案持仓)。
    """
    from PIL import Image, ImageDraw, ImageFont

    BORDER = 50          # 边距（加大）
    HEADER_HEIGHT = 1100  # 顶部持仓指令区（含创红方案篮子信息块）
    BG_COLOR = (13, 17, 23)         # #0d1117
    CAPTION_COLOR = (230, 237, 243)  # #e6edf3
    SEP_COLOR = (48, 54, 61)         # #30363d
    SUBTLE_COLOR = (139, 148, 158)   # #8b949e
    RED_COLOR = (255, 107, 107)      # 红利/红色信号
    BLUE_COLOR = (88, 166, 255)       # 成长/蓝色信号
    GOLD_COLOR = (255, 200, 50)      # 持仓指令高亮色
    GREEN_COLOR = (63, 185, 80)      # 下跌/绿色 (中国惯例: 跌=绿)

    # ── 加载字体（跨平台：自动发现）──
    import glob as _glob

    # 动态发现 Noto CJK 字体 (Linux GitHub Actions)
    def _discover_fonts():
        paths = []
        # Linux: 递归搜索 Noto CJK 字体
        for base in ["/usr/share/fonts", "/usr/local/share/fonts"]:
            if os.path.exists(base):
                paths.extend(_glob.glob(os.path.join(base, "**", "NotoSans*CJK*.ttc"), recursive=True))
                paths.extend(_glob.glob(os.path.join(base, "**", "NotoSans*CJK*.ttf"), recursive=True))
                # 也搜索文泉驿
                paths.extend(_glob.glob(os.path.join(base, "**", "wqy*.ttc"), recursive=True))
                paths.extend(_glob.glob(os.path.join(base, "**", "wqy*.ttf"), recursive=True))
        # Windows 字体
        win_fonts = [
            "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/msyhbd.ttc",
            "C:/Windows/Fonts/simhei.ttf", "C:/Windows/Fonts/simsun.ttc",
        ]
        for fp in win_fonts:
            if os.path.exists(fp):
                paths.append(fp)
        return paths

    font_paths = _discover_fonts()
    if font_paths:
        print(f"  [PIL合成] 找到 {len(font_paths)} 个候选字体, 首选: {font_paths[0]}", file=sys.stderr)
    else:
        print(f"  [PIL合成] ❌ 未找到任何中文字体文件!", file=sys.stderr)

    def _load_font(size):
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        # 最后回退
        print(f"  [PIL合成] ⚠️ 字体回退: load_default() (中文将为方框!)", file=sys.stderr)
        try:
            return ImageFont.truetype("C:/Windows/Fonts/arial.ttf", size)
        except Exception:
            return ImageFont.load_default()

    def wrap_text(text, font, max_w):
        # 按像素宽度换行: CJK逐字断, 拉丁按词断。返回行列表。
        lines = []
        for raw in text.splitlines():
            if not raw:
                lines.append("")
                continue
            cur = ""
            for ch in raw:
                trial = cur + ch
                try:
                    w = font.getlength(trial)
                except Exception:
                    w = font.getsize(trial)[0]
                if w <= max_w or not cur:
                    cur = trial
                else:
                    lines.append(cur)
                    cur = ch
            lines.append(cur)
        return lines

    def draw_wrapped(draw, xy, text, font, fill, max_w, lh=44):
        # 换行绘制, 返回结束 y。
        x, y = xy
        for line in wrap_text(text, font, max_w):
            draw.text((x, y), line, fill=fill, font=font)
            y += lh
        return y

    # 字体全部放大 ~1.5倍
# 字体全部放大 ~1.5倍
    font_title = _load_font(72)       # 大标题
    font_hold = _load_font(90)        # 持仓指令（最大最醒目）
    font_sub = _load_font(48)         # 副标题/数据行
    font_section = _load_font(60)     # 章节标题
    font_body = _load_font(46)        # 图表说明正文
    font_small = _load_font(40)       # 图表说明小字

    # ── 读取所有图片 ──
    images = []
    max_w = 0
    for _title, path in chart_files:
        try:
            img = Image.open(path)
            images.append(img)
            max_w = max(max_w, img.width)
        except Exception as e:
            print(f"  无法读取 {path}: {e}")

    if not images or not results:
        print("  没有可拼接的图片或结果数据")
        return

    uniform_w = max_w + BORDER * 2

    # ── 为每张图生成数据驱动说明 ──
    def _gen_caption(idx_pair: str, chart_type: str, r: dict) -> list[str]:
        """根据图表类型和数据分析结果生成说明文字（精简版）"""
        signal = r.get("current_signal", "?")

        sig_emoji = "🔥" if signal == "成长" else "💤"

        if chart_type == "short":
            return [
                f"【{idx_pair}】短期趋势（近2月）",
                f"{sig_emoji} 当前信号：{signal}期",
            ]
        elif chart_type == "long":
            return [
                f"【{idx_pair}】长期趋势（近3年）",
                f"{sig_emoji} 当前信号：{signal}期",
            ]
        elif chart_type == "returns":
            return [
                "【累计收益对比】策略 vs Buy&Hold",
                "   粗线 > 细线 → 策略跑赢；反之跑输",
            ]
        return [f"【{idx_pair}】"]

    def _calc_caption_height(lines: list[str]) -> int:
        """根据行数估算说明区域高度"""
        line_h = 68  # font_body 行高（放大后）
        return 36 + len(lines) * line_h + 30  # 上下 padding

    def _draw_annual_table(draw, x: int, y: int, width: int,
                           annual_returns: dict, data_date: str):
        """在画布底部绘制逐年涨幅表 (5 列)。

        列: 年份 | 创业板轮动 | 创业板只成长 | 创红方案收益 | 创红方案持仓
          - 轮动 = 本策略(红利视为风险指标, 跌破下轨切红利)
          - 只成长 = 成长期持有成长、红利期空仓(=1.0), 即策略的"成长贡献"分量
          - 创红方案 = 创业板50 TOP3 篮子 × 红利信号 (成长→持TOP3, 红利→空仓), 2014-06 起
        涨(>=0)=红, 跌(<0)=绿(中国惯例)。缺失年份显示 —。最新年份标 * 表示年内至今。
        """
        # ── 标题 ──
        draw.text((x, y), "逐年涨幅：轮动对比 + 创红方案", fill=CAPTION_COLOR, font=font_section)
        sub = (f"按自然年 · 涨红跌绿 · 末行*为年内至今(截至 {data_date}) · "
               f"空仓=红利期主动持币(收益=0, 非缺数据)")
        _sub_lines = wrap_text(sub, font_small, width - 8)
        _sy = y + 64
        for _sl in _sub_lines:
            draw.text((x, _sy), _sl, fill=SUBTLE_COLOR, font=font_small)
            _sy += 44

        # ── 列布局 (5 列) ──
        w_year = width * 0.11
        w_a = width * 0.21       # 2 个对比列等宽
        w_comb = width * 0.13    # 创红方案收益列
        w_hold = width - (w_year + 2 * w_a + w_comb)  # 创红方案持仓列 (占满余数)
        x_year_end = x + w_year
        x_a1 = x_year_end
        x_a2 = x_a1 + w_a
        x_comb = x_a2 + w_a
        x_hold = x_comb + w_comb

        HEADER_ROW_H = 64
        ROW_H = 58
        ty = y + 64 + len(_sub_lines) * 44 + 12  # 表头起始 y (含换行副标题)

        cyb_map = {yy: rr for yy, rr in annual_returns.get("cyb", [])}
        cyb_go_map = {yy: rr for yy, rr in annual_returns.get("cyb_go", [])}
        comb_map = {yy: rr for yy, rr in annual_returns.get("combined", [])}
        comb_hold = annual_returns.get("combined_hold", {})
        all_years = sorted(set(cyb_map) | set(cyb_go_map) | set(comb_map))
        if not all_years:
            return
        max_year = max(all_years)

        # ── 表头 (附连乘终值, 便于直接验算: 各年(1+ret)连乘=该终值) ──
        def _term(mp):
            p = 1.0
            for yy in sorted(mp):
                p *= (1 + mp[yy])
            return p
        cyb_term = _term(cyb_map)
        cyb_go_term = _term(cyb_go_map)
        comb_term = _term(comb_map)
        draw.rectangle([(x, ty), (x + width, ty + HEADER_ROW_H)], fill=(22, 27, 34))
        draw.line([(x, ty + HEADER_ROW_H), (x + width, ty + HEADER_ROW_H)],
                  fill=SEP_COLOR, width=2)
        draw.text((x + 20, ty + 14), "年份", fill=CAPTION_COLOR, font=font_small)
        draw.text((x_a1 + 18, ty + 14), f"创业板轮动 {cyb_term:.2f}x", fill=RED_COLOR, font=font_small)
        draw.text((x_a2 + 18, ty + 14), f"创业板只成长 {cyb_go_term:.2f}x", fill=BLUE_COLOR, font=font_small)
        draw.text((x_comb + 16, ty + 14), f"创红方案 {comb_term:.2f}x", fill=GOLD_COLOR, font=font_small)
        draw.text((x_hold + 16, ty + 14), "创红方案持仓", fill=GOLD_COLOR, font=font_small)

        # ── 数据行 ──
        ry = ty + HEADER_ROW_H
        for i, yr in enumerate(all_years):
            row_bg = (17, 21, 28) if i % 2 == 0 else (22, 27, 34)
            draw.rectangle([(x, ry), (x + width, ry + ROW_H)], fill=row_bg)
            yr_label = str(yr) + ("*" if yr == max_year else "")
            draw.text((x + 20, ry + 14), yr_label, fill=CAPTION_COLOR, font=font_small)

            for xc, mp in [(x_a1, cyb_map), (x_a2, cyb_go_map)]:
                if yr in mp:
                    ret = mp[yr]
                    col = RED_COLOR if ret >= 0 else GREEN_COLOR
                    draw.text((xc + 18, ry + 14), f"{ret * 100:+.1f}%", fill=col, font=font_small)
                else:
                    draw.text((xc + 18, ry + 14), "—", fill=SUBTLE_COLOR, font=font_small)
            # 创红方案收益
            if yr in comb_map:
                ret = comb_map[yr]
                col = RED_COLOR if ret >= 0 else GREEN_COLOR
                draw.text((x_comb + 16, ry + 14), f"{ret * 100:+.1f}%", fill=col, font=font_small)
            else:
                draw.text((x_comb + 16, ry + 14), "—", fill=SUBTLE_COLOR, font=font_small)
            # 创红方案持仓 (列宽不足时自动缩字号, 避免文字截断)
            if yr in comb_hold:
                _htxt = comb_hold[yr]
                _hf = font_small
                _avail = w_hold - 32
                try:
                    if draw.textlength(_htxt, font=font_small) > _avail:
                        _hf = _load_font(30)
                except Exception:
                    pass
                draw.text((x_hold + 16, ry + 14), _htxt, fill=CAPTION_COLOR, font=_hf)
            else:
                draw.text((x_hold + 16, ry + 14), "—", fill=SUBTLE_COLOR, font=font_small)
            ry += ROW_H

        # ── 竖网格线 ──
        for xg in [x, x_year_end, x_a1, x_a2, x_comb, x_hold, x + width]:
            draw.line([(xg, ty), (xg, ry)], fill=SEP_COLOR, width=1)

    def _draw_rebalance_history(draw, x: int, y: int, width: int, combined: dict):
        """创业板50 TOP3 调仓历史表: 每6个月一期, 列明生效日 / TOP3全名 / 该期状态。

        直接解决'持仓只显示半年'的困惑——按调仓期(而非按自然年)展示,
        并标注每期内'成长期持有天数 / 红利期空仓天数'。
        """
        draw.text((x, y), "创红方案 调仓历史（每6个月一期）",
                  fill=CAPTION_COLOR, font=font_section)
        sub = ("蓝字=成长期持有天数(持该期TOP3等权); 红字=红利期空仓天数(持币, 收益=0, 非缺数据)。"
               "该期状态 = 期内成长期天数 / 空仓天数。")
        draw.text((x, y + 64), sub, fill=SUBTLE_COLOR, font=font_small)

        def _tw(s_):
            try:
                return draw.textlength(s_, font=font_small)
            except Exception:
                return font_small.getsize(s_)[0]

        periods = sorted(SCHEDULE.keys())
        dates = combined.get("dates", [])
        positions = combined.get("positions", [])
        stats = []
        for i, sd in enumerate(periods):
            nd = periods[i + 1] if i + 1 < len(periods) else "9999-12-31"
            g = e = 0
            for d, p in zip(dates, positions):
                if p is None:
                    continue
                if sd <= d < nd:
                    if p == 1:
                        g += 1
                    else:
                        e += 1
            stats.append((sd, SCHEDULE[sd], g, e))

        HEADER_ROW_H = 60
        ROW_H = 50
        ty = y + 110
        w_date = width * 0.20
        w_top3 = width * 0.50
        w_status = width - w_date - w_top3
        x_date_end = x + w_date
        x_top3 = x_date_end
        x_status = x_top3 + w_top3

        draw.rectangle([(x, ty), (x + width, ty + HEADER_ROW_H)], fill=(22, 27, 34))
        draw.line([(x, ty + HEADER_ROW_H), (x + width, ty + HEADER_ROW_H)],
                  fill=SEP_COLOR, width=2)
        draw.text((x + 16, ty + 14), "生效日", fill=CAPTION_COLOR, font=font_small)
        draw.text((x_top3 + 16, ty + 14), "TOP3（全名·等权）", fill=CAPTION_COLOR, font=font_small)
        draw.text((x_status + 16, ty + 14), "该期状态", fill=CAPTION_COLOR, font=font_small)

        ry = ty + HEADER_ROW_H
        for i, (sd, codes, g, e) in enumerate(stats):
            row_bg = (17, 21, 28) if i % 2 == 0 else (22, 27, 34)
            draw.rectangle([(x, ry), (x + width, ry + ROW_H)], fill=row_bg)
            names = "/".join(STOCK_NAMES.get(c, c) for c in codes)
            draw.text((x + 16, ry + 10), sd, fill=CAPTION_COLOR, font=font_small)
            draw.text((x_top3 + 16, ry + 10), names, fill=CAPTION_COLOR, font=font_small)
            # 状态下分色: 成长=蓝, 空仓=红 (同一单元格两段分别着色, 不再整行单色)
            sx = x_status + 16
            sy = ry + 10
            if g == 0 and e == 0:
                draw.text((sx, sy), "（无交易数据）", fill=SUBTLE_COLOR, font=font_small)
            elif e == 0:
                draw.text((sx, sy), f"全程持有 {g}天", fill=BLUE_COLOR, font=font_small)
            elif g == 0:
                draw.text((sx, sy), f"全程空仓 {e}天", fill=RED_COLOR, font=font_small)
            else:
                s1, s2, s3 = f"成长{g}天", " / ", f"空仓{e}天"
                draw.text((sx, sy), s1, fill=BLUE_COLOR, font=font_small)
                w1 = _tw(s1)
                draw.text((sx + w1, sy), s2, fill=SUBTLE_COLOR, font=font_small)
                w2 = _tw(s2)
                draw.text((sx + w1 + w2, sy), s3, fill=RED_COLOR, font=font_small)
            ry += ROW_H

        for xg in [x, x_date_end, x_top3, x_status, x + width]:
            draw.line([(xg, ty), (xg, ry)], fill=SEP_COLOR, width=1)

    # ── 计算总高度 ──
    total_h = HEADER_HEIGHT
    for i, (img, (title_key, _path)) in enumerate(zip(images, chart_files)):
        # 仅创业板策略, results 只含 growth_cyb (r_idx 恒为 0)
        r_idx = 0
        r = results[r_idx] if r_idx < len(results) else None

        if r and "error" not in r:
            idx_pair = r.get("index_pair", "").split(" vs ")[0]
            if "短期" in title_key:
                caption_lines = _gen_caption(idx_pair, "short", r)
            elif "长期" in title_key:
                caption_lines = _gen_caption(idx_pair, "long", r)
            elif "累计收益" in title_key:
                caption_lines = _gen_caption("", "returns", r)
            else:
                caption_lines = [title_key]
        else:
            caption_lines = [title_key]

        caption_h = _calc_caption_height(caption_lines)
        total_h += caption_h + img.height + BORDER // 2
    total_h += BORDER

    # ── 年度涨幅表预留高度 ──
    annual_height = 0
    if annual_returns:
        _cyb_y = [y for y, _ in annual_returns.get("cyb", [])]
        _comb_y = [y for y, _ in annual_returns.get("combined", [])]
        _ally = _cyb_y + _comb_y
        if _ally:
            _row_count = max(_ally) - min(_ally) + 1
            # 动态计算副标题行数(原为写死的"最多3行", 副标题换行超3行会顶掉表格/重叠)
            _sub_text = (f"按自然年 · 涨红跌绿 · 末行*为年内至今(截至 YYYY-MM-DD) · "
                         f"空仓=红利期主动持币(收益=0, 非缺数据)")
            _sub_n = len(wrap_text(_sub_text, font_small, uniform_w - 2 * BORDER - 8))
            annual_height = 64 + _sub_n * 44 + 12 + 64 + _row_count * 58 + 30
            total_h += BORDER + annual_height

    # ── 调仓历史表预留高度 ──
    history_height = 0
    if combined:
        _n_periods = len(SCHEDULE)
        history_height = 110 + 60 + _n_periods * 50 + 30
        total_h += BORDER + history_height

    # ── 创建画布 ──
    canvas = Image.new("RGB", (uniform_w, total_h), BG_COLOR)
    draw = ImageDraw.Draw(canvas)

    # ════════════════════════════════════════════
    # 顶部持仓指令区
    # ════════════════════════════════════════════
    draw.rectangle([(0, 0), (uniform_w, HEADER_HEIGHT)], fill=(22, 27, 34))
    draw.line([(0, HEADER_HEIGHT - 2), (uniform_w, HEADER_HEIGHT - 2)],
              fill=SEP_COLOR, width=3)

    fetch_time = results[0].get("fetch_time", "") if results else datetime.now().strftime("%Y-%m-%d %H:%M")
    data_date = results[0].get("latest_date", "") if results else ""
    if not data_date:
        data_date = fetch_time[:10] if fetch_time else datetime.now().strftime("%Y-%m-%d")

    # ── 标题 ──
    draw.text((BORDER, 25), "创红方案 · 每日持仓信号", fill=CAPTION_COLOR, font=font_title)
    draw.text((BORDER, 110),
              f"数据日期: {data_date}    策略: 1%缓冲均线 (MA=20日)  |  成长→持TOP3 / 红利→空仓",
              fill=SUBTLE_COLOR, font=font_sub)

    # ── 持仓指令（创红方案作头条: 基于创业板指/红利信号）──
    y = 190
    cyb_signal = next((r.get("current_signal") for r in results if "error" not in r), None)
    if cyb_signal == "红利":
        hold_text = "空仓观望（不持红利）"
        hold_color = RED_COLOR
        hold_bg = (60, 20, 20)
    elif cyb_signal == "成长":
        if basket_info:
            names = "、".join(t["name"] for t in basket_info["top3"])
        else:
            names = "创业板50 TOP3"
        hold_text = f"持有{names}"
        hold_color = BLUE_COLOR
        hold_bg = (20, 30, 60)
    else:
        hold_text = "信号不一致，按创业板指信号执行"
        hold_color = GOLD_COLOR
        hold_bg = (60, 50, 10)

    # 持仓指令背景框（大号高亮）
    box_h = 130
    draw.rectangle([(BORDER, y), (uniform_w - BORDER, y + box_h)],
                   fill=hold_bg)
    draw.rectangle([(BORDER, y), (uniform_w - BORDER, y + box_h)],
                   outline=hold_color, width=4)
    draw.text((BORDER + 30, y + 20), hold_text, fill=hold_color, font=font_hold)

    y += box_h + 40

    # ── 一句话总结 + 切换条件 ──
    for r in results:
        if "error" in r:
            continue
        pair_name = r.get("index_pair", "").split(" vs ")[0]
        signal = r.get("current_signal", "?")
        upper = r.get("current_upper", 0)
        lower = r.get("current_lower", 0)
        ratio = r.get("current_ratio", 0)
        text_color = BLUE_COLOR if signal == "成长" else RED_COLOR

        if signal == "红利":
            dist = (upper - ratio) / ratio * 100
            cond = f"比值涨至 {upper:.4f} → 换成长 (还差 {dist:+.1f}%)"
        else:
            dist = (lower - ratio) / ratio * 100
            cond = f"比值跌至 {lower:.4f} → 换红利 (还差 {dist:+.1f}%)"

        draw.text((BORDER, y),
                  f"  {pair_name}: {signal}期  |  {cond}",
                  fill=text_color, font=font_sub)
        y += 72

    # ── 底部提示 ──
    y += 10
    draw.text((BORDER, y), "  仅供研究参考，不构成投资建议",
              fill=SUBTLE_COLOR, font=font_small)
    y += 50
    draw.text((BORDER, y), "  ↓ 短期趋势 → 长期全景 → 累计收益",
              fill=SUBTLE_COLOR, font=font_small)

    # ── 创红方案篮子信息块 ──
    if basket_info:
        bx = BORDER + 30
        _top3 = basket_info["top3"]
        top3_str = "  ".join(
            (f"{t['name']} {t['weight']:.1f}%" if t.get("weight") is not None else t["name"])
            for t in _top3)
        _info = [
            ("▌ 创红方案篮子（成长→持TOP3 / 红利→空仓）",
             font_sub, GOLD_COLOR, 60, 22),
            (f"当前篮子: 创业板50 前3权重 — {top3_str}", font_small, CAPTION_COLOR, 44, 14),
            (f"数据源: {basket_info['source']}{basket_info['note']}", font_small, SUBTLE_COLOR, 44, 14),
            (f"关键日期: {'  |  '.join(basket_info['key_dates'])}", font_small, SUBTLE_COLOR, 44, 14),
        ]
        if combined:
            _info.append((
                f"创红方案({combined['start_date']} 起): 总 {combined['total_ret']*100:+.0f}%  "
                f"年化 {combined['ann_ret']*100:+.1f}%  最大回撤 {combined['max_dd']*100:+.1f}%",
                font_small, GOLD_COLOR, 44, 14))
            _info.append((
                "⚠ 篮子3股集中, 单股退市(乐视式)瞬时 -1/3 仓位, 实盘须强制止损/退市处理",
                font_small, (255, 160, 80), 44, 14))
        _maxw = uniform_w - 2 * BORDER - 60
        block_h = 24
        for (_t, _f, _c, _lh, _gap) in _info:
            block_h += len(wrap_text(_t, _f, _maxw)) * _lh + _gap
        block_h += 18
        by = 560
        draw.rectangle([(BORDER, by), (uniform_w - BORDER, by + block_h)],
                       fill=(18, 24, 33), outline=GOLD_COLOR, width=2)
        yy = by + 24
        for (_t, _f, _c, _lh, _gap) in _info:
            yy = draw_wrapped(draw, (bx, yy), _t, _f, _c, _maxw, _lh)
            yy += _gap

    y_offset = HEADER_HEIGHT

    # ════════════════════════════════════════════
    # 逐个图表 + 说明
    # ════════════════════════════════════════════
    for i, (img, (title_key, _path)) in enumerate(zip(images, chart_files)):
        # 仅创业板策略, results 只含 growth_cyb (r_idx 恒为 0)
        r_idx = 0
        r = results[r_idx] if r_idx < len(results) else None

        if r and "error" not in r:
            idx_pair = r.get("index_pair", "").split(" vs ")[0]
            if "短期" in title_key:
                caption_lines = _gen_caption(idx_pair, "short", r)
            elif "长期" in title_key:
                caption_lines = _gen_caption(idx_pair, "long", r)
            elif "累计收益" in title_key:
                caption_lines = _gen_caption("", "returns", r)
            else:
                caption_lines = [title_key]
        else:
            caption_lines = [title_key]

        caption_h = _calc_caption_height(caption_lines)

        # ── 说明区域背景 ──
        draw.rectangle([(0, y_offset), (uniform_w, y_offset + caption_h)],
                       fill=(22, 27, 34))
        draw.line([(0, y_offset + caption_h - 1),
                    (uniform_w, y_offset + caption_h - 1)],
                  fill=SEP_COLOR, width=1)

        # ── 逐行绘制说明文字 ──
        line_y = y_offset + 14
        for j, line in enumerate(caption_lines):
            if j == 0:
                # 第一行 = 章节标题（大号加粗）
                draw.text((BORDER, line_y), line, fill=CAPTION_COLOR, font=font_section)
                line_y += 58
            elif line.startswith("   "):
                # 缩进行 = 正文（小号）
                draw.text((BORDER + 20, line_y), line.strip(), fill=SUBTLE_COLOR,
                          font=font_small)
                line_y += 44
            else:
                # 正文行
                # 判断是否含颜色标记
                use_color = CAPTION_COLOR
                if "红利" in line and "信号" in line:
                    use_color = RED_COLOR
                elif "成长" in line and "信号" in line:
                    use_color = BLUE_COLOR
                draw.text((BORDER, line_y), line, fill=use_color, font=font_body)
                line_y += 52

        y_offset += caption_h

        # ── 放置图表 ──
        x_center = (uniform_w - img.width) // 2
        canvas.paste(img, (x_center, y_offset))
        y_offset += img.height + BORDER // 2

    # ════════════════════════════════════════════
    # 年度涨幅表 (放在最底部)
    # ════════════════════════════════════════════
    if annual_returns and annual_height:
        y_offset += BORDER // 2
        _draw_annual_table(draw, BORDER, y_offset, uniform_w - 2 * BORDER,
                           annual_returns, data_date)
        y_offset += annual_height
    if combined:
        y_offset += BORDER // 2
        _draw_rebalance_history(draw, BORDER, y_offset, uniform_w - 2 * BORDER, combined)
        y_offset += history_height

    canvas.save(save_path, "PNG")
    print(f"  合成大图: {save_path} ({uniform_w}x{total_h})")
    return save_path


# ─── 飞书推送 ──────────────────────────────────────────

def push_to_feishu(image_path: str, chat_id: str = None) -> bool:
    """通过飞书 Open API 上传图片并发送到指定群聊

    使用环境变量 FEISHU_APP_ID / FEISHU_APP_SECRET 获取凭证。
    流程: 获取 tenant_access_token → 上传图片 → 发送图片消息
    """
    if chat_id is None:
        chat_id = FEISHU_CHAT_ID

    app_id = FEISHU_APP_ID
    app_secret = FEISHU_APP_SECRET

    if not app_secret:
        print("  [飞书推送] 跳过: 未配置 FEISHU_APP_SECRET")
        return False

    try:
        # 1. 获取 tenant_access_token
        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        resp = requests.post(token_url, json={
            "app_id": app_id,
            "app_secret": app_secret,
        }, timeout=15)
        resp.raise_for_status()
        token_data = resp.json()
        if token_data.get("code") != 0:
            print(f"  [飞书推送] 获取 token 失败: {token_data}")
            return False
        access_token = token_data["tenant_access_token"]
        print(f"  [飞书推送] 获取 tenant_access_token 成功")

        # 2. 上传图片
        upload_url = "https://open.feishu.cn/open-apis/im/v1/images"
        with open(image_path, "rb") as f:
            resp = requests.post(upload_url, headers={
                "Authorization": f"Bearer {access_token}",
            }, files={
                "image_type": (None, "message"),
                "image": (os.path.basename(image_path), f, "image/png"),
            }, timeout=30)
        resp.raise_for_status()
        upload_data = resp.json()
        if upload_data.get("code") != 0:
            print(f"  [飞书推送] 上传图片失败: {upload_data}")
            return False
        image_key = upload_data["data"]["image_key"]
        print(f"  [飞书推送] 上传图片成功: {image_key}")

        # 3. 发送图片消息
        msg_url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        content = json.dumps({"image_key": image_key})
        resp = requests.post(msg_url, headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }, json={
            "receive_id": chat_id,
            "msg_type": "image",
            "content": content,
        }, timeout=15)
        resp.raise_for_status()
        msg_data = resp.json()
        if msg_data.get("code") != 0:
            print(f"  [飞书推送] 发送消息失败: {msg_data}")
            return False
        print(f"  [飞书推送] 发送成功! message_id: {msg_data.get('data', {}).get('message_id', '?')}")
        return True

    except Exception as e:
        print(f"  [飞书推送] 异常: {e}")
        return False


# ─── 入口 ──────────────────────────────────────────────

def main():
    """运行完整监控"""
    all_results = []
    all_briefs = []
    all_chart_files = []  # [(标题, 路径), ...] 用于合成大图

    # 资讯联动: 当前篮子 TOP3 + 下次调仓 (联网抓取国证官网, 失败兜底)
    try:
        basket_info = get_current_basket_info()
    except Exception as e:
        print(f"  [资讯联动] 异常: {e}")
        basket_info = None

    for growth_key in ["growth_cyb"]:
        result = analyze(growth_key)
        report = format_report(result)
        brief = format_brief(result, basket_info)
        print(report)
        all_results.append(result)
        all_briefs.append(brief)
        if "error" not in result:
            save_report(result, report)
        # 收集创业板指的趋势图
        if growth_key == "growth_cyb" and result.get("chart_paths"):
            name_map = {"short": "短期趋势 (近2月)", "long": "长期趋势 (近3年)"}
            for ctype, cpath in result["chart_paths"]:
                idx_name = result.get("index_pair", "未知").split(" vs ")[0]
                all_chart_files.append((f"{idx_name} — {name_map.get(ctype, ctype)}", cpath))

    # 一次性准备回测数据 (加载+计算只做一次, 两图共用, 消除重复计算)
    prep = prepare_backtest_data()
    cyb_annual, cyb_go_annual = [], []
    date_str = datetime.now().strftime("%Y%m%d")
    combined_returns_path = None

    if prep is None:
        print("  [跳过] 累计收益图数据不足")
    else:
        combined_returns_path = str(CACHE_DIR / f"chart_combined_returns_{date_str}.png")
        try:
            combined_returns_path, cyb_annual, cyb_go_annual = plot_combined_returns_chart(
                prep, all_results, combined_returns_path)
            all_chart_files.append(("累计收益对比 — 策略曲线", combined_returns_path))
        except Exception as e:
            print(f"  合并收益图生成失败: {e}")
            combined_returns_path = None

    # 生成『创业板只做成长』曲线+逐年收益图 (接入每日报告)
    if prep is None:
        print("  [跳过] 只做成长曲线图数据不足")
    else:
        growth_curve_path = str(CACHE_DIR / f"chart_cyb_growth_only_curve_{date_str}.png")
        try:
            p = plot_growth_only_curve_chart(prep, growth_curve_path)
            if p:
                all_chart_files.append(("创业板只做成长 — 累计净值 + 逐年收益", p))
        except Exception as e:
            print(f"  只做成长曲线图生成失败: {e}")

    # 创红方案(创业板50 TOP3 篮子 × 红利信号) — 净值/逐年收益/逐年持仓
    try:
        combined = compute_combined_strategy()
    except Exception as e:
        print(f"  [创红方案] 异常: {e}")
        combined = None
    if combined:
        print(f"  [创红方案] 总 {combined['total_ret']*100:+.0f}%  年化 {combined['ann_ret']*100:+.1f}%  "
              f"回撤 {combined['max_dd']*100:+.1f}%  ({combined['start_date']}~{combined['end_date']})")

    # ── 合成大图 ──
    composed_path = str(CACHE_DIR / f"chart_all_in_one_{date_str}.png")
    annual_dict = {"cyb": cyb_annual, "cyb_go": cyb_go_annual}
    if combined:
        annual_dict["combined"] = combined["combined_annual"]
        annual_dict["combined_hold"] = combined["combined_holdings"]
    compose_all_charts(all_chart_files, composed_path, results=all_results,
                       annual_returns=annual_dict, combined=combined, basket_info=basket_info)

    # 写入合并摘要
    summary = "\n\n".join(all_briefs)
    if combined_returns_path:
        summary += f"\n\n📊 合并大图: {composed_path}"
    (CACHE_DIR / "latest_summary.txt").write_text(summary, encoding="utf-8")
    print("\n\n" + summary)

    # ── 推送到飞书 ──
    if composed_path and os.path.exists(composed_path):
        print("\n" + "=" * 50)
        print("推送合成大图到飞书...")
        push_to_feishu(composed_path)

    return all_results, summary, composed_path


if __name__ == "__main__":
    main()

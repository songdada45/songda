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

数据源: 腾讯财经 → 新浪财经 → 东方财富 → yfinance
指数: 创业板指 399006 / 科创50 000688 / 中证红利 000922
"""

import csv
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ─── 配置 ────────────────────────────────────────────
INDICES = {
    "growth_cyb":  {"name": "创业板指",   "code": "399006", "market": "sz", "ticker": "399006.SZ"},
    "growth_kc50": {"name": "科创50",    "code": "000688", "market": "sh", "ticker": "000688.SS"},
    "dividend":    {"name": "中证红利",  "code": "000922", "market": "sh", "ticker": "000922.SS"},
}

MA_WINDOW = 20          # 均线窗口
MA_BAND = 0.01          # 缓冲区带宽 1% (原研究最优参数)
DATA_DAYS = 2000        # 约8年交易日(腾讯API上限)，支持长期收益对比
ONE_WAY_FEE = 0.001     # 单边交易费率 0.1%

CACHE_DIR = Path.cwd() / ".growth_dividend_cache"
CACHE_DIR.mkdir(exist_ok=True)

STATE_FILE = CACHE_DIR / "strategy_state.json"   # 持仓状态持久化

# ─── 飞书推送配置 (GitHub Actions 通过 Secrets 注入) ───
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aae07cacb1785bdb")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_228f22564f13ddf89372bcbfb0513921")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

EASTMONEY_SECID = {"399006": "0.399006", "000688": "1.000688", "000922": "1.000922"}


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
        return [{"date": str(k[0]), "close": float(k[2])} for k in klines]
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
                records.append({"date": str(k["day"]), "close": float(k["close"])})
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
            records.append({"date": parts[0], "close": float(parts[2])})
        return records
    except Exception as e:
        print(f"  东方财富 {code} 失败: {e}")
        return None


def fetch_yfinance(ticker: str, days: int) -> list[dict] | None:
    """从 yfinance 获取数据"""
    try:
        import yfinance as yf
    except ImportError:
        return None
    try:
        end = datetime.now()
        start = end - timedelta(days=days * 2)
        df = yf.download(ticker, start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"), progress=False, auto_adjust=True)
        if df is None or df.empty:
            return None
        records = []
        for idx, row in df.iterrows():
            d = idx.strftime("%Y-%m-%d") if hasattr(idx, "strftime") else str(idx)[:10]
            c = float(row["Close"])
            if c > 0:
                records.append({"date": d, "close": c})
        return records
    except Exception as e:
        print(f"  yfinance {ticker} 失败: {e}")
        return None


def load_cache(index_code: str) -> list[dict] | None:
    cache_file = CACHE_DIR / f"{index_code}.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if isinstance(data, list) and len(data) >= 15:
            return data
    except Exception:
        pass
    return None


def save_cache(index_code: str, records: list[dict]):
    (CACHE_DIR / f"{index_code}.json").write_text(
        json.dumps(records, ensure_ascii=False), encoding="utf-8")


def load_long_csv_data() -> dict[str, list[dict]] | None:
    """从博主15年CSV数据加载更长历史 (用于累计收益图扩展时间轴)
    
    CSV 来源: ZF1Huang/growth_dividend_rotation_research data_cache/
    数据范围:
      - 399006 (创业板指): 2010-06-01 起 (~16年)
      - 000922 (中证红利):  2007-05-28 起 (~19年)  
      - 000688 (科创50):   2020-01-02 起 (~6.5年)
    
    返回: {code: [{"date":..., "close":...}, ...]} 或 None
    """
    csv_dir = Path(__file__).parent / "long_data"
    codes = ["399006", "000922", "000688"]
    result = {}
    for code in codes:
        csv_path = csv_dir / f"{code}.csv"
        if not csv_path.exists():
            return None
        try:
            with open(csv_path, "r", encoding="utf-8-sig") as f:
                reader = csv.reader(f)
                header = next(reader)
                # 找 close 列索引
                ci = header.index("close") if "close" in header else 2
                rows = []
                for r in reader:
                    if len(r) > ci:
                        rows.append({"date": r[0], "close": float(r[ci])})
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
        ("yfinance", lambda: fetch_yfinance(index_info["ticker"], days)),
    ]

    for name, fetcher in sources:
        print(f"  尝试 {name}...", end=" ", flush=True)
        try:
            records = fetcher()
            if records and len(records) >= 15:
                print(f"成功 ({len(records)} 条)")
                save_cache(code, records)
                return records
            print(f"失败 (数据不足)")
        except Exception as e:
            print(f"异常: {e}")
        time.sleep(0.5)

    if cached:
        print(f"  使用过期缓存")
        return cached
    return None


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
                        band: float, prev_state: int | None = None) -> list[dict]:
    """
    计算缓冲均线信号

    规则:
      - Ratio >= MA × (1 + band)  → state = 1 (成长期, 持成长)
      - Ratio <= MA × (1 - band)  → state = 0 (红利期, 持红利)
      - 缓冲区内 → state 延续上一日

    返回: [{"date", "ratio", "ma", "upper", "lower", "state", "signal", "zone"}, ...]
    """
    signals = []
    last_state = prev_state

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


# ─── 状态持久化 ────────────────────────────────────────

def load_strategy_state() -> dict:
    """加载上次持仓状态"""
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_strategy_state(state: dict):
    """保存当前持仓状态"""
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


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

    # 读取上次状态
    state_cache = load_strategy_state()
    prev_state = state_cache.get(growth_key, {}).get("state", None)

    # 计算缓冲均线信号
    all_signals = calc_ma_band_signal(ratios, mas, MA_BAND, prev_state)

    # 关联日期
    valid_start = len(ratios) - len(all_signals)
    for j, s in enumerate(all_signals):
        s["date"] = dates[valid_start + j]
        s["growth_close"] = round(growth_closes[valid_start + j], 2)
        s["dividend_close"] = round(div_closes[valid_start + j], 2)

    # 最近10个信号
    recent = all_signals[-10:]

    current = recent[-1]
    consecutive = 1
    for s in reversed(recent[:-1]):
        if s["signal"] == current["signal"]:
            consecutive += 1
        else:
            break

    # 检查是否有信号切换 (上轨/下轨 触发)
    signal_change = None
    if len(recent) >= 2:
        prev = recent[-2]
        if prev["signal"] != current["signal"] and current["zone"] != "缓冲区内(维持)":
            signal_change = {
                "from": prev["signal"], "to": current["signal"],
                "date": current["date"], "trigger": current["zone"],
            }

    # 保存当前状态
    state_cache[growth_key] = {"state": current["state"], "date": current["date"]}
    save_strategy_state(state_cache)

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
        "consecutive_days": consecutive,
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
        f"  | 当前信号:  {emoji}  {signal.upper()}期  (已持续 {result['consecutive_days']} 天)",
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


def format_brief(result: dict) -> str:
    """简洁版报告 (用于消息推送)"""
    if "error" in result:
        return f"[轮动监控] 错误: {result['error']}"

    signal = result["current_signal"]
    emoji = "📈" if signal == "成长" else "💰"
    diff_pct = result["ratio_ma_diff_pct"]

    lines = [
        f"{emoji} 风格轮动监控 {result['index_pair']}",
        f"信号: {signal}期 | 持续{result['consecutive_days']}天",
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
    return "\n".join(lines)


# ─── 可视化图表 (长期 / 短期 / 累计收益) ──────────────────

CHART_LONG_DAYS = 750    # 长期图: 约3年交易日，把握大趋势
CHART_SHORT_DAYS = 45    # 短期图: 约2个月交易日，观察近期拐点


def setup_chinese_font():
    """配置中文字体"""
    try:
        import matplotlib.font_manager as fm
        for font_name in ["Microsoft YaHei", "SimHei", "STHeiti", "WenQuanYi Micro Hei"]:
            try:
                fm.findfont(font_name, fallback_to_default=False)
                return font_name
            except Exception:
                continue
    except Exception:
        pass
    return "sans-serif"


def _setup_dark_style():
    """统一深色主题配置"""
    import matplotlib.pyplot as plt
    font_name = setup_chinese_font()
    plt.rcParams["font.family"] = font_name
    plt.rcParams["font.size"] = 14
    plt.rcParams["axes.labelsize"] = 14
    plt.rcParams["xtick.labelsize"] = 12
    plt.rcParams["ytick.labelsize"] = 12
    plt.rcParams["legend.fontsize"] = 13
    plt.rcParams["axes.titlesize"] = 16
    plt.rcParams["axes.unicode_minus"] = False


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
        f"信号: {result['current_signal']}期 | 持续{result['consecutive_days']}天\n"
        f"比值: {result['current_ratio']:.4f} | 均线: {result['current_ma']:.4f}\n"
        f"偏离: {result['ratio_ma_diff_pct']:+.2f}% | "
        f"距上轨: {result['distance_to_upper']:+.2f}% | 距下轨: {result['distance_to_lower']:+.2f}%"
    )
    ax.text(0.02, 0.97, info, transform=ax.transAxes,
            fontsize=12, color="#8b949e", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.9))


def _add_legend(ax, lines: list):
    """添加图例 (深色主题)"""
    legend = ax.legend(lines, [l.get_label() for l in lines],
                       loc="upper right", framealpha=0.85, fontsize=12,
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

    plt.tight_layout()
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

    plt.tight_layout()
    fig.savefig(save_path, dpi=150, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)


# ── 图表 3: 合并累计收益曲线 (5条曲线) ──────────────────

def plot_combined_returns_chart(results: list[dict], save_path: str):
    """合并收益对比图 — 5条归一化收益曲线

    曲线:
      1. 一直持有中证红利 (红利基准)
      2. 一直持有创业板指 (成长1)
      3. 一直持有科创50 (成长2)
      4. 策略轮动(创业板指 vs 红利)
      5. 策略轮动(科创50 vs 红利)

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

    # ── 数据加载: 优先用CSV长数据 (16年) + 腾讯API补充最新日 ──
    # 策略: CSV历史数据覆盖 2010~ , 腾讯API补充 CSV 末尾之后的新交易日

    div_info = INDICES["dividend"]
    cyb_info = INDICES["growth_cyb"]
    kc50_info = INDICES["growth_kc50"]

    print("\n" + "=" * 50)
    print("生成合并收益对比图...")
    print("加载CSV长期历史数据...")
    csv_data = load_long_csv_data()

    # 腾讯API获取最新数据 (用于补充CSV末尾之后的新日期)
    print("获取腾讯API最新数据 (补充)...")
    api_div = get_index_data(div_info)
    api_cyb = get_index_data(cyb_info)
    api_kc50 = get_index_data(kc50_info)

    if not api_div or not api_cyb or not api_kc50:
        print("  腾讯API数据获取不全，跳过往期收益图")
        return None

    # ── 构建 date→close 映射 (CSV + API 合并) ──
    def _merge_data(csv_rows, api_rows):
        """CSV长数据 + API最新数据合并，CSV优先 (覆盖更早日期)"""
        merged = {}
        if csv_rows:
            for r in csv_rows:
                merged[r["date"]] = r["close"]
        # API数据补充 (只补充CSV没有的新日期)
        if api_rows:
            for r in api_rows:
                if r["date"] not in merged:
                    merged[r["date"]] = r["close"]
        return merged

    div_map = _merge_data(
        csv_data["000922"] if csv_data else None, api_div)
    cyb_map = _merge_data(
        csv_data["399006"] if csv_data else None, api_cyb)
    kc50_map = _merge_data(
        csv_data["000688"] if csv_data else None, api_kc50)

    # 创业板指 + 红利 共同日期 (从2010年起, CSV覆盖16年)
    cyb_div_dates = sorted(set(div_map.keys()) & set(cyb_map.keys()))
    # 科创50 + 红利 共同日期 (从2020年起)
    kc50_div_dates = sorted(set(div_map.keys()) & set(kc50_map.keys()))

    print(f"  创板+红利: {cyb_div_dates[0]} ~ {cyb_div_dates[-1]} ({len(cyb_div_dates)}天)")
    print(f"  科创+红利: {kc50_div_dates[0]} ~ {kc50_div_dates[-1]} ({len(kc50_div_dates)}天)")

    if len(cyb_div_dates) < MA_WINDOW + 2:
        print(f"  创板+红利交易日不足 ({len(cyb_div_dates)} 天)")
        return None

    # ── Buy & Hold 归一化曲线 (各自从最早日期开始) ──
    # 红利和创业板: 从2018-05起 (2指数交集)
    div_bh_dt = [dt.strptime(d, "%Y-%m-%d") for d in cyb_div_dates]
    div_bh_norm = [div_map[d] / div_map[cyb_div_dates[0]] for d in cyb_div_dates]
    cyb_bh_norm = [cyb_map[d] / cyb_map[cyb_div_dates[0]] for d in cyb_div_dates]

    # 科创50: 从2019-12起 (2指数交集)
    kc50_bh_dt = [dt.strptime(d, "%Y-%m-%d") for d in kc50_div_dates]
    kc50_bh_norm = [kc50_map[d] / kc50_map[kc50_div_dates[0]] for d in kc50_div_dates]

    # ── 策略轮动收益 (各指数对独立计算, 从2指数交集起) ──
    def calc_strategy_returns(growth_prices, div_prices, dates_list):
        """计算给定成长指数的策略轮动收益 (避免前视偏差)"""
        # 计算每日比值和信号
        ratios_list = [g / d for g, d in zip(growth_prices, div_prices)]
        mas_list = calc_ma(ratios_list, MA_WINDOW)
        signals_list = calc_ma_band_signal(ratios_list, mas_list, MA_BAND)

        # 建立 date → signal 映射
        sig_by_date = {}
        valid_start = len(ratios_list) - len(signals_list)
        for j, s in enumerate(signals_list):
            sig_by_date[dates_list[valid_start + j]] = s["state"]

        # 净值计算: Day 0=1.0, 根据 T-1 信号决定 T 日持有的资产
        nav = [1.0]
        position = None  # None=初始, 0=红利, 1=成长
        growth_ret = []
        div_ret = []

        for i in range(1, len(dates_list)):
            g_ret = growth_prices[i] / growth_prices[i - 1]
            d_ret = div_prices[i] / div_prices[i - 1]
            growth_ret.append(g_ret)
            div_ret.append(d_ret)

            prev_date = dates_list[i - 1]
            signal_state = sig_by_date.get(prev_date)

            if signal_state is not None:
                new_pos = signal_state
            else:
                # 信号尚未生成，保持前一日仓位或初始化
                if position is None:
                    new_pos = 1 if growth_prices[i-1] / div_prices[i-1] > \
                                   (sum(ratios_list[:i]) / i if i > 0 else 1) else 0
                else:
                    new_pos = position

            if position is not None and position != new_pos:
                # 调仓 → 扣除手续费
                ret = d_ret if new_pos == 0 else g_ret
                nav.append(nav[-1] * ret * (1 - ONE_WAY_FEE))
            else:
                ret = d_ret if new_pos == 0 else g_ret
                nav.append(nav[-1] * ret)

            position = new_pos

        return nav

    print("  计算策略轮动收益(创业板指 vs 红利)...")
    cyb_prices_full = [cyb_map[d] for d in cyb_div_dates]
    div_prices_full = [div_map[d] for d in cyb_div_dates]
    cyb_strat_nav = calc_strategy_returns(cyb_prices_full, div_prices_full, cyb_div_dates)
    cyb_strat_dates = [dt.strptime(d, "%Y-%m-%d") for d in cyb_div_dates[-len(cyb_strat_nav):]]

    print("  计算策略轮动收益(科创50 vs 红利)...")
    kc50_prices_full = [kc50_map[d] for d in kc50_div_dates]
    div_prices_kc = [div_map[d] for d in kc50_div_dates]
    kc50_strat_nav = calc_strategy_returns(kc50_prices_full, div_prices_kc, kc50_div_dates)
    kc50_strat_dates = [dt.strptime(d, "%Y-%m-%d") for d in kc50_div_dates[-len(kc50_strat_nav):]]

    # ── 绘图 ──
    fig, ax = plt.subplots(figsize=(20, 8))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#0d1117")

    # 5条曲线 — 各自从最早可用日期开始
    l_div = ax.plot(div_bh_dt, div_bh_norm, color="#ff6b6b", linewidth=1.8, alpha=0.9,
                     label="中证红利 Buy&Hold")[0]
    l_cyb = ax.plot(div_bh_dt, cyb_bh_norm, color="#58a6ff", linewidth=1.5, alpha=0.8, linestyle="--",
                     label="创业板指 Buy&Hold")[0]
    l_kc50 = ax.plot(kc50_bh_dt, kc50_bh_norm, color="#4da6ff", linewidth=1.5, alpha=0.8, linestyle="--",
                      label="科创50 Buy&Hold")[0]

    l_cyb_s = ax.plot(cyb_strat_dates, cyb_strat_nav, color="#ffa94d", linewidth=2.5, alpha=1.0,
                       label="策略轮动(创业板指)")[0]

    l_kc50_s = ax.plot(kc50_strat_dates, kc50_strat_nav, color="#da77f2", linewidth=2.5, alpha=1.0,
                        label="策略轮动(科创50)")[0]

    # 基准线 1.0
    ax.axhline(y=1.0, color="#8b949e", linewidth=0.6, linestyle=":", alpha=0.5)

    # ── 信息框 (左上角) ──
    cyb_result = next((r for r in results if "创业板" in r.get("index_pair", "")), {})
    kc50_result = next((r for r in results if "科创50" in r.get("index_pair", "")), {})

    info_lines = [
        f"红利/创业板: {cyb_div_dates[0]} ~ {cyb_div_dates[-1]} ({len(cyb_div_dates)}天)",
        f"科创50: {kc50_div_dates[0]} ~ {kc50_div_dates[-1]} ({len(kc50_div_dates)}天)",
        f"策略: 缓冲均线 (带宽=1%, MA=20日, 单边费=0.1%)",
        f"当前信号: 创→{cyb_result.get('current_signal','?')}期 | 科→{kc50_result.get('current_signal','?')}期",
    ]
    info_text = "\n".join(info_lines)
    ax.text(0.02, 0.97, info_text, transform=ax.transAxes,
            fontsize=12, color="#8b949e", va="top", ha="left",
            bbox=dict(boxstyle="round,pad=0.5", facecolor="#161b22",
                       edgecolor="#30363d", alpha=0.9))

    # ── 最终收益排行 (右下角) ──
    # 解释: x1.24 意思是投入 1 元最终变成 1.24 元; +24% 是累计收益率
    rankings = [
        ("中证红利 Buy&Hold", div_bh_norm[-1], "#ff6b6b"),
        ("创业板指 Buy&Hold", cyb_bh_norm[-1], "#58a6ff"),
        ("科创50 Buy&Hold", kc50_bh_norm[-1], "#4da6ff"),
        ("策略轮动(创业板指)", cyb_strat_nav[-1], "#ffa94d"),
        ("策略轮动(科创50)", kc50_strat_nav[-1], "#da77f2"),
    ]
    rankings.sort(key=lambda x: x[1], reverse=True)  # 收益从高到低

    rank_lines = ["最终收益排行 (投入1元 → ?)"]
    medal = ["1st", "2nd", "3rd", "4th", "5th"]
    for i, (name, val, color) in enumerate(rankings):
        pct = (val - 1) * 100
        rank_lines.append(f"{medal[i]} {name}: {val:.2f}元 (+{pct:+.0f}%)")

    rank_text = "\n".join(rank_lines)
    ax.text(0.98, 0.97, rank_text, transform=ax.transAxes,
            fontsize=12, color="#e6edf3", va="top", ha="right",
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

    # 图例 (放在左下角, 避免与信息框重叠)
    legend = ax.legend(loc="lower left", framealpha=0.85, fontsize=13,
                       facecolor="#161b22", edgecolor="#30363d",
                       labelcolor="#e6edf3")
    for text in legend.get_texts():
        text.set_color("#e6edf3")

    plt.tight_layout()
    fig.savefig(save_path, dpi=150, facecolor="#0d1117", bbox_inches="tight")
    plt.close(fig)
    print(f"  合并收益图: {save_path}")
    return save_path


def save_report(result: dict, report_text: str):
    """保存报告"""
    date_str = datetime.now().strftime("%Y%m%d")
    (CACHE_DIR / f"report_{date_str}.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    (CACHE_DIR / f"report_{date_str}.txt").write_text(report_text, encoding="utf-8")


# ─── 合成图: 所有图表纵向拼接为一张大图 ─────────────────

def compose_all_charts(chart_files: list[tuple[str, str]], save_path: str,
                       results: list[dict] | None = None):
    """将多张图表纵向拼接为一张大图，每张图前加详细数据说明，顶部加摘要

    针对手机端阅读优化：使用大字号、每张图前提供数据驱动的多行描述
    """
    from PIL import Image, ImageDraw, ImageFont

    BORDER = 50          # 边距（加大）
    HEADER_HEIGHT = 1300 # 顶部持仓指令区（大幅扩大）
    BG_COLOR = (13, 17, 23)         # #0d1117
    CAPTION_COLOR = (230, 237, 243)  # #e6edf3
    SEP_COLOR = (48, 54, 61)         # #30363d
    SUBTLE_COLOR = (139, 148, 158)   # #8b949e
    RED_COLOR = (255, 107, 107)      # 红利/红色信号
    BLUE_COLOR = (88, 166, 255)       # 成长/蓝色信号
    GOLD_COLOR = (255, 200, 50)      # 持仓指令高亮色

    # ── 加载字体（跨平台：Windows / Linux）──
    font_paths = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",  # Linux (GitHub Actions)
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/msyh.ttc",       # 微软雅黑
        "C:/Windows/Fonts/msyhbd.ttc",     # 微软雅黑粗体
        "C:/Windows/Fonts/simhei.ttf",     # 黑体
        "C:/Windows/Fonts/simsun.ttc",     # 宋体
    ]

    def _load_font(size, bold=False):
        for fp in font_paths:
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
        return ImageFont.load_default()

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
        """根据图表类型和数据分析结果生成多行说明文字"""
        signal = r.get("current_signal", "?")
        ratio = r.get("current_ratio", 0)
        ma = r.get("current_ma", 0)
        diff = r.get("ratio_ma_diff_pct", 0)
        days = r.get("consecutive_days", 0)
        zone = r.get("current_zone", "")
        upper = r.get("current_upper", 0)
        lower = r.get("current_lower", 0)

        sig_emoji = "🔥" if signal == "成长" else "💤"
        sig_color = "蓝" if signal == "成长" else "红"

        lines = []
        if chart_type == "short":
            lines = [
                f"【{idx_pair}】短期趋势 — 近2个月细节",
                f"{sig_emoji} 当前信号：{signal}期（已持续 {days} 天）",
                f"   比值 {ratio:.4f}  |  20日均线 {ma:.4f}  |  偏离 {diff:+.2f}%",
                f"   上轨(MA×1.01) {upper:.4f}  |  下轨(MA×0.99) {lower:.4f}",
                f"   状态：{zone}",
                f"   △ 图上{sig_color}色标记 = 信号切换点，彩色底色 = 对应信号区间",
            ]
        elif chart_type == "long":
            lines = [
                f"【{idx_pair}】长期趋势 — 近3年全景",
                f"{sig_emoji} 当前信号：{signal}期（已持续 {days} 天）",
                f"   纵观3年走势，可以清晰看到成长/红利风格的完整轮动周期",
                f"   虚线 = 均线±1%缓冲带，实线 = 比值走势",
                f"   此图用于判断当前处于大周期的哪个位置，辅助决策",
            ]
        elif chart_type == "returns":
            cyb_r, kc50_r = results[0], results[1]
            if "error" not in cyb_r:
                lines = [
                    "【累计收益对比】5条策略曲线 — 覆盖全部可用历史数据",
                    "   1. 红色实线 = 一直持有中证红利（Buy & Hold）",
                    "   2. 蓝色虚线 = 一直持有创业板指",
                    "   3. 橙色虚线 = 一直持有科创50",
                    "   4. 蓝色粗实线 = 按本策略在创业板指与红利间切换",
                    "   5. 橙色粗实线 = 按本策略在科创50与红利间切换",
                    "   粗线高于细线 → 策略跑赢 Buy & Hold；反之跑输",
                ]
            else:
                lines = ["【累计收益对比】", "   收益数据生成中..."]
        return lines

    def _calc_caption_height(lines: list[str]) -> int:
        """根据行数估算说明区域高度"""
        line_h = 68  # font_body 行高（放大后）
        return 36 + len(lines) * line_h + 30  # 上下 padding

    # ── 计算总高度 ──
    total_h = HEADER_HEIGHT
    for i, (img, (title_key, _path)) in enumerate(zip(images, chart_files)):
        # 从 chart_files 的 title_key 推断是哪个图类型
        r_idx = 0
        if "科创" in title_key:
            r_idx = 1
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
    draw.text((BORDER, 25), "风格轮动每日持仓信号", fill=CAPTION_COLOR, font=font_title)
    draw.text((BORDER, 110),
              f"数据日期: {data_date}    策略: 1%缓冲均线 (MA=20日)",
              fill=SUBTLE_COLOR, font=font_sub)

    # ── 持仓指令（最醒目，放在最顶部显眼位置）──
    y = 190
    all_growth = all(r.get("current_signal") == "成长" for r in results if "error" not in r)
    all_dividend = all(r.get("current_signal") == "红利" for r in results if "error" not in r)

    if all_dividend:
        hold_text = "继续持有红利"
        hold_color = RED_COLOR
        hold_bg = (60, 20, 20)
    elif all_growth:
        hold_text = "继续持有成长"
        hold_color = BLUE_COLOR
        hold_bg = (20, 30, 60)
    else:
        hold_text = "两组信号不一致"
        hold_color = GOLD_COLOR
        hold_bg = (60, 50, 10)

    # 持仓指令背景框（大号高亮）
    box_h = 130
    draw.rectangle([(BORDER, y), (uniform_w - BORDER, y + box_h)],
                   fill=hold_bg)
    # 边框
    draw.rectangle([(BORDER, y), (uniform_w - BORDER, y + box_h)],
                   outline=hold_color, width=4)
    # 持仓指令文字（居中）
    draw.text((BORDER + 30, y + 20), hold_text, fill=hold_color, font=font_hold)

    y += box_h + 40
    draw.text((BORDER, y), "信号详情", fill=CAPTION_COLOR, font=font_section)
    y += 80

    # ── 两组指数对信号详情 ──
    for r in results:
        if "error" in r:
            continue
        pair_name = r.get("index_pair", "").split(" vs ")[0]
        signal = r.get("current_signal", "?")
        ratio = r.get("current_ratio", 0)
        ma = r.get("current_ma", 0)
        diff = r.get("ratio_ma_diff_pct", 0)
        days = r.get("consecutive_days", 0)
        zone = r.get("current_zone", "")
        upper = r.get("current_upper", 0)
        lower = r.get("current_lower", 0)
        text_color = BLUE_COLOR if signal == "成长" else RED_COLOR

        draw.text((BORDER, y), f"  {pair_name} vs 红利", fill=text_color, font=font_sub)
        y += 64
        draw.text((BORDER + 40, y), f"信号: {signal}期 (持续{days}天)  |  比值 {ratio:.4f}  |  均线 {ma:.4f}  |  偏离 {diff:+.2f}%",
                  fill=CAPTION_COLOR, font=font_body)
        y += 62
        draw.text((BORDER + 40, y), f"上轨 {upper:.4f}  |  下轨 {lower:.4f}  |  状态: {zone}",
                  fill=SUBTLE_COLOR, font=font_body)
        y += 76

    # ── 切换条件提醒 ──
    draw.text((BORDER, y), "切换条件提醒", fill=CAPTION_COLOR, font=font_section)
    y += 80
    for r in results:
        if "error" in r:
            continue
        pair_name = r.get("index_pair", "").split(" vs ")[0]
        signal = r.get("current_signal", "?")
        upper = r.get("current_upper", 0)
        lower = r.get("current_lower", 0)
        ratio = r.get("current_ratio", 0)
        if signal == "红利":
            dist = (upper - ratio) / ratio * 100
            cond = f"比值需涨至 {upper:.4f} (上轨) 切换成长 (还差 {dist:+.1f}%)"
            cond_color = BLUE_COLOR
        else:
            dist = (lower - ratio) / ratio * 100
            cond = f"比值需跌至 {lower:.4f} (下轨) 切换红利 (还差 {dist:+.1f}%)"
            cond_color = RED_COLOR
        draw.text((BORDER + 20, y), f"  {pair_name}: {cond}", fill=cond_color, font=font_body)
        y += 62

    # ── 底部提示 ──
    y += 15
    draw.text((BORDER, y), "  仅供研究参考，不构成投资建议", fill=SUBTLE_COLOR, font=font_small)
    y += 55
    draw.text((BORDER, y), "  ↓ 短期趋势（重点）→ 长期全景（参考）→ 累计收益（底部）",
              fill=SUBTLE_COLOR, font=font_small)

    y_offset = HEADER_HEIGHT

    # ════════════════════════════════════════════
    # 逐个图表 + 说明
    # ════════════════════════════════════════════
    for i, (img, (title_key, _path)) in enumerate(zip(images, chart_files)):
        r_idx = 0
        if "科创" in title_key:
            r_idx = 1
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

    for growth_key in ["growth_cyb", "growth_kc50"]:
        result = analyze(growth_key)
        report = format_report(result)
        brief = format_brief(result)
        print(report)
        all_results.append(result)
        all_briefs.append(brief)
        if "error" not in result:
            save_report(result, report)
        # 只收集创业板指的趋势图 (科创50趋势图不需要)
        if growth_key == "growth_cyb" and result.get("chart_paths"):
            name_map = {"short": "短期趋势 (近2月)", "long": "长期趋势 (近3年)"}
            for ctype, cpath in result["chart_paths"]:
                idx_name = result.get("index_pair", "未知").split(" vs ")[0]
                all_chart_files.append((f"{idx_name} — {name_map.get(ctype, ctype)}", cpath))

    # 生成合并收益对比图
    date_str = datetime.now().strftime("%Y%m%d")
    combined_returns_path = str(CACHE_DIR / f"chart_combined_returns_{date_str}.png")
    try:
        plot_combined_returns_chart(all_results, combined_returns_path)
        all_chart_files.append(("累计收益对比 — 5条策略曲线", combined_returns_path))
    except Exception as e:
        print(f"  合并收益图生成失败: {e}")
        combined_returns_path = None

    # ── 合成大图 ──
    composed_path = str(CACHE_DIR / f"chart_all_in_one_{date_str}.png")
    compose_all_charts(all_chart_files, composed_path, results=all_results)

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

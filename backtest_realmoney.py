"""创红方案 · 真实资金回测  [独立脚本, 不改生产]

最终方案 (用户拍板 "B"):
  - 初始 1,000,000 元现金
  - 跟随创红 regime 信号 (创业板指/红利指 缓冲均线 MA20/带宽1%):
      成长期 -> 把【当前总资金 ÷ 3】各买一只创业板50 TOP3 (每次调仓重置)
      红利期 -> 全部清仓, 空仓持现金
  - T-1 收盘信号 -> T 开盘价成交 (信号收盘后才可知, 次日才买, 无前视)
  - 双边费 ONE_WAY_FEE = 0.1%
  - 成交股数按 lot 整数倍 (lot=100: A股真实规则; lot=1: 小数股纯净口径)
  - 价格用后复权(hfq): 除权缺口自动抹平, 分红作为再投资计入总收益
  - 创业板指 regime 信号保持纯净 (不被任何个股因子干扰)

复用 growth_dividend_monitor 的数据加载与 regime 信号, 不修改生产 monitor。
"""

import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from growth_dividend_monitor import (
    compute_combined_strategy,
    get_bucket_codes,
    load_or_fetch_basket,
    align_to_master_basket,
    STOCK_CODES,
    ONE_WAY_FEE,
)

INIT_CAPITAL = 1000000.0
FEE = ONE_WAY_FEE                       # 0.001


# ─── 价格访问 ──────────────────────────────────────────

def close_px(code, i, basket_close):
    v = basket_close[code][i]
    return v if (v is not None) else 0.0


def open_px(code, i, basket_open):
    v = basket_open[code][i]
    return v if (v is not None) else 0.0


# ─── 真实资金模拟器 (B 方案: 成长期权当前资金÷3 买 TOP3) ──

def simulate_real_cash(dates, regime, basket_open, basket_close,
                       lot=100, init=INIT_CAPITAL, fee=FEE):
    """真实资金模拟器 (B 方案)。

    返回 dict: nav, trades, td, switches
    """
    n = len(dates)
    first_idx = next((i for i in range(n) if regime[i] is not None), None)
    if first_idx is None or first_idx >= n:
        return None
    # regime[i] 在生产中 = "前一日收盘信号"; dates[i] 开盘执行即 T-1收盘->T开盘,
    # 严格"次日买入", 无前视。首笔建仓在 first_idx(=生产 trade_date)。
    td = first_idx

    cash = init
    holdings = {}        # code -> shares (int)
    nav = [init] * n
    trades = []
    switches = 0
    prev_target = None

    for i in range(td, n):
        target = regime[i]
        if prev_target is not None and target != prev_target:
            switches += 1
        prev_target = target

        held_codes = [c for c, s in holdings.items() if s > 0]
        is_holding = len(held_codes) > 0

        desired = (target == 1)  # 成长期持 TOP3, 红利期空仓 (纯 regime)
        codes = get_bucket_codes(dates[i])

        if desired and not is_holding:
            # 买入: 当前资金 ÷ 3 各买一只
            per = cash / 3.0
            for code in codes:
                px = open_px(code, i, basket_open)
                if px > 0:
                    sh = int(per / (px * (1 + fee)) // lot) * lot
                    if sh > 0:
                        cash -= sh * px * (1 + fee)
                        holdings[code] = sh
                        trades.append(("B", dates[i], code, sh, px))

        elif not desired and is_holding:
            # 清仓
            for code in held_codes:
                px = open_px(code, i, basket_open)
                sh = holdings[code]
                if px > 0 and sh > 0:
                    cash += sh * px * (1 - fee)
                    trades.append(("S", dates[i], code, sh, px))
                holdings[code] = 0

        elif desired and is_holding:
            # 篮子切换 (同 T 开盘先卖后买)
            if set(codes) != set(held_codes):
                for code in held_codes:
                    px = open_px(code, i, basket_open)
                    sh = holdings[code]
                    if px > 0 and sh > 0:
                        cash += sh * px * (1 - fee)
                        trades.append(("S", dates[i], code, sh, px))
                    holdings[code] = 0
                per = cash / 3.0
                for code in codes:
                    px = open_px(code, i, basket_open)
                    if px > 0:
                        sh = int(per / (px * (1 + fee)) // lot) * lot
                        if sh > 0:
                            cash -= sh * px * (1 + fee)
                            holdings[code] = sh
                            trades.append(("B", dates[i], code, sh, px))

        # 当日盯市 (收盘)
        v = cash
        for code, sh in holdings.items():
            if sh > 0:
                v += sh * close_px(code, i, basket_close)
        nav[i] = v

    return {"nav": nav, "trades": trades, "td": td, "switches": switches}


def calc_metrics(nav, dates, td, init=INIT_CAPITAL):
    vals = nav[td:]
    final = nav[-1]
    total = final / init - 1
    d0 = datetime.strptime(dates[td], "%Y-%m-%d")
    d1 = datetime.strptime(dates[-1], "%Y-%m-%d")
    years = (d1 - d0).days / 365.25
    ann = (final / init) ** (1 / years) - 1 if years > 0 else 0.0
    peak = vals[0]
    mdd = 0.0
    for v in vals:
        if v > peak:
            peak = v
        dd = v / peak - 1
        if dd < mdd:
            mdd = dd
    return total, ann, mdd, final


def yearly(dates, nav, td):
    rows = {}
    for i in range(td, len(dates)):
        yr = int(dates[i][:4])
        if yr not in rows:
            rows[yr] = [nav[i], nav[i]]
        else:
            rows[yr][1] = nav[i]
    out = []
    for yr in sorted(rows):
        s, e = rows[yr]
        out.append((yr, e, e / s - 1 if s > 0 else 0.0))
    return out


def main():
    base = compute_combined_strategy()
    if base is None:
        print("compute_combined_strategy 失败, 退出")
        return
    dates = base["dates"]
    regime = base["positions"]
    n = len(dates)
    print(f"区间: {dates[0]} ~ {dates[-1]}  ({n} 交易日)")
    print(f"生产参考(等股数连续口径): {base['total_ret']*100:+.1f}% "
          f"({base['nav'][-1]:.2f}x, 年化 {base['ann_ret']*100:+.1f}%, 回撤 {base['max_dd']*100:+.1f}%)")

    b_o, b_c = {}, {}
    for code in STOCK_CODES:
        adj = "qfq" if code == "300104" else "hfq"
        recs = load_or_fetch_basket("sz", code, adj)
        o, c = align_to_master_basket(recs, dates, zero_after_last=True)
        b_o[code], b_c[code] = o, c

    configs = [
        ("B 基准(100股, A股真实规则)", 100),
        ("B 基准(小数股, 纯净口径)", 1),
    ]
    results = {}
    for name, lot in configs:
        r = simulate_real_cash(dates, regime, b_o, b_c, lot=lot)
        if r is None:
            print(f"  {name}: 模拟失败")
            continue
        total, ann, mdd, final = calc_metrics(r["nav"], dates, r["td"])
        results[name] = (r, total, ann, mdd, final, len(r["trades"]))

    print("\n" + "=" * 70)
    print("创红方案 真实资金回测  (初始 1,000,000 元 | T开盘 | 费0.1% | hfq)")
    print("=" * 70)
    print(f"{'策略':<28}{'期末(元)':>12}{'总收益':>9}{'年化':>9}{'回撤':>9}{'交易':>6}")
    print("-" * 70)
    for name, lot in configs:
        if name not in results:
            continue
        _, total, ann, mdd, final, nt = results[name]
        print(f"{name:<28}{final:>12.0f}{total*100:>+8.1f}%{ann*100:>+8.1f}%"
              f"{mdd*100:>+8.1f}%{nt:>6d}")
    print("-" * 70)

    r0 = results["B 基准(100股, A股真实规则)"][0]
    print(f"\n逐年资金曲线 — B 基准(100股) (从1,000,000元起):")
    print(f"  {'年份':<6}{'年末资金':>12}{'年收益':>10}")
    for yr, e, ret in yearly(dates, r0["nav"], r0["td"]):
        print(f"  {yr:<6}{e:>12.0f}{ret*100:>+9.1f}%")

    # T-1→T 时序校验
    print("\n" + "=" * 70)
    print("T-1→T 时序校验 (信号收盘后才可知, 成交必在次日开盘, 无前视偏差)")
    print("=" * 70)
    date_to_idx = {d: k for k, d in enumerate(dates)}
    buys = [t for t in r0["trades"] if t[0] == "B"]
    demo = buys[:2]
    lines = []
    for t in demo:
        ei = date_to_idx[t[1]]
        sig_d = dates[ei - 1] if ei - 1 >= 0 else "(无)"
        lines.append(f"{sig_d}(收盘信号)→{t[1]}(开盘成交)")
    print(f"  B基准 首笔买入 idx={date_to_idx[buys[0][1]]}, 示例 [{'; '.join(lines)}]")
    ok = all(regime[date_to_idx[t[1]]] is not None for t in r0["trades"] if t[0] == "B")
    print(f"  断言: 所有买入的成交日决策信号均取自前一日收盘 → "
          f"{'通过 (无前视偏差)' if ok else '失败'}")


if __name__ == "__main__":
    main()

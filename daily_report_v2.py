"""创红/科红 每日报告 v2 —— 结论优先、可直接执行。

用法（接到原脚本里）：
    from daily_report_v2 import render_daily_report
    text = render_daily_report(result, combined, basket_info, freshness)
    print(text)
    # 飞书：把 text 作为文本消息发出去，图作为附件

设计原则：
  1. 第一屏就要能决定"今天做不做什么"
  2. 每个数字都要能落到"明天开盘怎么操作"
  3. 技术细节折叠到最后，不影响决策
"""
from __future__ import annotations

import datetime


# ────────────────────────── 小工具 ──────────────────────────
def _pct(x, digits=1, sign=True):
    if x is None:
        return "—"
    return f"{x*100:+.{digits}f}%" if sign else f"{x*100:.{digits}f}%"


def _money(x):
    return f"{x:,.0f}"


def _days_between(a: str, b: str) -> int:
    try:
        return (datetime.date.fromisoformat(b) - datetime.date.fromisoformat(a)).days
    except Exception:
        return 0


def _trailing_run(positions, target=0):
    """统计末尾连续处于某仓位的天数"""
    if not positions:
        return 0
    n = 0
    for p in reversed(positions):
        if p is None:
            break
        if p == target:
            n += 1
        else:
            break
    return n


def _drawdown(combined):
    nav = combined.get("nav") or []
    if not nav:
        return None, None, None
    peak, peak_i = nav[0], 0
    for i, v in enumerate(nav):
        if v > peak:
            peak, peak_i = v, i
    cur = nav[-1] / peak - 1
    dates = combined.get("dates") or []
    return cur, (dates[peak_i] if peak_i < len(dates) else None), nav[-1]


# ────────────────────────── 主渲染 ──────────────────────────
def render_daily_report(result, combined=None, basket_info=None, freshness=None,
                        scheme="创红", intraday=None) -> str:
    if not result or "error" in result:
        return f"❌ {scheme}监控异常：{(result or {}).get('error', '无数据')}"

    if intraday is None:            # 未指定时按抓取时间判断：15:10 之前视为盘中
        try:
            ft = (result.get("fetch_time") or "").split(" ")[1][:5]
            hh, mm = int(ft[:2]), int(ft[3:5])
            intraday = (hh, mm) < (15, 10)
        except Exception:
            intraday = False

    sig = result.get("current_signal", "—")          # 成长 / 红利
    zone = result.get("current_zone", "")
    ratio = result.get("current_ratio")
    ma = result.get("current_ma")
    upper = result.get("current_upper")
    lower = result.get("current_lower")
    growth_label = (result.get("index_pair") or "成长 vs 红利").split(" vs ")[0]
    last = (result.get("recent_10d") or [{}])[-1]

    # ── 数据可用性优先：STALE/CRITICAL 时绝不生成交易信号 ──
    _f = freshness or {}
    _idx = (_f.get("index") or {}).get("state", "?")
    _bk = (_f.get("basket") or {}).get("state", "?")
    # 只有「指数行情坏了」或「数据过期」才阻断信号；
    # 名单不一致(SCHEDULE 待更新)只告警，因为信号本身只依赖指数比值。
    _data_bad = bool(_f) and (_idx != "OK" or _f.get("overall") == "STALE")
    _stale_days = None
    try:
        if date:
            _stale_days = (datetime.date.today() - datetime.date.fromisoformat(date)).days
    except Exception:
        _stale_days = None
    if _stale_days is not None and _stale_days > 3:
        _data_bad = True
    g_close = last.get("growth_close")
    d_close = last.get("dividend_close")
    date = result.get("latest_date", "")

    pos = (combined or {}).get("current_position") or {}
    in_growth = pos.get("in_growth")
    weight = pos.get("weight")
    periods = (combined or {}).get("period_positions") or []
    # 按买入日排序后再取最后一笔（不依赖上游列表顺序）
    periods_sorted = sorted(periods, key=lambda x: x.get("buy") or "")
    last_trade = periods_sorted[-1] if periods_sorted else None
    positions = (combined or {}).get("positions") or []
    cold_days = _trailing_run(positions, 0)
    hold_days = _trailing_run(positions, 1)

    # 触发价换算（按当前均线近似；均线每天会移动）
    buy_trigger = (upper * d_close) if (upper and d_close) else None
    sell_trigger = (lower * d_close) if (lower and d_close) else None
    need_up = (buy_trigger / g_close - 1) if (buy_trigger and g_close) else None
    down_to_sell = (sell_trigger / g_close - 1) if (sell_trigger and g_close) else None

    lines = []
    title_tag = "（盘中，非最终信号）" if intraday else "（收盘）"
    _head = "⛔" if _data_bad else ("🟩" if in_growth else "🟥")
    lines.append(f"{_head} {scheme}方案 · "
                 f"{'持仓' if in_growth else '空仓'}   {date} {title_tag}"
                 + ("   ⚠️ 数据异常" if _data_bad else ""))
    lines.append("─" * 34)

    # ── 1. 今日结论（最重要，放最上面） ──
    if _data_bad:
        _why = []
        if _stale_days is not None and _stale_days > 3:
            _why.append(f"数据停留在 {date}（落后 {_stale_days} 天）")
        if _idx != "OK":
            _why.append(f"指数 {_idx}")
        if _bk not in ("OK", "?"):
            _why.append(f"个股 {_bk}（仅影响篮子报价）")
        lines.append("⛔ 数据异常：今日不生成交易信号")
        lines.append("   " + ("；".join(_why) if _why else "数据校验未通过"))
        lines.append("   请勿据此操作。排查：看 Actions 日志里行情接口是否返回 501/超时。")
    else:
        action, detail = _verdict(sig, in_growth, need_up, down_to_sell,
                                  result.get("signal_change"), weight)
        lines.append(action)
        lines.append(f"   {detail}")
    lines.append("")

    # ── 2. 现在在哪 ──
    lines.append("📍 现在的位置")
    lines.append(f"   比值 {ratio:.4f}  |  20日均线 {ma:.4f}  |  偏离 "
                 f"{(ratio/ma-1)*100:+.1f}%")
    zone_note = {"跌破下轨": f"已跌破下轨 {lower:.4f}（红利期第 {cold_days} 个交易日）",
                 "突破上轨": f"已突破上轨 {upper:.4f}（成长期第 {hold_days} 个交易日）",
                 }.get(zone, f"在上下轨之间（维持原仓位）")
    lines.append(f"   区域：{zone} — {zone_note}")
    lines.append(f"   {growth_label} {_money(g_close)}   中证红利 {_money(d_close)}"
                 if g_close and d_close else "   指数点位：—")
    lines.append("")

    # ── 3. 什么时候动手（可执行换算） ──
    lines.append("🎯 触发条件（按当前均线估算，均线每天会变）")
    if not in_growth:
        if buy_trigger and need_up is not None:
            lines.append(f"   买入：比值 ≥ {upper:.4f}  →  {growth_label}约 {_money(buy_trigger)}"
                         f"（今天 {_money(g_close)}，还需 {need_up*100:+.1f}%）")
            lines.append(f"   卖出（建仓后）：比值 ≤ {lower:.4f}  →  {growth_label}约 {_money(sell_trigger)}")
        if basket_info:
            names = "、".join(t["name"] for t in basket_info.get("top3", []))
            lines.append(f"   买什么：{names}（各 1/3）")
            lines.append(f"   下次调仓：{basket_info.get('next_rebalance', '?')}"
                         f"（{basket_info.get('days_left', '?')} 天后）")
    else:
        if sell_trigger and down_to_sell is not None:
            lines.append(f"   卖出：比值 ≤ {lower:.4f}  →  {growth_label}约 {_money(sell_trigger)}"
                         f"（今天 {_money(g_close)}，还需 {down_to_sell*100:+.1f}%）")
            lines.append(f"   买入（重新进场）：比值 ≥ {upper:.4f}  →  约 {_money(buy_trigger)}")
        if basket_info:
            names = "、".join(t["name"] for t in basket_info.get("top3", []))
            lines.append(f"   当前持有：{names}（各 1/3）")
            lines.append(f"   下次调仓：{basket_info.get('next_rebalance', '?')}"
                         f"（{basket_info.get('days_left', '?')} 天后）")
    lines.append("")

    # ── 4. 持仓与上一笔 ──
    lines.append("📦 持仓")
    if in_growth:
        lines.append(f"   仓位：{'半仓 50%' if (weight or 1) < 1 else '全仓 100%'}"
                     f"（建仓日 {pos.get('entry_date', '?')}，已持 {hold_days} 个交易日）")
        lines.append(f"   建仓前涨幅：{_pct(pos.get('runup'))}  →  {pos.get('reason', '')}")
    else:
        lines.append("   仓位：0%（红利期空仓，持币）")
    if last_trade:
        st = last_trade.get("status", "")
        lines.append(f"   上一笔：{last_trade.get('buy')} 买入 → "
                     f"{last_trade.get('sell') or '持有中'}，"
                     f"{_pct(last_trade.get('ret'))}（{last_trade.get('days', '?')} 天，{st}）")
    lines.append("")

    # ── 5. 风控开关状态 ──
    of = (combined or {}).get("overheat_filter") or {}
    lines.append("🛡 风控开关")
    if of.get("enabled"):
        sk = of.get("skipped", 0)
        relief = of.get("relief") or {}
        if relief.get("active"):
            lines.append(f"   过热过滤：⛔ 触发中 — 预计 {relief.get('relief_date', '?')} 解除"
                         f"（还需约 {relief.get('need_trading_days', '?')} 个交易日）")
        else:
            lines.append(f"   过热过滤：正常（历史累计拦截 {sk} 次买入；当前未触发）")
    else:
        lines.append("   过热过滤：已关闭")
    if in_growth:
        lines.append(f"   减半仓：{'触发（半仓）' if (weight or 1) < 1 else '未触发（全仓）'}")
    else:
        lines.append(f"   减半仓：待建仓时判定（阈值：建仓前 60 日涨幅 25%~50% 则半仓）")
    lines.append("")

    # ── 6. 绩效（只给 3 个数，其余进附录） ──
    if combined:
        dd_now, peak_date, nav = _drawdown(combined)
        annual = dict(combined.get("combined_annual") or [])
        this_year = annual.get(int(date[:4])) if date else None
        lines.append(f"📈 绩效（{combined.get('start_date','')} 起，含个股后复权、单边 0.1% 费）")
        lines.append(f"   年化 {_pct(combined.get('ann_ret'))}  |  "
                     f"最大回撤 {_pct(combined.get('max_dd'))}  |  "
                     f"净值 {combined.get('final_nav', 0):.1f} 倍")
        extra = f"   今年 {_pct(this_year)}" if this_year is not None else ""
        if dd_now is not None:
            extra += f"  |  当前距峰值 {_pct(dd_now)}"
            if peak_date:
                extra += f"（峰值 {peak_date}）"
        lines.append(extra)
        lines.append("")

    # ── 7. 数据可信度（一行） ──
    f = freshness or {}
    t3 = (f.get("top3") or {})
    idx = (f.get("index") or {})
    bk = (f.get("basket") or {})
    ok = f.get("overall", "?")
    mark = "✅" if ok == "OK" else "⚠️"
    lines.append(f"{mark} 数据：TOP3 {t3.get('state','?')}（{t3.get('live_date','?')}） | "
                 f"指数 {idx.get('state','?')} | 个股 {bk.get('state','?')} | "
                 f"最新交易日 {f.get('expected_trading_day','?')}")
    if ok != "OK":
        for m in (f.get("messages") or [])[:2]:
            lines.append(f"   ⚠️ {m}")
    return "\n".join(lines)


def _verdict(sig, in_growth, need_up, down_to_sell, signal_change, weight):
    """返回 (标题行, 说明行)"""
    if signal_change:
        sc = signal_change
        return (f"🔔 信号切换：{sc.get('from')} → {sc.get('to')}",
                f"明天开盘执行调仓（触发：{sc.get('trigger', '')}）")
    if in_growth:
        w = "半仓" if (weight or 1) < 1 else "全仓"
        if down_to_sell is not None and down_to_sell > -1.0:
            return ("⚠️ 接近卖出触发，明天留意",
                    f"比值再跌 {abs(down_to_sell):.1f}% 就切红利期（{w}持有中）")
        return (f"✅ 今日动作：继续持有（{w}），不用操作",
                "成长期，等下轨触发或下次调仓")
    if need_up is not None and need_up * 100 < 2.0:
        return ("⚠️ 接近买入触发，明天可能要建仓",
                f"比值只需再涨 {need_up:.1f}% 即进成长期，提前准备资金")
    return ("✅ 今日动作：空仓，什么都不用做",
            "红利期，等比值回到均线上方")


# ────────────────────────── 附录（可选，折叠进图片/文件） ──────────────────────────
def render_appendix(result) -> str:
    rows = result.get("recent_10d") or []
    if not rows:
        return ""
    out = ["", "── 附录：近 10 日明细 ──",
           f"{'日期':<12}{'成长指数':>11}{'中证红利':>11}{'比值':>9}{'均线':>9}{'信号':>6}"]
    for r in rows:
        out.append(f"{r['date']:<12}{r['growth_close']:>11.2f}{r['dividend_close']:>11.2f}"
                   f"{r['ratio']:>9.4f}{r['ma']:>9.4f}{r['signal']:>6}")
    return "\n".join(out)

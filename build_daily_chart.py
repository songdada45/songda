"""每日「一张图」日报：两个方案合成一张图，飞书发一条富文本（图 + 几行字）。

用法：python build_daily_chart.py
依赖：matplotlib（CI 已装）、requests
"""
from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.gridspec import GridSpec  # noqa: E402
from matplotlib.patches import Rectangle  # noqa: E402

REPO = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO))

OUT_DIR = Path.cwd() / ".daily_digest"
OUT_DIR.mkdir(exist_ok=True)
PNG = OUT_DIR / "daily_dashboard.png"

C_BG = "#0f1117"
C_PANEL = "#161a22"
C_GRID = "#2a3040"
C_TXT = "#e6e8ee"
C_DIM = "#8b93a7"
C_GREEN = "#3ddc84"
C_RED = "#ff5c5c"
C_BLUE = "#5b9bff"
C_ORANGE = "#ffb020"
C_GRAY = "#6b7280"


# ───────────────────────── 字体 ─────────────────────────
def setup_font():
    cands = ["Microsoft YaHei", "Noto Sans CJK SC", "Noto Sans CJK JP",
             "WenQuanYi Zen Hei", "SimHei", "PingFang SC", "Source Han Sans SC"]
    have = {f.name for f in fm.fontManager.ttflist}
    for name in cands:
        if name in have:
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return name
    for p in ["/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
              "C:/Windows/Fonts/msyh.ttc"]:
        if os.path.exists(p):
            fm.fontManager.addfont(p)
            name = fm.FontProperties(fname=p).get_name()
            plt.rcParams["font.family"] = name
            plt.rcParams["axes.unicode_minus"] = False
            return name
    plt.rcParams["axes.unicode_minus"] = False
    return "default"


# ───────────────────────── 取数 ─────────────────────────
def collect(mod, scheme, growth_index_key="growth_cyb"):
    """从一个 monitor 模块里取绘图所需数据"""
    info = mod.INDICES[growth_index_key]
    div = mod.INDICES["dividend"]
    g = mod.get_index_data(info)
    d = mod.get_index_data(div)
    dates, gl, dl = mod.align_data(g, d)
    ratio = [a / b for a, b in zip(gl, dl)]
    ma = mod.calc_ma(ratio, mod.MA_WINDOW)
    sig = mod.calc_ma_band_signal(ratio, ma, mod.MA_BAND)

    rows, k = [], len(ratio) - len(sig)
    for j, s in enumerate(sig):
        i = k + j
        rows.append(dict(date=dates[i], ratio=ratio[i], ma=ma[i],
                         upper=s["upper"], lower=s["lower"],
                         state=s["state"], growth=gl[i], div=dl[i]))
    rows = rows[-260:]

    combined = None
    try:
        combined = mod.compute_combined_strategy()
    except Exception as e:  # noqa: BLE001
        print(f"[chart] {scheme} compute_combined 失败: {e}")

    basket = None
    try:
        basket = mod.get_current_basket_info()
    except Exception as e:  # noqa: BLE001
        print(f"[chart] {scheme} basket 失败: {e}")

    return dict(scheme=scheme, rows=rows, combined=combined, basket=basket,
                growth_name=info["name"], div_name=div["name"],
                band=mod.MA_BAND, window=mod.MA_WINDOW)


def verdict(d):
    rows = d["rows"]
    if not rows:
        return dict(last=None, in_growth=False, weight=0, pos={},
                    buy_trigger=0, need_up=0, sell_trigger=0, hold_days=0)
    last = rows[-1]
    in_growth = d["combined"] and (d["combined"].get("current_position") or {}).get("in_growth")
    pos = (d["combined"] or {}).get("current_position") or {}
    weight = pos.get("weight") or 0
    buy_trigger = last["upper"] * last["div"]
    need_up = buy_trigger / last["growth"] - 1 if last["growth"] else None
    sell_trigger = last["lower"] * last["div"]
    hold_days = 0
    if in_growth:
        for r in reversed(rows):
            if r["state"] == 1:
                hold_days += 1
            else:
                break
    return dict(last=last, in_growth=in_growth, weight=weight, pos=pos,
                buy_trigger=buy_trigger, need_up=need_up,
                sell_trigger=sell_trigger, hold_days=hold_days)


# ───────────────────────── 画图 ─────────────────────────
def draw_panel(fig, gs, d, v, title):
    sub = gs.subgridspec(1, 2, width_ratios=[1.75, 1], wspace=0.06)

    # ---- 左：比值走势 ----
    ax = fig.add_subplot(sub[0, 0])
    ax.set_facecolor(C_PANEL)
    for sp in ax.spines.values():
        sp.set_color(C_GRID)
    ax.tick_params(colors=C_DIM, labelsize=9)
    ax.grid(True, color=C_GRID, lw=0.5, alpha=0.45)

    rows = d["rows"]
    if not rows:
        ax.text(0.5, 0.5, "本方案今日无数据", color=C_DIM, fontsize=14,
                ha="center", va="center", transform=ax.transAxes)
        ax.set_xticks([]); ax.set_yticks([])
        ax.set_title(title, color=C_TXT, fontsize=13, loc="left", pad=8, fontweight="bold")
        return
    x = np.arange(len(rows))
    r = np.array([q["ratio"] for q in rows])
    ma = np.array([q["ma"] for q in rows])
    up = np.array([q["upper"] for q in rows])
    lo = np.array([q["lower"] for q in rows])
    st = np.array([q["state"] for q in rows])

    # regime 底色
    for i in range(len(rows)):
        col = C_GREEN if st[i] == 1 else C_RED
        ax.axvspan(i - 0.5, i + 0.5, color=col, alpha=0.07, lw=0)

    ax.fill_between(x, lo, up, color=C_BLUE, alpha=0.16, label="缓冲区")
    ax.plot(x, ma, color=C_ORANGE, lw=1.6, label=f"MA{d['window']}")
    ax.plot(x, up, color=C_ORANGE, lw=0.8, ls="--", alpha=0.7)
    ax.plot(x, lo, color=C_ORANGE, lw=0.8, ls="--", alpha=0.7)
    ax.plot(x, r, color=C_TXT, lw=2.0, label="比值")
    ax.scatter([x[-1]], [r[-1]], s=90, color=C_BLUE, zorder=5,
               edgecolor="white", linewidth=1.2)

    # 标注当前值
    ax.annotate(f"{r[-1]:.4f}", (x[-1], r[-1]), xytext=(-14, 26),
                textcoords="offset points", color=C_TXT, fontsize=11,
                ha="right", fontweight="bold")
    ax.set_xlim(-1, len(rows))
    pad = (r.max() - r.min()) * 0.12 + 1e-4
    ax.set_ylim(min(lo.min(), r.min()) - pad, max(up.max(), r.max()) + pad)
    ax.set_title(title, color=C_TXT, fontsize=13, loc="left", pad=8, fontweight="bold")
    ax.legend(loc="lower left", fontsize=7.5, facecolor=C_PANEL,
              edgecolor=C_GRID, labelcolor=C_TXT, ncol=2, framealpha=0.85)
    # x 轴只标首尾
    ax.set_xticks([0, len(rows) // 2, len(rows) - 1])
    ax.set_xticklabels([rows[0]["date"][5:], rows[len(rows) // 2]["date"][5:],
                        rows[-1]["date"][5:]], color=C_DIM, fontsize=8)

    # ---- 右：状态卡 ----
    ax2 = fig.add_subplot(sub[0, 1])
    ax2.set_facecolor(C_PANEL)
    ax2.set_xticks([]); ax2.set_yticks([])
    for sp in ax2.spines.values():
        sp.set_color(C_GRID)
    ax2.set_xlim(0, 1); ax2.set_ylim(0, 1)

    ok = bool(v["in_growth"])
    if ok:
        badge, bcol = ("满仓" if v["weight"] >= 1 else "半仓"), C_GREEN
    else:
        badge, bcol = "空仓", C_RED

    # 标题
    ax2.text(0.06, 0.95, badge, color=bcol, fontsize=30, fontweight="bold", va="top")
    ax2.text(0.06, 0.855, "成长期 · 持有前 3 权重" if ok else "红利期 · 持币等待",
             color=C_DIM, fontsize=10, va="top")

    # 状态条
    bar_x, bar_w, bar_y, bar_h = 0.06, 0.88, 0.70, 0.055
    for x0, w, col, alpha in [(bar_x, bar_w * 0.25, C_RED, 0.30),
                              (bar_x + bar_w * 0.25, bar_w * 0.5, C_GRAY, 0.35),
                              (bar_x + bar_w * 0.75, bar_w * 0.25, C_GREEN, 0.30)]:
        ax2.add_patch(Rectangle((x0, bar_y), w, bar_h, facecolor=col, alpha=alpha, lw=0))
    ax2.text(bar_x + bar_w * 0.125, bar_y - 0.035, "红利区", color=C_RED, fontsize=8, ha="center")
    ax2.text(bar_x + bar_w * 0.50, bar_y - 0.035, "缓冲区", color=C_DIM, fontsize=8, ha="center")
    ax2.text(bar_x + bar_w * 0.875, bar_y - 0.035, "成长区", color=C_GREEN, fontsize=8, ha="center")

    dev = v["last"]["ratio"] / v["last"]["ma"] - 1
    norm = 0.5 + 0.5 * (dev / d["band"]) / 2.0
    norm = min(max(norm, 0.03), 0.97)
    mx = bar_x + bar_w * norm
    ax2.plot([mx, mx], [bar_y - 0.012, bar_y + bar_h + 0.012], color=C_BLUE, lw=1.4, alpha=0.9)
    ax2.scatter([mx], [bar_y + bar_h / 2], s=120, color=C_BLUE, zorder=6,
                edgecolor="white", linewidth=1.2)
    # 「现在」标签：贴边时自动换到另一侧
    ax2.text(mx + (0.03 if norm < 0.5 else -0.03), bar_y + bar_h + 0.035, "现在",
             color=C_BLUE, fontsize=9, fontweight="bold",
             ha="left" if norm < 0.5 else "right")
    if norm <= 0.04 or norm >= 0.96:
        ax2.text(0.06, bar_y - 0.075,
                 "↓ 已远低于下轨（超出区间）" if norm <= 0.04 else "↑ 已远高于上轨（超出区间）",
                 color=C_DIM, fontsize=8.5)

    # 关键数字
    if not ok:
        ax2.text(0.06, 0.56, "距买入还差", color=C_DIM, fontsize=10)
        ax2.text(0.06, 0.475, f"{abs(v['need_up'])*100:.1f}%", color=C_GREEN,
                 fontsize=30, fontweight="bold")
        ax2.text(0.06, 0.405, f"买点 {d['growth_name']} {v['buy_trigger']:,.0f}"
                              f"（今天 {v['last']['growth']:,.0f}）", color=C_TXT, fontsize=10)
    else:
        need_down = v["last"]["growth"] / v["sell_trigger"] - 1
        ax2.text(0.06, 0.56, "距卖出还差", color=C_DIM, fontsize=10)
        ax2.text(0.06, 0.475, f"{abs(need_down)*100:.1f}%", color=C_RED,
                 fontsize=30, fontweight="bold")
        ax2.text(0.06, 0.405, f"卖点 {d['growth_name']} {v['sell_trigger']:,.0f}"
                              f"（今天 {v['last']['growth']:,.0f}）", color=C_TXT, fontsize=10)

    # 底部信息
    yy = 0.29
    if d["basket"]:
        names = "、".join(t["name"] for t in d["basket"].get("top3", []))
        ax2.text(0.06, yy, f"买什么：{names}", color=C_TXT, fontsize=9.5)
        ax2.text(0.06, yy - 0.075, f"下次调仓 {d['basket'].get('next_rebalance','?')}"
                                   f"（{d['basket'].get('days_left','?')} 天）",
                 color=C_DIM, fontsize=9)
    if ok and v["pos"].get("entry_date"):
        ax2.text(0.06, yy - 0.15,
                 f"已持有 {v['hold_days']} 个交易日 · 建仓前涨幅 "
                 f"{(v['pos'].get('runup') or 0)*100:+.0f}%", color=C_TXT, fontsize=9)
    elif not ok:
        ax2.text(0.06, yy - 0.15, "当前不持有任何标的", color=C_DIM, fontsize=9)
    cm = d.get("combined") or {}
    if cm:
        ann = cm.get("ann_ret")
        mdd = cm.get("max_dd")
        if ann is not None and mdd is not None:
            ax2.text(0.06, 0.055, f"回测年化 {ann*100:+.0f}% · 最大回撤 {mdd*100:.0f}%",
                     color=C_GRAY, fontsize=8)


def build(d1, d2, now=None):
    now = now or datetime.datetime.now()
    setup_font()
    fig = plt.figure(figsize=(12.5, 13.5), dpi=100, facecolor=C_BG)
    gs = GridSpec(4, 1, figure=fig, height_ratios=[0.55, 4.1, 4.1, 0.5],
                  hspace=0.16, left=0.04, right=0.97, top=0.975, bottom=0.02)

    axh = fig.add_subplot(gs[0, 0]); axh.axis("off")
    axh.text(0, 0.72, "大周期轮动 · 每日一图", color=C_TXT, fontsize=22,
             fontweight="bold", transform=axh.transAxes)
    empty = any(not d.get("rows") for d in (d1, d2))
    stale = any(d.get("stale") for d in (d1, d2))
    both_out = all(not verdict(d)["in_growth"] for d in (d1, d2)) and not empty
    both_in = all(verdict(d)["in_growth"] for d in (d1, d2))
    if empty:
        headline, hcol = "注意：有一路数据未更新，见下方标注", C_ORANGE
    elif stale:
        headline, hcol = "注意：数据未更新到最新交易日，仅供参考", C_ORANGE
    elif both_out:
        headline, hcol = "今天：两套方案都空仓，不用操作", C_RED
    elif both_in:
        headline, hcol = "今天：两套方案都持仓，继续持有", C_GREEN
    else:
        headline, hcol = "今天：一套持仓一套空仓，按各自条件执行", C_ORANGE
    axh.text(0, 0.30, f"{now:%Y-%m-%d}（周{'一二三四五六日'[now.weekday()]}）",
             color=C_DIM, fontsize=10, transform=axh.transAxes)
    axh.text(0.42, 0.30, headline, color=hcol, fontsize=12, fontweight="bold",
             transform=axh.transAxes)


    for i, (d, scheme_key) in enumerate([(d1, "①"), (d2, "②")]):
        v = verdict(d)
        title = (f"{scheme_key} {d['scheme']} · {d['growth_name']} vs {d['div_name']}"
                 + ("    [数据异常]" if d.get("data_bad") else ""))
        draw_panel(fig, gs[1 + i], d, v, title)

    # footer
    axf = fig.add_subplot(gs[3, 0]); axf.axis("off")
    axf.text(0, 0.55, f"更新 {now:%Y-%m-%d %H:%M}   ·   绿底＝满仓期，红底＝空仓期",
             color=C_DIM, fontsize=9, transform=axf.transAxes)
    axf.text(0, 0.10, "比值上穿上轨＝买入，下破下轨＝清仓；买卖点按均线估算，每天会变",
             color=C_GRAY, fontsize=8.5, transform=axf.transAxes)

    fig.savefig(PNG, facecolor=C_BG, dpi=100)
    plt.close(fig)
    print(f"[chart] 已生成 {PNG} ({PNG.stat().st_size/1024:.0f} KB)")
    return PNG


# ───────────────────────── 飞书 ─────────────────────────
def _feishu_token(app_id, app_secret):
    import requests
    r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                      json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
    d = r.json()
    return d.get("tenant_access_token") if d.get("code") == 0 else None


def already_sent_today(chat_id, key_text, app_id, app_secret):
    """当天是否已经发过同类消息（按标题关键字判断）。查不到就返回 False（宁可按未发处理）。"""
    try:
        import requests
        at = _feishu_token(app_id, app_secret)
        if not at:
            return False
        r = requests.get("https://open.feishu.cn/open-apis/im/v1/messages",
                         headers={"Authorization": f"Bearer {at}"},
                         params={"container_id_type": "chat", "container_id": chat_id,
                                 "sort_type": "ByCreateTimeDesc", "page_size": 20},
                         timeout=15)
        d = r.json()
        if d.get("code") != 0:
            print(f"[幂等] 查询消息失败（按未发处理）: {d.get('msg')}")
            return False
        import datetime as _dt
        _cn = _dt.timezone(_dt.timedelta(hours=8))
        for it in d.get("data", {}).get("items", []):
            body = it.get("body", {}).get("content", "")
            if key_text not in body:
                continue
            # 只把「收盘后（15:05 之后）」发的那条算数，盘中临时推的不拦正式报告
            try:
                ts = int(it.get("create_time", "0")) / 1000
                when = _dt.datetime.fromtimestamp(ts, _cn)
            except Exception:
                when = None
            if when is None or (when.hour, when.minute) >= (15, 5):
                print(f"[幂等] 今天收盘后已发过（{key_text} @ {when}），跳过")
                return True
            print(f"[幂等] 发现盘中那条（{when}），不算数，继续发送正式版")
        return False
    except Exception as e:  # noqa: BLE001
        print(f"[幂等] 检查异常（按未发处理）: {e}")
        return False


def feishu_send_post(png_path, title, lines):
    import requests
    app_id = os.environ.get("FEISHU_APP_ID", "cli_aae07cacb1785bdb")
    app_secret = os.environ.get("FEISHU_APP_SECRET", "")
    chat_id = os.environ.get("FEISHU_CHAT_ID", "oc_228f22564f13ddf89372bcbfb0513921")
    if not app_secret:
        print("[chart] 未配置 FEISHU_APP_SECRET，跳过发送")
        return False
    key = f"每日一图 {datetime.date.today():%m-%d}"
    if already_sent_today(chat_id, key,
                          os.environ.get("FEISHU_APP_ID", "cli_aae07cacb1785bdb"),
                          os.environ.get("FEISHU_APP_SECRET", "")):
        return True

    try:
        r = requests.post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                          json={"app_id": app_id, "app_secret": app_secret}, timeout=15)
        r.raise_for_status()
        tok = r.json()
        if tok.get("code") != 0:
            print("[chart] token 失败", tok); return False
        at = tok["tenant_access_token"]

        with open(png_path, "rb") as f:
            up = requests.post("https://open.feishu.cn/open-apis/im/v1/images",
                               headers={"Authorization": f"Bearer {at}"},
                               files={"image_type": (None, "message"),
                                      "image": (os.path.basename(png_path), f, "image/png")},
                               timeout=60)
        up.raise_for_status()
        ud = up.json()
        if ud.get("code") != 0:
            print("[chart] 上传图失败", ud); return False
        image_key = ud["data"]["image_key"]

        content = {"zh_cn": {"title": title,
                             "content": [[{"tag": "text", "text": ln}] for ln in lines]
                                        + [[{"tag": "img", "image_key": image_key}]]}}
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {at}"},
            json={"receive_id": chat_id, "msg_type": "post",
                  "content": json.dumps(content, ensure_ascii=False)}, timeout=30)
        resp.raise_for_status()
        rd = resp.json()
        if rd.get("code") != 0:
            print("[chart] 发送失败", rd); return False
        print("[chart] 发送成功 message_id:", rd.get("data", {}).get("message_id", "?"))
        return True
    except Exception as e:  # noqa: BLE001
        print("[chart] 异常:", e)
        return False


def load_json(name):
    p = OUT_DIR / name
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    # 适配绘图函数需要的结构
    return dict(
        scheme=d["scheme"], rows=d["rows"], growth_name=d["growth_name"],
        div_name=d["div_name"], band=0.01, window=20,
        combined={"current_position": d["position"],
                  "ann_ret": (d.get("perf") or {}).get("ann"),
                  "max_dd": (d.get("perf") or {}).get("mdd")},
        basket={"top3": [{"name": n} for n in (d.get("basket") or {}).get("names", [])],
                "next_rebalance": (d.get("basket") or {}).get("next"),
                "days_left": (d.get("basket") or {}).get("days")},
        data_bad=False,
        stale=(d.get("stale") or False),
    )


def main():
    d1 = load_json("01_创红.json")
    d2 = load_json("02_科红.json")
    if not d1 and not d2:
        print("[chart] 没有可用的绘图数据（两个 JSON 都缺失），退出由文字日报兜底")
        return 1
    # 缺一个时用占位，保证单方案也能出图
    for i, d in enumerate((d1, d2)):
        if d is None:
            d = dict(scheme="—", rows=[], growth_name="—", div_name="—", band=0.01,
                     window=20, combined=None, basket=None, data_bad=True)
            if i == 0:
                d1 = d
            else:
                d2 = d

    png = build(d1, d2)

    v1, v2 = verdict(d1), verdict(d2)
    d1["perf"] = d1.get("perf")
    def short(d, v):
        if not d["rows"]:
            return f"{d['scheme']}｜今日无数据"
        if v["in_growth"]:
            return (f"{d['scheme']}｜满仓，{d['growth_name']} 跌到 "
                    f"{v['sell_trigger']:,.0f} 就清仓"
                    f"（现 {v['last']['growth']:,.0f}）")
        return (f"{d['scheme']}｜{d['growth_name']} 涨到 "
                f"{v['buy_trigger']:,.0f} 才买，还差 {abs(v['need_up'])*100:.1f}%"
                f"（现 {v['last']['growth']:,.0f}）")

    date_s = datetime.date.today().strftime("%m-%d")
    if any(not d.get("rows") for d in (d1, d2)):
        title = f"{date_s} · 有方案数据未更新，仅图供参考"
    elif not v1["in_growth"] and not v2["in_growth"]:
        title = f"{date_s} · 两套都空仓，今天不用操作"
    elif v1["in_growth"] and v2["in_growth"]:
        title = f"{date_s} · 两套都持仓，继续拿着"
    else:
        title = f"{date_s} · 一套持仓一套空仓，各自按条件执行"
    lines = [short(d1, v1), short(d2, v2)]
    ok = feishu_send_post(str(png), title, lines)
    (OUT_DIR / "caption.txt").write_text(title + "\n" + "\n".join(lines), encoding="utf-8")
    return 0 if (ok or not os.environ.get("FEISHU_APP_SECRET")) else 1


if __name__ == "__main__":
    raise SystemExit(main())

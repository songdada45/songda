"""把两份 monitor 报告合并成「每天一条」飞书消息并发送。

用法：python send_daily_digest.py
环境变量：FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_CHAT_ID
输入：.daily_digest/01_创红.txt、.daily_digest/02_科红.txt（由两个 monitor 写入）
"""
from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path

import requests

DIGEST_DIR = Path.cwd() / ".daily_digest"
PARTS = [("① 创红 · 创业板指 vs 中证红利", "01_创红.txt", "创红"),
         ("② 科红 · 科创50 vs 中证红利", "02_科红.txt", "科红")]

APP_ID = os.environ.get("FEISHU_APP_ID", "cli_aae07cacb1785bdb")
APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
CHAT_ID = os.environ.get("FEISHU_CHAT_ID", "oc_228f22564f13ddf89372bcbfb0513921")


def _strip(title: str) -> str:
    """去掉分隔线、附录明细，压缩空行，保留决策正文"""
    out = []
    for l in title.splitlines():
        if l.startswith("───"):
            continue
        if "附录" in l:          # 附录（近10日明细）不进消息，太长
            break
        out.append(l.rstrip())
    while out and not out[0].strip():
        out.pop(0)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _status_line(part: str) -> str:
    """从报告里提取一行状态，用于顶部速览"""
    head = part.splitlines()[0] if part.splitlines() else ""
    m = re.search(r"(空仓|持仓)", head)
    pos = m.group(1) if m else "?"
    act = ""
    for l in part.splitlines():
        s = l.strip()
        if s.startswith(("✅", "⚠️", "🔔", "⛔")):
            act = s
            break
    return f"{pos}｜{act}" if act else pos


def _daily_line(parts: dict) -> str:
    bits = []
    for _, fname, short in PARTS:
        txt = parts.get(fname)
        if not txt:
            bits.append(f"{short}：未生成")
            continue
        st = _status_line(txt)
        m2 = re.search(r"还需 ([+\-]\d+\.\d+%)", txt)
        extra = f" · 距买入 {m2.group(1)}" if m2 else ""
        bits.append(f"{short} {st.split('｜')[0]}{extra}")
    return " ｜ ".join(bits)


def compose(parts: dict) -> str:
    now = datetime.datetime.now()
    wd = "一二三四五六日"[now.weekday()]
    L = []
    L.append(f"📊 大周期轮动日报 · {now:%Y-%m-%d}（周{wd}）")
    L.append("═" * 30)
    L.append(_daily_line(parts))
    L.append("")
    for title, fname, _ in PARTS:
        txt = parts.get(fname)
        L.append(f"【{title}】")
        if txt:
            L.append(_strip(txt).strip())
        else:
            L.append("⚠️ 本次未生成（脚本异常，见 Actions 日志）")
        L.append("")
    L.append("─" * 30)
    L.append("口径：个股后复权、单边 0.1% 费；净值起点 创红 2017-01 / 科红 2020-07")
    L.append("图表与明细：Actions 运行页 → Artifacts")
    return "\n".join(L)


def send_text(text: str) -> bool:
    if not APP_SECRET:
        print("[digest] 未配置 FEISHU_APP_SECRET，跳过发送")
        print(text[:500])
        return False
    try:
        r = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": APP_ID, "app_secret": APP_SECRET}, timeout=15)
        r.raise_for_status()
        td = r.json()
        if td.get("code") != 0:
            print("[digest] 取 token 失败:", td)
            return False
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
            headers={"Authorization": f"Bearer {td['tenant_access_token']}"},
            json={"receive_id": CHAT_ID, "msg_type": "text",
                  "content": json.dumps({"text": text}, ensure_ascii=False)}, timeout=20)
        resp.raise_for_status()
        rd = resp.json()
        if rd.get("code") != 0:
            print("[digest] 发送失败:", rd)
            return False
        print("[digest] 发送成功 message_id:", rd.get("data", {}).get("message_id", "?"))
        return True
    except Exception as e:  # noqa: BLE001
        print("[digest] 异常:", e)
        return False


def main():
    DIGEST_DIR.mkdir(exist_ok=True)
    parts = {}
    for _, fname, _ in PARTS:
        p = DIGEST_DIR / fname
        if p.exists():
            parts[fname] = p.read_text(encoding="utf-8")
    if not parts:
        msg = (f"⚠️ 大周期轮动日报 · {datetime.date.today():%Y-%m-%d}\n"
               "今日两份报告都未生成，请检查 Actions 日志（大概率是行情接口不可用）。")
        (DIGEST_DIR / "00_失败告警.txt").write_text(msg, encoding="utf-8")
        send_text(msg)
        return 0

    text = compose(parts)
    out = DIGEST_DIR / "daily_digest.txt"
    out.write_text(text, encoding="utf-8")
    print(f"[digest] 合并完成 {len(text)} 字符 → {out}")
    send_text(text)
    print(f"[digest] 本次共发送 1 条消息")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

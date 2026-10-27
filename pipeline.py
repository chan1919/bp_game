# -*- coding: utf-8 -*-
"""期货基差表数据采集管道.

思路:
  1. 从新浪期货品种表拿到全部商品品种 (名称 / mark);
  2. 每个品种拿到当前挂牌的全部合约, 从合约代码反推品种代码
     (如 白糖 的 mark 是 bst_qh, 但合约代码是 SR2701);
  3. 从最近月份开始逐月往前试探日线, 找到"最近一个已完成交割"的合约,
     取最后交易日结算价作为"最近交割价"(基准);
  4. 其余活跃合约取近 1 年日线结算价, 与基准计算基差 / 基差率.

基差   = 交割价 - 合约结算价   (现货视角, 期货升水时为负)
基差率 = 基差 / 交割价 * 100%

输出:
  data/index.json      品种索引 + 每个品种的合约阶梯 (一览页直接读取)
  data/<CODE>.json     单品种历史明细 (价格序列, 备用)

注意: 只做商品期货. 贵金属、中金所(股指/国债) 已剔除,
      见 EXCLUDE_PREFIX / EXCLUDE_EXCHANGE. 集运指数(欧线)属于商品指数期货, 保留.
"""
import argparse
import json
import os
import re
import threading
import time
import traceback
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

CN_TZ = timezone(timedelta(hours=8))
OUT_DIR = "data"
WINDOW_DAYS = 365          # 展示窗口: 近 1 年
MAX_MONTHS_BACK = 30       # 往前试探交割合约的最大月数
MIN_LIVE = 3               # 后续活跃合约太少则不展示
MIN_BARS = 20              # 时间轴太短则不展示
MIN_VOLUME = 50            # 单合约日成交量低于此值 ...
MIN_POSITION = 200         # ... 且持仓量也低于此值, 视为僵尸合约剔除
WORKERS = 8
TIMEOUT = 15
RETRY = 3

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://finance.sina.com.cn/",
}

VARIETY_JS = ("https://vip.stock.finance.sina.com.cn/quotes_service/view/js/"
              "qihuohangqing.js")
QUOTE_API = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/"
             "json_v2.php/Market_Center.getHQFuturesData")
KLINE_API = ("https://stock.finance.sina.com.cn/futures/api/jsonp.php/"
             "var%20_{sym}=/InnerFuturesNewService.getDailyKLine?symbol={sym}")

EX_NAME = {
    "czce": "郑商所",
    "dce": "大商所",
    "shfe": "上期所",
    "cffex": "中金所",
    "gfex": "广期所",
    "ine": "上期能源",
}
EX_ORDER = ["dce", "czce", "shfe", "ine", "gfex"]

# 新浪把能源中心的品种挂在 shfe 下, 这里按代码归位
INE_PREFIX = {"SC", "NR", "LU", "BC", "EC"}
INE_NAME = "上海国际能源交易中心"

# 中金所全部为金融期货(股指/国债), 不参与
EXCLUDE_EXCHANGE = {"cffex"}
# 按品种代码剔除: 贵金属 + 已停止交易/无成交的僵尸品种
EXCLUDE_PREFIX = {
    "AU",   # 黄金
    "AG",   # 白银
    "PT",   # 铂
    "PD",   # 钯
    "ZC",   # 动力煤 (无成交)
    "WH",   # 强麦 (无成交)
    "PM",   # 普麦 (无成交)
    "JR",   # 粳稻 (无成交)
    "RI",   # 早籼稻 (无成交)
    "LR",   # 晚籼稻 (无成交)
    "WR",   # 线材 (无成交)
    "BB",   # 胶合板 (无成交)
    "FB",   # 纤维板 (无成交)
    "RS",   # 菜籽 (无成交)
}

_local = threading.local()


def http():
    """每线程一个 Session."""
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update(HEADERS)
        _local.s = s
    return s


def get(url, **kw):
    last = None
    for i in range(RETRY):
        try:
            r = http().get(url, timeout=TIMEOUT, **kw)
            if r.status_code == 200:
                return r
            last = RuntimeError("HTTP %s" % r.status_code)
        except Exception as e:          # noqa: BLE001
            last = e
        time.sleep(0.6 * (i + 1))
    raise last


# ---------------- 品种列表 ----------------
def fetch_varieties():
    """返回 [{mark, name, exchange, exchange_short, ex}]."""
    r = get(VARIETY_JS)
    text = r.content.decode("gb2312", "ignore")
    # 去掉 // 注释 (渤海/昆明/全球期货等已下线板块被注释掉)
    text = "\n".join(re.sub(r"//.*$", "", ln) for ln in text.splitlines())
    parts = re.split(r"\b(czce|dce|shfe|cffex|gfex|ine)\s*:", text)
    out, seen = [], set()
    for i in range(1, len(parts) - 1, 2):
        ex, body = parts[i], parts[i + 1]
        if ex in EXCLUDE_EXCHANGE:
            continue
        m = re.match(r"\s*\[\s*'([^']+)'", body)
        full = m.group(1) if m else ex
        for name, mark in re.findall(r"\['([^']+)',\s*'([a-zA-Z]+_qh)'", body):
            mark = mark.lower()
            if mark in seen:
                continue
            seen.add(mark)
            out.append({
                "mark": mark,
                "name": name.strip(),
                "exchange": full,
                "exchange_short": EX_NAME.get(ex, ex.upper()),
                "ex": ex,
            })
    return out


def load_pool(path="README.md"):
    """README 的 `## 品种` 小节可维护品种池: 每行 `代码 [名称]`.
    返回 None 表示使用全部品种."""
    if not os.path.exists(path):
        return None
    codes, inside = [], False
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s.startswith("##"):
                inside = s.lstrip("#").strip() == "品种"
                continue
            if not inside or not s:
                continue
            s = s.lstrip("-*").strip()
            if not s or s.startswith("#"):
                continue
            tok = s.split()[0].strip().upper()
            if re.match(r"^[A-Z]{1,3}$", tok):
                codes.append(tok)
    return codes or None


# ---------------- 行情 ----------------
def fetch_active_contracts(mark):
    """该品种当前挂牌的全部合约 (含已交割但仍在列表中的僵尸合约)."""
    r = get(QUOTE_API, params={
        "page": "1", "num": "100", "sort": "position",
        "asc": "0", "node": mark, "base": "futures",
    })
    try:
        data = r.json()
    except Exception:
        return []
    return data if isinstance(data, list) else []


def fetch_kline(symbol):
    """日线: [{d,o,h,l,c,v,p,s}]."""
    r = get(KLINE_API.format(sym=symbol))
    text = r.text
    i, j = text.find("("), text.rfind(")")
    if i < 0 or j <= i:
        return []
    try:
        bars = json.loads(text[i + 1:j])
    except Exception:
        return []
    return bars if isinstance(bars, list) else []


def ym_of(symbol):
    """JD2701 -> 2701; 兼容 3 位老代码."""
    m = re.search(r"(\d{3,4})$", symbol)
    if not m:
        return None
    d = m.group(1)
    return int(d) if len(d) == 4 else int("2" + d)


def ym_prev(ym):
    y, m = divmod(ym, 100)
    return (y - 1) * 100 + 12 if m == 1 else y * 100 + (m - 1)


def ym_label(ym):
    return "%04d-%02d" % (2000 + ym // 100, ym % 100)


def today_ym(now):
    return now.year * 100 + now.month


def num(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd)


def contract_code(prefix, ym):
    return "%s%02d%02d" % (prefix, ym // 100 % 100, ym % 100)


# ---------------- 单品种 ----------------
def build_variety(v, latest_date, now, log):
    rows = fetch_active_contracts(v["mark"])
    if not rows:
        return None, None

    # 合约代码反推品种代码: 白糖 mark 是 bst_qh, 合约是 SR2701
    prefs = Counter()
    for x in rows:
        m = re.match(r"^([A-Za-z]+)(\d{3,4})$", (x.get("symbol") or "").strip())
        if m:
            prefs[m.group(1).upper()] += 1
    if not prefs:
        return None, None
    prefix = prefs.most_common(1)[0][0]
    if prefix in EXCLUDE_PREFIX:
        return None, None

    if prefix in INE_PREFIX:
        v = dict(v, ex="ine", exchange=INE_NAME, exchange_short="上期能源")

    contracts = {}
    for x in rows:
        sym = (x.get("symbol") or "").strip().upper()
        if not re.match(r"^%s\d{3,4}$" % prefix, sym):
            continue
        ym = ym_of(sym)
        if not ym or ym in (0, 1):
            continue
        contracts[sym] = {"code": sym, "ym": ym, "month": ym_label(ym)}
    if len(contracts) < MIN_LIVE:
        log("%s %-8s 合约太少(%d)" % (prefix, v["name"], len(contracts)))
        return None, None

    # ---- 找最近已交割合约: 从最小月份往前逐月试探 ----
    first_ym = min(c["ym"] for c in contracts.values())
    cur_ym = today_ym(now)
    delivery = None
    ym = first_ym
    for _ in range(MAX_MONTHS_BACK):
        sym = contract_code(prefix, ym)
        bars = fetch_kline(sym)
        if bars:
            last = bars[-1]
            last_date = last.get("d") or ""
            pos = num(last.get("p"), 0) or 0
            # 已交割: 最后一根日线早于全市场最新交易日, 或持仓已归零
            expired = bool(last_date and latest_date and last_date < latest_date) \
                or (pos == 0 and ym <= cur_ym)
            if expired:
                # 新浪对已交割合约会追加 p=0 的占位行(价格异常),
                # 取最后一个仍有持仓的交易日作为"最后交易日"
                real = last
                for b in reversed(bars):
                    if (num(b.get("p"), 0) or 0) > 0:
                        real = b
                        break
                price = num(real.get("s")) or num(real.get("c"))
                if not price or price <= 0:
                    break
                delivery = {
                    "contract": sym,
                    "ym": ym,
                    "price": price,
                    "date": real.get("d") or last_date,
                    "close": num(real.get("c")),
                    "settlement": num(real.get("s")),
                    "volume": int(float(real.get("v") or 0)),
                }
                break
        ym = ym_prev(ym)
    if not delivery:
        log("%s %-8s 未找到交割合约" % (prefix, v["name"]))
        return None, None

    base = delivery["price"]

    # ---- 时间轴: 近 1 年, 取全部活跃合约的并集 ----
    end = latest_date or delivery["date"]
    start = (datetime.strptime(end, "%Y-%m-%d")
             - timedelta(days=WINDOW_DAYS)).strftime("%Y-%m-%d")

    live = sorted((c for c in contracts.values() if c["ym"] > delivery["ym"]),
                  key=lambda c: c["ym"])
    if len(live) < MIN_LIVE:
        log("%s %-8s 后续合约太少(%d)" % (prefix, v["name"], len(live)))
        return None, None

    series, dates = [], set()
    for c in live:
        bars = fetch_kline(c["code"])
        if not bars:
            continue
        px = {}
        for b in bars:
            d = b.get("d") or ""
            if d < start:
                continue
            val = num(b.get("s"))
            if val is None or val <= 0:
                val = num(b.get("c"))
            if val is not None:
                px[d] = val
        if len(px) < 3:
            continue
        days = sorted(px)
        last_bar = None
        for b in reversed(bars):
            if (b.get("d") or "") in px:
                last_bar = b
                break
        if not last_bar:
            continue
        last_px = px[last_bar["d"]]
        prev_px = px[days[-2]] if len(days) > 1 else None
        c.update({
            "last": last_px,
            "last_date": last_bar["d"],
            "prev": prev_px,
            "chg": None if prev_px is None else round(last_px - prev_px, 2),
            "chg_pct": None if not prev_px else round(
                (last_px - prev_px) / prev_px * 100, 2),
            "volume": int(float(last_bar.get("v") or 0)),
            "position": int(float(last_bar.get("p") or 0)),
            "px": px,
        })
        series.append(c)
        dates.update(px)

    if len(series) < MIN_LIVE:
        log("%s %-8s 有效合约太少(%d)" % (prefix, v["name"], len(series)))
        return None, None

    # 剔除僵尸合约: 成交量和持仓量同时很低才剔除,
    # 避免误伤持仓大但当天成交少的远月套保合约
    liquid = [c for c in series
              if c["volume"] >= MIN_VOLUME or c["position"] >= MIN_POSITION]
    if len(liquid) < MIN_LIVE:
        log("%s %-8s 活跃合约太少(%d)" % (prefix, v["name"], len(liquid)))
        return None, None
    if len(liquid) < len(series):
        log("%s %-8s 剔除僵尸合约 %d 个" % (
            prefix, v["name"], len(series) - len(liquid)))
        series = liquid

    axis = sorted(dates)
    if len(axis) < MIN_BARS:
        log("%s %-8s 时间轴过短(%d)" % (prefix, v["name"], len(axis)))
        return None, None

    out = []
    for c in series:
        basis = round(base - c["last"], 2)
        out.append({
            "code": c["code"],
            "month": c["month"],
            "ym": c["ym"],
            "last": c["last"],
            "last_date": c["last_date"],
            "prev": c["prev"],
            "chg": c["chg"],
            "chg_pct": c["chg_pct"],
            "volume": c["volume"],
            "position": c["position"],
            "basis": basis,
            "basis_rate": round(basis / base * 100, 2),
            "prices": [c["px"].get(d) for d in axis],
        })

    detail = {
        "code": prefix,
        "name": v["name"],
        "exchange": v["exchange"],
        "exchange_short": v["exchange_short"],
        "ex": v["ex"],
        "update_time": now.strftime("%Y-%m-%d %H:%M"),
        "latest_trade_date": latest_date,
        "window": {"start": axis[0], "end": axis[-1], "days": WINDOW_DAYS},
        "delivery": delivery,
        "dates": axis,
        "contracts": out,
    }
    summary = {
        "code": prefix,
        "name": v["name"],
        "exchange": v["exchange"],
        "exchange_short": v["exchange_short"],
        "ex": v["ex"],
        "delivery_contract": delivery["contract"],
        "delivery_price": base,
        "delivery_date": delivery["date"],
        "contracts": len(out),
        "front": out[0]["code"],
        "front_basis_rate": out[0]["basis_rate"],
        # 一览页需要的合约阶梯, 省去前端再逐个加载明细文件
        "chain": [{
            "code": c["code"],
            "month": c["month"],
            "ym": c["ym"],
            "last": c["last"],
            "chg": c["chg"],
            "chg_pct": c["chg_pct"],
            "basis": c["basis"],
            "basis_rate": c["basis_rate"],
            "volume": c["volume"],
            "position": c["position"],
        } for c in out],
    }
    log("%s %-8s 交割 %s @%s | 活跃 %d" % (
        prefix, v["name"], delivery["contract"], base, len(out)))
    return detail, summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑指定品种代码, 逗号分隔")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    now = datetime.now(CN_TZ)
    pool = load_pool()

    lock = threading.Lock()

    def log(msg):
        with lock:
            print(msg, flush=True)

    log("拉取品种列表 ...")
    varieties = fetch_varieties()
    log("新浪共 %d 个品种 (已排除中金所)" % len(varieties))
    if args.only:
        want = {s.strip().upper() for s in args.only.split(",") if s.strip()}
        varieties = [v for v in varieties
                     if v["mark"].split("_")[0].upper() in want]
    if args.limit:
        varieties = varieties[:args.limit]

    # 最近交易日: 取实时快照 tradedate 最大值
    latest_date = ""
    for v in varieties:
        try:
            rows = fetch_active_contracts(v["mark"])
        except Exception:
            continue
        for x in rows:
            d = x.get("tradedate") or ""
            if d > latest_date and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                latest_date = d
        if latest_date:
            break
    if not latest_date:
        latest_date = now.strftime("%Y-%m-%d")
    log("最近交易日: %s" % latest_date)

    results, failed, seen = [], [], set()
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(build_variety, v, latest_date, now, log): v
                for v in varieties}
        for f in futs:
            try:
                detail, summary = f.result()
            except Exception as e:      # noqa: BLE001
                log("%s 失败: %s" % (futs[f]["name"], e))
                failed.append(futs[f]["name"])
                continue
            if not detail or detail["code"] in seen:
                continue
            seen.add(detail["code"])
            results.append((detail, summary))

    # 排序: 交易所 -> 品种代码
    ex_rank = {k: i for i, k in enumerate(EX_ORDER)}
    results.sort(key=lambda x: (ex_rank.get(x[0]["ex"], 99), x[0]["code"]))

    if pool:
        have = {d["code"] for d, _ in results}
        lost = [c for c in pool if c not in have]
        order = {c: i for i, c in enumerate(pool)}
        results = [r for r in results if r[0]["code"] in order]
        results.sort(key=lambda x: order[x[0]["code"]])
        if lost:
            log("README 池中没有数据: %s" % ",".join(lost))

    for detail, _ in results:
        path = os.path.join(OUT_DIR, "%s.json" % detail["code"])
        with open(path, "w", encoding="utf-8") as f:
            json.dump(detail, f, ensure_ascii=False, separators=(",", ":"))

    index = {
        "update_time": now.strftime("%Y-%m-%d %H:%M"),
        "latest_trade_date": latest_date,
        "window_days": WINDOW_DAYS,
        "basis_formula": "交割价 - 合约结算价",
        "total": len(results),
        "failed": failed,
        "varieties": [s for _, s in results],
    }
    with open(os.path.join(OUT_DIR, "index.json"), "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, separators=(",", ":"))

    log("完成 %d 个品种, 失败 %d 个, 用时 %.1fs" % (
        len(results), len(failed), time.time() - t0))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise

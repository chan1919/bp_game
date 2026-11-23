#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""抓取生意社「现期表」的现货参考价, 写入 data/spot.json.

页面: https://www.100ppi.com/sf/
- 只保留本站品种池里有的代码, 没有参考价的品种直接不写.
- 玻璃/烧碱/鸡蛋/生猪的现货单位与期货不同, 用页面自带的「现期差」
  折算成期货口径 (price), 原始现货价放在 raw/unit 里.
- 抓不到或解析失败时保留旧文件, 不阻断每日行情更新.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

URL = "https://www.100ppi.com/sf/"
OUT = os.path.join("data", "spot.json")
CN_TZ = timezone(timedelta(hours=8))
TIMEOUT = 30

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://www.100ppi.com/",
}

# 生意社商品名 -> 本站品种代码
NAME_TO_CODE = {
    "铜": "CU",
    "螺纹钢": "RB",
    "锌": "ZN",
    "铝": "AL",
    "燃料油": "FU",
    "天然橡胶": "RU",
    "铅": "PB",
    "石油沥青": "BU",
    "热轧卷板": "HC",
    "镍": "NI",
    "锡": "SN",
    "纸浆": "SP",
    "不锈钢": "SS",
    "丁二烯橡胶": "BR",
    "PTA": "TA",
    "白糖": "SR",
    "棉花": "CF",
    "菜籽油OI": "OI",
    "玻璃": "FG",
    "菜籽粕": "RM",
    "硅铁": "SF",
    "锰硅": "SM",
    "甲醇MA": "MA",
    "棉纱": "CY",
    "尿素": "UR",
    "纯碱": "SA",
    "涤纶短纤": "PF",
    "PX": "PX",
    "烧碱": "SH",
    "瓶片": "PR",
    "丙烯": "PL",
    "棕榈油": "P",
    "聚氯乙烯": "V",
    "聚乙烯": "L",
    "豆粕": "M",
    "豆油": "Y",
    "玉米": "C",
    "焦炭": "J",
    "焦煤": "JM",
    "铁矿石": "I",
    "鸡蛋": "JD",
    "聚丙烯": "PP",
    "乙二醇": "EG",
    "苯乙烯": "EB",
    "液化石油气": "PG",
    "生猪": "LH",
    "纯苯": "BZ",
    "工业硅": "SI",
    "碳酸锂": "LC",
    "多晶硅": "PS",
}

# 现货单位/期货口径不一致的品种:
# code -> (展示单位, 原始单位, 备注)
SPECIAL = {
    "FG": ("元/吨", "元/㎡", "现货按元/平方米，已按 80 ㎡/吨折成元/吨"),
    "SH": ("元/吨", "元/吨", "现货为 32% 液碱，已折成 100% 干吨"),
    "JD": ("元/500千克", "元/公斤", "现货按元/公斤，已折成元/500千克"),
    "LH": ("元/吨", "元/公斤", "现货按元/公斤，已折成元/吨"),
    "FU": ("元/吨", "元/吨", "现货 180CST，期货 RMG 380，规格不同仅供参考"),
    "I": ("元/吨", "元/吨", "现货湿吨，期货干吨，规格不同仅供参考"),
    "J": ("元/吨", "元/吨", "现货一级冶金焦，期货混合规格，仅供参考"),
}

# 这些品种直接用页面「现期差」折算成期货口径; 其余用原始现货价.
USE_EQ = {"FG", "SH", "JD", "LH"}


def num(s):
    s = (s or "").replace(",", "").replace("&nbsp;", "").strip()
    m = re.search(r"-?\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def fetch_html():
    s = requests.Session()
    s.headers.update(HEADERS)
    r = s.get(URL, timeout=TIMEOUT)
    # 生意社的简单反爬: 首次返回一段设置 HW_CHECK cookie 的 JS, 带上 cookie 再请求.
    m = re.search(r'var\s+_0x2\s*=\s*"([^"]+)"', r.text)
    if m:
        s.cookies.set("HW_CHECK", m.group(1), domain="www.100ppi.com", path="/")
        r = s.get(URL, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def parse(html):
    soup = BeautifulSoup(html, "html.parser")

    dm = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", html)
    date = "%04d-%02d-%02d" % (
        int(dm.group(1)), int(dm.group(2)), int(dm.group(3))) if dm else ""
    tm = re.search(r"\d{4}年\d{1,2}月\d{1,2}日\s*(\d{1,2}):(\d{2})", html)
    updated = ("%s %02d:%s" % (date, int(tm.group(1)), tm.group(2))) if (date and tm) else ""

    table = soup.find("table", id="fdata")
    if not table:
        return date, updated, {}

    items = {}
    for tr in table.find_all("tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 8:
            continue
        name = tds[0].get_text(" ", strip=True)
        code = NAME_TO_CODE.get(name)
        if not code:
            continue

        raw = num(tds[1].get_text(" ", strip=True))
        near_px = num(tds[3].get_text(" ", strip=True))
        nd = tds[4].find("td")
        diff = num(nd.get_text(" ", strip=True)) if nd else None
        if raw is None:
            continue

        eq = (near_px + diff) if (near_px is not None and diff is not None) else None
        price = eq if (code in USE_EQ and eq is not None) else raw

        unit, raw_unit, note = SPECIAL.get(code, ("元/吨", "元/吨", ""))
        items[code] = {
            "name": name,
            "price": round(price, 2),
            "raw": round(raw, 2),
            "unit": unit,
            "raw_unit": raw_unit,
            "note": note,
        }
    return date, updated, items


def load_valid_codes():
    try:
        with open(os.path.join("data", "index.json"), encoding="utf-8") as f:
            idx = json.load(f)
        return {v["code"] for v in idx.get("varieties", [])}
    except Exception:
        return set()


def load_old():
    try:
        with open(OUT, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def main():
    try:
        html = fetch_html()
        date, updated, items = parse(html)
    except Exception as e:  # noqa: BLE001
        print("spot: 抓取失败, 保留旧文件: %s" % e)
        return 0

    valid = load_valid_codes()
    if valid:
        items = {k: v for k, v in items.items() if k in valid}

    if not date or not items:
        print("spot: 未解析到有效数据, 保留旧文件")
        return 0

    old = load_old()
    if old and old.get("date") and old["date"] > date:
        print("spot: 页面日期 %s 早于现有 %s, 保留旧文件" % (date, old["date"]))
        return 0

    out = {
        "date": date,
        "updated": updated or datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M"),
        "source": URL,
        "count": len(items),
        "items": items,
    }
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    print("spot: %s, %d 个品种 -> %s" % (date, len(items), OUT))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())

"""川越市公共施設予約システム (https://k5.p-kashikan.jp/kawagoe-city/).

1日ごとの空き状況表を POST 1回で取得できる (ログイン・セッション不要)。
空いている枠のセルは "○" で、onmousedown の引数に時間帯 ('19002100' など) が入っている。
"""

import datetime as dt
import re
import time

import requests
from bs4 import BeautifulSoup

URL = "https://k5.p-kashikan.jp/kawagoe-city/index.php"
BOOKING_URL = "https://k5.p-kashikan.jp/kawagoe-city/"
MAX_DAYS = 70


def _fetch_day(s, shisetsu_code, date, row_prefix):
    r = s.post(URL, data={"op": "srch_sst", "ShisetsuCode": shisetsu_code,
                          "UseDate": date.strftime("%Y%m%d"), "disp_span": "0"}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = [tr for tr in soup.select("table.koma-table tr")
            if (name := tr.find("td", class_="name")) and name.get_text(strip=True).startswith(row_prefix)]
    if not rows:
        raise RuntimeError("川越市: 空き状況の表が見つかりません (サイトの構造が変わった可能性)")
    counts = {}
    open_for_booking = False  # "-" (受付期間外) 以外の表示があるか
    for tr in rows:
        for td in tr.find_all("td")[1:]:
            text = td.get_text(strip=True)
            if text not in ("-", ""):
                open_for_booking = True
            m = re.search(r"'(\d{2})(\d{2})(\d{2})(\d{2})'", td.get("onmousedown", ""))
            if text == "○" and m:
                slot = f"{m[1]}:{m[2]}-{m[3]}:{m[4]}"
                counts[slot] = counts.get(slot, 0) + 1
    return counts, open_for_booking


def fetch(target, today, wanted_dates):
    """{(date, "HH:MM-HH:MM"): 空きコート数} と {(date, slot): 注記} を返す. wanted_dates の日だけ取得する."""
    s = requests.Session()
    s.headers["User-Agent"] = "Mozilla/5.0 (tennis-cancel-notifier)"
    result = {}
    for i in range(MAX_DAYS):
        date = today + dt.timedelta(days=i)
        if date not in wanted_dates:
            continue
        counts, open_for_booking = _fetch_day(s, target["shisetsu_code"], date, target.get("row_prefix", "テニスコート"))
        time.sleep(0.5)  # サーバーに負荷をかけない
        if not open_for_booking and i > 3:
            break  # 受付期間の終わりまで来た
        for slot, n in counts.items():
            result[(date, slot)] = n
        for slot in target["slots"]:
            result.setdefault((date, slot), 0)
    return result, {}

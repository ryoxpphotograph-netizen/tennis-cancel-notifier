"""さいたま市施設予約システム (https://saitama.rsv.ws-scs.jp/web/).

画面遷移 (空き状況 → 利用目的 → 館 → 週表示) を HTTP で再現し、週表示の表を読む。
空いている枠には空きコート数が数字で表示される。
"""

import datetime as dt
import re
import time

import requests
from bs4 import BeautifulSoup

BASE = "https://saitama.rsv.ws-scs.jp/web/"
BOOKING_URL = BASE
PPSD_CD = "300"    # 利用目的分類: 屋外スポーツ
PPS_CD = "300180"  # 利用目的: テニス（硬式）
MAX_WEEKS = 10


class _Site:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "Mozilla/5.0 (tennis-cancel-notifier)"
        self.form = {}

    def _load(self, resp):
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content.decode("cp932", errors="replace"), "html.parser")
        f = soup.find("form", attrs={"name": "form1"})
        self.form = {e["name"]: e.get("value", "") for e in f.find_all("input") if e.get("name")} if f else {}
        return soup

    def get(self, path):
        return self._load(self.s.get(BASE + path, timeout=30))

    def post(self, path, **overrides):
        data = {**self.form, **overrides}
        # 日本語を含む値はエラーメッセージ用の hidden 項目のみなので送らない
        data = {k: v for k, v in data.items() if str(v).isascii()}
        time.sleep(0.5)  # サーバーに負荷をかけない
        return self._load(self.s.post(BASE + path, data=data, timeout=30))


def _slot_label(text):
    """「１９時」→「19:00-21:00」 (2時間枠)."""
    h = int(text.translate(str.maketrans("０１２３４５６７８９", "0123456789")).rstrip("時"))
    return f"{h:02d}:00-{h + 2:02d}:00"


def _parse_week(soup):
    """週表示の表を {(date, 時間帯): 空き数 or 状態文字列} にする."""
    time_th = soup.find("th", class_="akitablelist2")  # 「９時」などの行見出し
    if time_th is None:
        raise RuntimeError("さいたま市: 空き状況の表が見つかりません (サイトの構造が変わった可能性)")
    rows = time_th.find_parent("table").find_all("tr")
    header = next(r for r in rows if r.find("th") and re.search(r"\d{4}年", r.find("th").get_text()))
    ths = header.find_all("th")
    year = int(re.search(r"(\d{4})年", ths[0].get_text()).group(1))
    dates, prev_month = [], None
    for th in ths[1:]:
        m, d = map(int, re.search(r"(\d+)月(\d+)日", th.get_text()).groups())
        if prev_month is not None and m < prev_month:
            year += 1  # 年またぎ
        prev_month = m
        dates.append(dt.date(year, m, d))

    result = {}
    for r in rows:
        th = r.find("th", class_="akitablelist2")
        if not th:
            continue
        slot = _slot_label(th.get_text(strip=True))
        for date, td in zip(dates, r.find_all("td")):
            text = td.get_text(strip=True)
            if text.isdigit():
                result[(date, slot)] = int(text)
            else:
                img = td.find("img")
                result[(date, slot)] = img.get("alt", "") if img else text
    return result


def fetch(target, today, wanted_dates):
    """{(date, "HH:MM-HH:MM"): 空きコート数} と {(date, slot): 注記} を返す."""
    site = _Site()
    site.get("")                                                     # トップ
    site.post("rsvWTransInstSrchVacantAction.do")                     # 空き状況検索
    site.post("rsvWTransInstSrchPpsdAction.do", conditionMode="2")    # 利用目的から
    site.post("rsvWTransInstSrchPpsAction.do", conditionMode="2", selectPpsdCd=PPSD_CD)
    site.post("rsvWTransInstSrchBuildAction.do", selectPpsPpsdCd=PPSD_CD, selectPpsCd=PPS_CD,
              selectAreaCd="0", selectCommunityManageCd="0", selectCommunityPlaceCd="0")
    site.post("rsvWTransInstSrchInstAction.do", selectBldCd=target["bld_cd"])  # 館 → 1ヶ月表示

    soup = site.post(
        "rsvWInstSrchVacantAction.do", transVacantMode="7",
        srchSelectYMD=today.strftime("%Y%m%d"),
        dispYY=str(today.year), dispMM=str(today.month), dispDD=str(today.day),
        selectYY=str(today.year), selectMM=str(today.month), selectDD=str(today.day),
    )
    cells = {}
    for week in range(MAX_WEEKS):
        week_cells = _parse_week(soup)
        cells.update(week_cells)
        if all(v == "受付期間外" for v in week_cells.values()):
            break
        if week < MAX_WEEKS - 1:
            soup = site.post("rsvWInstSrchVacantAction.do", transVacantMode="4", srchSelectInstNo="0")  # 次の週
    return {k: (v if isinstance(v, int) else 0) for k, v in cells.items()}, {}

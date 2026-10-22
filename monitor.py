"""テニスコートのキャンセル空き監視 → LINE 通知.

各市の施設予約システムから指定した曜日・時間帯の空きコート数を取得し、
前回の結果 (state.json) と比べて空きが新たに出た/増えた枠だけを LINE に通知する。
"""

import argparse
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import jpholiday
import requests

from sites import kawagoe, saitama

# ===== 監視条件 =====
# days: "weekday" = 平日 (月〜金、祝日除く) / "holiday" = 土日祝
# slots: 予約サイトの時間区分 ("HH:MM-HH:MM")
TARGETS = [
    {"name": "三橋総合公園", "site": "saitama", "bld_cd": "4060",
     "days": "weekday", "slots": ["19:00-21:00"]},
    {"name": "川越運動公園", "site": "kawagoe", "shisetsu_code": "057",
     "days": "weekday", "slots": ["19:00-21:00"]},
]
DAYS_AHEAD = 70  # 何日先まで見るか (実際は各サイトの受付期間まで)

SITES = {"saitama": saitama, "kawagoe": kawagoe}
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
ERRORS_KEY = "_errors"     # state.json 内の連続エラー回数
ERROR_ALERT_AFTER = 6      # 何回連続で失敗したら LINE で知らせるか (10分間隔なら約1時間)
WEEKDAY_JA = "月火水木金土日"
JST = dt.timezone(dt.timedelta(hours=9))


def is_holiday(d):
    return d.weekday() >= 5 or jpholiday.is_holiday(d)


def wanted_dates(target, today):
    dates = (today + dt.timedelta(days=i) for i in range(DAYS_AHEAD))
    if target["days"] == "weekday":
        return {d for d in dates if not is_holiday(d)}
    if target["days"] == "holiday":
        return {d for d in dates if is_holiday(d)}
    raise ValueError(f"days の指定が不正です: {target['days']}")


def fetch_target(target, today):
    """{"施設名|YYYY-MM-DD|HH:MM-HH:MM": 空きコート数} と注記を返す."""
    dates = wanted_dates(target, today)
    counts, notes = SITES[target["site"]].fetch(target, today, dates)
    slots, slot_notes = {}, {}
    for (date, slot), n in counts.items():
        if date in dates and slot in target["slots"]:
            key = f"{target['name']}|{date.isoformat()}|{slot}"
            slots[key] = n
            if (date, slot) in notes:
                slot_notes[key] = notes[(date, slot)]
    return slots, slot_notes


def format_slot(key, count, note=None):
    _, date_s, slot = key.split("|")
    d = dt.date.fromisoformat(date_s)
    line = f"・{d.month}/{d.day}({WEEKDAY_JA[d.weekday()]}) {slot.replace('-', '〜')}  空き{count}面"
    return line + (f"  ※{note}" if note else "")


def send_line(text):
    token = os.environ.get("LINE_CHANNEL_ACCESS_TOKEN")
    user_id = os.environ.get("LINE_USER_ID")
    if not token:
        print("[LINE 未設定のため送信しません]\n" + text)
        return
    messages = [{"type": "text", "text": text[:5000]}]
    if user_id:  # 自分だけに送る
        url, body = "https://api.line.me/v2/bot/message/push", {"to": user_id, "messages": messages}
    else:  # ユーザーID未設定なら公式アカウントの友だち全員 (= 自分) に送る
        url, body = "https://api.line.me/v2/bot/message/broadcast", {"messages": messages}
    r = requests.post(url, headers={"Authorization": f"Bearer {token}"}, json=body, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"LINE 送信失敗: {r.status_code} {r.text}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="取得結果を表示するだけで通知・保存しない")
    ap.add_argument("--test-line", action="store_true", help="LINE にテストメッセージを送る")
    args = ap.parse_args()

    if args.test_line:
        # 実際の通知と同じ形のサンプル (予約リンクをタップして確認できる)
        today = dt.datetime.now(JST).date()
        samples = []
        for t in TARGETS:
            key = f"{t['name']}|{today.isoformat()}|{t['slots'][0]}"
            samples.append(f"🎾 【テスト】{t['name']}テニスコートに空きが出ました\n"
                           f"{format_slot(key, 1)}\n予約: {SITES[t['site']].BOOKING_URL}")
        send_line("\n\n".join(samples) + "\n\n※これはテスト通知です。実際の空きではありません。")
        return

    today = dt.datetime.now(JST).date()
    prev = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    prev_errors = prev.pop(ERRORS_KEY, {})
    state = {ERRORS_KEY: {}}
    messages = []

    # 施設ごとに別のサーバーなので並行して取得する
    with ThreadPoolExecutor(max_workers=len(TARGETS)) as pool:
        futures = [pool.submit(fetch_target, t, today) for t in TARGETS]

    for target, future in zip(TARGETS, futures):
        try:
            slots, notes = future.result()
        except Exception as e:  # noqa: BLE001  1施設の失敗で他の施設の監視を止めない
            print(f"[{target['name']}] 取得エラー: {e}", file=sys.stderr)
            # 前回の状態を引き継ぎ、復旧時に同じ空きを再通知しないようにする
            state.update({k: v for k, v in prev.items() if k.startswith(target["name"] + "|")})
            # 一時的なエラーは無視し、続いたときだけ1回知らせる
            n = state[ERRORS_KEY][target["name"]] = prev_errors.get(target["name"], 0) + 1
            if n == ERROR_ALERT_AFTER:
                messages.append(f"⚠️ {target['name']}の空き状況が{n}回続けて取得できていません。"
                                f"予約サイトの仕様が変わった可能性があります。\n{e}")
            continue

        print(f"[{target['name']}] 監視枠 {len(slots)} 件 / 空きあり {sum(1 for c in slots.values() if c)} 件")
        if args.dry_run:
            for key, count in sorted(slots.items()):
                print("  ", format_slot(key, count, notes.get(key)))
            continue

        state.update(slots)
        new = {k: c for k, c in sorted(slots.items()) if c > prev.get(k, 0)}
        if new:
            booking_url = SITES[target["site"]].BOOKING_URL
            lines = [f"🎾 {target['name']}テニスコートに空きが出ました"]
            lines += [format_slot(k, c, notes.get(k)) for k, c in new.items()]
            lines.append(f"予約: {booking_url}")
            messages.append("\n".join(lines))

    if args.dry_run:
        return
    if messages:
        send_line("\n\n".join(messages))
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()

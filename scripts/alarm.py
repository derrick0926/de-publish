"""鬧鐘：睡到排程表裡最早的一則，時間到就啟動 publish.yml，再設下一則。

- 同一時間只有一個鬧鐘（alarm.yml 的 concurrency 會取消舊的）；排程有變動時發文助手重新啟動 alarm.yml。
- GitHub 單次工作最多 6 小時：每段最多睡 5 小時 45 分，用完就叫醒下一段自己（失敗重試 3 次）。
- 每 30 分鐘醒來重讀一次排程表，期間的改動不會漏掉。
- 預覽待確認（status=preview）的也會在時間到時交給 publish.yml：它不會發，只通知 PM（Derrick 2026-11-26）。
"""
import sys
import time
from datetime import datetime, timedelta

import common as c

BUDGET = 5 * 3600 + 45 * 60
CHUNK = 30 * 60
PENDING = ("scheduled", "preview")


def plan(schedule: dict, t: datetime):
    """回傳 ("publish", [到期的 id]) / ("sleep", 秒數) / ("idle", None)。純函式，方便測試。"""
    items = [i for i in schedule.get("items", []) if i.get("status") in PENDING]
    due = [i["id"] for i in items if c.parse(i["at"]) <= t]
    if due:
        return "publish", due
    if not items:
        return "idle", None
    nxt = min(c.parse(i["at"]) for i in items)
    return "sleep", max(1, int((nxt - t).total_seconds()))


def mark_publishing(ids: list[str]) -> None:
    s = c.load_schedule()
    for i in s["items"]:
        if i["id"] in ids and i.get("status") in PENDING:
            i["prev_status"], i["status"], i["dispatched_at"] = i["status"], "publishing", c.iso(c.now())
    c.save_schedule(s, f"alarm: 到時間 {', '.join(ids)}")


def main() -> int:
    start = time.monotonic()
    while True:
        c.pull()
        action, arg = plan(c.load_schedule(), c.now())
        if action == "idle":
            print("排程表沒有待發的貼文，鬧鐘結束")
            return 0
        if action == "publish":
            mark_publishing(arg)
            if not c.dispatch("publish.yml"):
                print("::error::啟動 publish.yml 失敗；每小時巡檢會補")
            time.sleep(60)
            continue
        left = BUDGET - (time.monotonic() - start)
        if left <= 60:
            print("這一段快到 6 小時上限，叫醒下一段鬧鐘")
            ok = c.dispatch("alarm.yml")
            return 0 if ok else 1
        nap = min(arg + 5, CHUNK, left - 30)
        print(f"下一則在 {timedelta(seconds=arg)} 後；先睡 {timedelta(seconds=int(nap))}")
        time.sleep(nap)


if __name__ == "__main__":
    sys.exit(main())

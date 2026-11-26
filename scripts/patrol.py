"""每小時備援巡檢（不花 Claude token）：只檢查兩件事。

1. 排程表有待發的貼文，但鬧鐘沒在跑 → 重新啟動 alarm.yml。
2. 有「時間已過還沒發」的（鬧鐘漏掉，或 publish.yml 卡住超過 20 分鐘）→ 啟動 publish.yml（逾時規則由它套用）。
"""
import sys
from datetime import timedelta

import common as c


def main() -> int:
    s = c.load_schedule()
    t = c.now()
    items = s.get("items", [])
    pending = [i for i in items if i.get("status") in ("scheduled", "preview")]
    due = [i for i in pending if c.parse(i["at"]) <= t]
    stuck = [i for i in items if i.get("status") == "publishing"
             and t - c.parse(i.get("dispatched_at", i["at"])) > timedelta(minutes=20)]
    print(f"待發 {len(pending)}、已到時間 {len(due)}、發佈中卡住 {len(stuck)}")
    if (due or stuck) and c.active_runs("publish.yml") == 0:
        print("有到時間沒發的貼文：啟動 publish.yml")
        c.dispatch("publish.yml")
    if pending and c.active_runs("alarm.yml") == 0:
        print("鬧鐘沒在跑：重新啟動 alarm.yml")
        c.dispatch("alarm.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())

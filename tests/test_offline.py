"""離線測試：假的 Meta／Threads／GitHub API，不連網、不發文、不動 git。

執行：python tests/test_offline.py
"""
import json
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import os
os.environ.update({"DRY_GIT": "1", "META_PAGE_ID": "P", "META_PAGE_TOKEN": "tokP", "META_IG_USER_ID": "IG",
                   "THREADS_USER_ID": "T", "THREADS_TOKEN": "tokT", "PUBLISH_GH_TOKEN": "tokG"})
import common as c
import alarm
import publish

c.time.sleep = lambda *_: None
publish.time.sleep = lambda *_: None

POST = {"id": "abc123", "title": "測試貼文", "vocus_url": "https://vocus.cc/article/x",
        "fb": {"message": "FB 內文", "images": ["fb-01.jpg", "fb-02.jpg"], "comment": "全文 https://vocus.cc/article/x"},
        "ig": {"caption": "IG 內文", "images": ["ig-01.jpg", "ig-02.jpg"]},
        "threads": {"text": "Threads 內文 https://vocus.cc/article/x"}}
JPG = b"\xff\xd8\xff\xe0fake"


class Fake:
    def __init__(self, fail=()):
        self.calls, self.events, self.fail, self.n = [], [], set(fail), 0

    def __call__(self, method, url, params=None, data=None, files=None, headers=None, raw=False, timeout=90):
        self.calls.append((method, url.split("?")[0]))
        self.n += 1
        u = url.split("?")[0]
        if u.startswith(c.GH):
            p = u[len(c.GH):]
            if method == "PUT" and "/contents/events/" in p:
                self.events.append(json.loads(__import__("base64").b64decode(json.loads(data)["content"])))
                return 201, {}
            if p.endswith("/dispatches"):
                return 204, {}
            if p.endswith("/publish/abc123"):
                return 200, [{"name": n, "type": "file"} for n in ["post.json", *POST["fb"]["images"], *POST["ig"]["images"]]]
            if p.endswith("/post.json"):
                return 200, json.dumps(POST).encode()
            if p.endswith(".jpg"):
                return 200, JPG
            if "/runs" in p:
                return 200, {"workflow_runs": []}
            raise AssertionError(f"沒預期的 GitHub 呼叫 {method} {p}")
        if "raw.githubusercontent.com" in u:
            return 200, b""
        # Meta／Threads：權杖不可以出現在錯誤訊息裡，回傳時順便確認有帶
        body = dict(data or {}) | dict(params or {})
        assert body.get("access_token") in ("tokP", "tokT"), u
        for key in self.fail:
            if key in u:
                self.fail.discard(key)
                return 400, {"error": {"message": f"假錯誤 {key}", "code": 9}}
        if u.endswith("/P/photos"):
            assert files and files["source"][1] == JPG
            return 200, {"id": f"ph{self.n}"}
        if u.endswith("/P/feed"):
            assert body["attached_media[1]"].startswith('{"media_fbid"')
            return 200, {"id": "P_post1"}
        if u.endswith("/P_post1/comments"):
            return 200, {"id": "cm1"}
        if u.endswith("/IG/media"):
            return 200, {"id": f"ct{self.n}"}
        if u.endswith("/IG/media_publish"):
            return 200, {"id": "igm1"}
        if u.endswith("/T/threads"):
            return 200, {"id": "tc1"}
        if u.endswith("/T/threads_publish"):
            return 200, {"id": "th1"}
        if method == "GET":
            f = (params or {}).get("fields", "")
            return 200, {"status_code": "FINISHED", "status": "FINISHED", "permalink_url": "https://fb/x",
                         "permalink": "https://link/x"} if f else {}
        raise AssertionError(f"沒預期的呼叫 {method} {u}")


def setup(items):
    d = Path(tempfile.mkdtemp())
    c.ROOT, c.SCHEDULE, c.STATE = d, d / "schedule.json", d / "state"
    c.SCHEDULE.write_text(json.dumps({"rev": 0, "items": items}), encoding="utf-8")


def item(minutes_ago, status="scheduled"):
    return {"id": "abc123", "title": "測試貼文", "at": c.iso(c.now() - timedelta(minutes=minutes_ago)), "status": status}


def test_plan():
    t = c.now()
    s = {"items": [{"id": "a", "at": c.iso(t + timedelta(hours=2)), "status": "scheduled"},
                   {"id": "b", "at": c.iso(t + timedelta(hours=1)), "status": "preview"},
                   {"id": "x", "at": c.iso(t - timedelta(hours=1)), "status": "publishing"}]}
    a, sec = alarm.plan(s, t)
    assert a == "sleep" and 3590 <= sec <= 3600, (a, sec)
    s["items"][1]["at"] = c.iso(t - timedelta(minutes=1))
    assert alarm.plan(s, t) == ("publish", ["b"])
    assert alarm.plan({"items": []}, t) == ("idle", None)


def test_success_and_no_repost():
    setup([item(2)])
    f = Fake(); c.http = f
    assert publish.main([]) == 0
    st = json.loads((c.STATE / "abc123.json").read_text())
    assert st["result"] == "done" and set(st["done"]) == {"fb_post", "fb_comment", "ig", "threads"}, st
    assert f.events[-1]["type"] == "publish_done" and f.events[-1]["links"]["ig"] == "https://link/x"
    assert json.loads(c.SCHEDULE.read_text())["items"] == []
    assert not (c.ROOT / "tmp" / "abc123").exists(), "IG 暫存圖要刪掉"
    blob = json.dumps(f.events, ensure_ascii=False)
    assert "tokP" not in blob and "tokT" not in blob
    # 重試同一則：全部已發，不能再呼叫任何發文 API
    f2 = Fake(); c.http = f2
    publish.main(["--ids", "abc123", "--force"])
    assert not [x for x in f2.calls if x[0] == "POST" and ("graph" in x[1])], f2.calls


def test_partial_failure_then_retry_only_ig():
    setup([item(1)])
    f = Fake(fail={"/IG/media_publish"}); c.http = f
    assert publish.main([]) == 1
    st = json.loads((c.STATE / "abc123.json").read_text())
    assert st["result"] == "failed" and "ig" in st["errors"] and "假錯誤" in st["errors"]["ig"]
    assert st["done"].get("fb_post") and st["done"].get("threads")
    assert f.events[-1]["type"] == "publish_failed" and f.events[-1]["failed"] == ["ig"]
    f2 = Fake(); c.http = f2
    assert publish.main(["--ids", "abc123", "--force"]) == 0
    posted = [u for m, u in f2.calls if m == "POST" and "graph" in u]
    assert posted and all("/IG/" in u for u in posted), posted
    assert json.loads((c.STATE / "abc123.json").read_text())["result"] == "done"


def test_overdue_and_unconfirmed():
    setup([item(61)])
    f = Fake(); c.http = f
    publish.main([])
    assert f.events[-1]["type"] == "publish_overdue"
    assert not [x for x in f.calls if "graph" in x[1]], "逾時不能發"
    setup([item(59)])
    f = Fake(); c.http = f
    publish.main([])
    assert f.events[-1]["type"] == "publish_done", "59 分鐘內照常補發"
    setup([item(0, "preview")])
    f = Fake(); c.http = f
    publish.main([])
    assert f.events[-1]["type"] == "publish_unconfirmed"
    assert not [x for x in f.calls if "graph" in x[1]], "沒確認的不能發"


def test_not_due_untouched():
    setup([item(-30)])
    f = Fake(); c.http = f
    assert publish.main([]) == 0 and not f.events
    assert len(json.loads(c.SCHEDULE.read_text())["items"]) == 1


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("✓", name)
    print("全部通過")

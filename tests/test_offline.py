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
import media as m

c.time.sleep = lambda *_: None
publish.time.sleep = lambda *_: None

POST = {"id": "abc123", "title": "測試貼文", "vocus_url": "https://vocus.cc/article/x",
        "fb": {"message": "第一段\n\n第二段\n", "images": ["fb-01.jpg", "fb-02.jpg"], "comment": "全文 https://vocus.cc/article/x"},
        "ig": {"caption": "IG 內文", "images": ["ig-01.jpg", "ig-02.jpg"]},
        "threads": {"text": "Threads 內文", "reply": "完整拆解在這：https://vocus.cc/article/x"}}
JPG = b"\xff\xd8\xff\xe0fake"
PNG = b"\x89PNG\r\n\x1a\nfake"


class Fake:
    def __init__(self, fail=()):
        self.calls, self.events, self.fail, self.n = [], [], set(fail), 0
        self.photos, self.uploaded, self.children = [], [], []

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
                return 200, [{"name": n, "type": "file"} for n in ["post.json", *(POST.get("fb") or {}).get("images", []), *(POST.get("ig") or {}).get("images", []), *(POST.get("threads") or {}).get("images", [])]]
            if p.endswith("/post.json"):
                return 200, json.dumps(POST).encode()
            if p.endswith(".jpg"):
                return 200, JPG
            if p.endswith(".png"):
                return 200, PNG
            if "/runs" in p:
                return 200, {"workflow_runs": []}
            raise AssertionError(f"沒預期的 GitHub 呼叫 {method} {p}")
        if "raw.githubusercontent.com" in u:
            return 200, b""
        # Meta／Threads：權杖不可以出現在錯誤訊息裡，回傳時順便確認有帶
        body = dict(data or {}) | dict(params or {})
        assert body.get("access_token") in ("tokP", "tokT"), u
        for key in self.fail:
            if key.startswith("code24:") and key[7:] in u:   # Threads 剛建好的容器還沒同步：回 media cannot be found（code 24）一次
                self.fail.discard(key); self.code24 = getattr(self, "code24", 0) + 1
                return 400, {"error": {"message": "The media with id 1 cannot be found.", "code": 24}}
            if key in u:
                self.fail.discard(key)
                return 400, {"error": {"message": f"假錯誤 {key}", "code": 9}}
        if u.endswith("/P/photos"):
            if files:
                assert files["source"][1] in (JPG, PNG)
                assert files["source"][2] == ("image/png" if files["source"][1] == PNG else "image/jpeg"), "PNG 要用 image/png 上傳"
                self.uploaded.append(files["source"][0])
            else:
                assert body.get("url", "").startswith("http"), "網址圖用 url 參數"
                self.uploaded.append("url:" + body["url"].rsplit("/", 1)[-1])
            self.photos.append(f"ph{self.n}")
            return 200, {"id": f"ph{self.n}"}
        if u.endswith("/P/videos"):
            assert body["file_url"].startswith("https://cdn.test/") and body.get("description")
            self.fb_video = body["file_url"]
            return 200, {"id": "P_vid1"}
        if u.endswith("/P_vid1") and method == "GET":
            return 200, {"permalink_url": "/DE/videos/1/"}
        if u.endswith("/P/feed") and not POST["fb"].get("images") and not POST["fb"].get("media"):
            assert "attached_media[0]" not in body, "純文字貼文不附圖"
            self.fb_text = body["message"]
            return 200, {"id": "P_post1"}
        if u.endswith("/P/feed"):
            n_img = len(POST["fb"].get("images") or POST["fb"].get("media") or [])
            assert body[f"attached_media[{n_img - 1}]"].startswith('{"media_fbid"') and f"attached_media[{n_img}]" not in body
            if n_img == 2:
                assert body["message"] == "第一段\n\u3000\n第二段", repr(body["message"])
            assert [body[f"attached_media[{k}]"] for k in range(n_img)] == ['{"media_fbid":"%s"}' % i for i in self.photos], "照順序附圖"
            return 200, {"id": "P_post1"}
        if u.endswith("/P_post1/comments") or u.endswith("/P_vid1/comments"):
            return 200, {"id": "cm1"}
        if u.endswith("/IG/media"):
            if body.get("is_carousel_item"):
                self.children.append((f"ct{self.n}", body["image_url"].rsplit("/", 1)[-1]))
            elif body.get("media_type") == "CAROUSEL":
                assert body["children"] == ",".join(c for c, _ in self.children), "輪播照順序"
            return 200, {"id": f"ct{self.n}"}
        if u.endswith("/IG/media_publish"):
            return 200, {"id": "igm1"}
        if u.endswith("/T/threads") and (body.get("is_carousel_item") or body.get("media_type") == "CAROUSEL"):
            # 速報帶圖：輪播子項不算一則貼文
            if body.get("is_carousel_item"):
                assert body["media_type"] in ("IMAGE", "VIDEO") and "text" not in body
                src = body.get("image_url") or body.get("video_url")
                self.th_kids = getattr(self, "th_kids", []) + [(f"tk{self.n}", (body["media_type"] + ":" if body["media_type"] == "VIDEO" else "") + src.rsplit("/", 1)[-1])]
                return 200, {"id": f"tk{self.n}"}
            assert body["children"] == ",".join(k for k, _ in self.th_kids), "Threads 輪播照順序"
            self.th_n = getattr(self, "th_n", 0) + 1
            self.th_texts = getattr(self, "th_texts", []) + [body["text"]]
            self.th_media = "carousel"
            return 200, {"id": f"tc{self.th_n}"}
        if u.endswith("/T/threads"):
            self.th_n = getattr(self, "th_n", 0) + 1
            self.th_texts = getattr(self, "th_texts", []) + [body["text"]]
            if body.get("media_type") == "IMAGE":
                assert not body.get("reply_to_id"), "回覆串維持純文字"
                self.th_media = "image:" + body["image_url"].rsplit("/", 1)[-1]
            elif body.get("media_type") == "VIDEO":
                assert not body.get("reply_to_id")
                self.th_media = "video:" + body["video_url"].rsplit("/", 1)[-1]
            else:
                assert body["media_type"] == "TEXT"
            if body.get("reply_to_id"):
                assert body["reply_to_id"] == f"th{self.th_n - 1}", "回覆要掛在前一則底下"
                self.threads_reply = body["text"]
            return 200, {"id": f"tc{self.th_n}"}
        if u.endswith("/T/threads_publish"):
            return 200, {"id": f"th{self.th_n}"}
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
    assert st["result"] == "done" and set(st["done"]) == {"fb_post", "fb_comment", "ig", "threads", "threads_reply"}, st
    assert f.events[-1]["type"] == "publish_done" and f.events[-1]["links"]["ig"] == "https://link/x"
    assert f.uploaded == ["fb-01.jpg", "fb-02.jpg"] and [n for _, n in f.children] == ["ig-01.jpg", "ig-02.jpg"], "圖片照貼文包順序"
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


FLASH = {"id": "abc123", "kind": "flash", "code": "SEO-001", "title": "速報測試", "platforms": ["threads"],
         "threads": {"posts": ["▞ 標題 ▚\n\n故事", "接下來的重點", "原文（SEJ）：https://x/"]},
         "fb": {"message": "不該發的 FB", "images": [], "comment": ""}}
DIGEST = {"id": "abc123", "kind": "digest", "title": "DE 行銷速報｜11/27", "platforms": ["threads", "fb"],
          "threads": {"posts": ["DE 行銷速報｜11/27\n\n▞ A ▚\n\n▶ https://a/"]},
          "fb": {"message": "DE 行銷速報｜11/27\n\n▞ A ▚\n▶ https://a/", "images": [], "comment": ""}}


def _with_post(post, fn):
    global POST
    old = POST
    POST = post
    try:
        fn()
    finally:
        POST = old


def test_flash_threads_chain_only():
    def go():
        setup([{**item(2), "kind": "flash"}])
        f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert set(st["done"]) == {"threads", "threads_reply", "threads_reply2"}, st
        assert f.th_texts == [publish.keep_blank_lines(x) for x in FLASH["threads"]["posts"]], f.th_texts
        assert not [x for x in f.calls if x[1].endswith("/P/feed")], "速報沒選 FB 就不發 FB"
        assert f.events[-1]["type"] == "publish_done" and f.events[-1]["kind"] == "flash" and "Threads" in f.events[-1]["text"]
        assert "FB" not in f.events[-1]["text"]
    _with_post(FLASH, go)


def test_digest_fb_text_only():
    def go():
        setup([{**item(2), "kind": "digest"}])
        f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert set(st["done"]) == {"fb_post", "threads"}, st
        assert f.fb_text.startswith("DE 行銷速報")
        assert f.events[-1]["kind"] == "digest"
    _with_post(DIGEST, go)


FLASH_IMG = {**FLASH, "threads": {**FLASH["threads"], "images": ["01.jpg", "02.png", "03.jpg"]}}
FLASH_ONE = {**FLASH, "platforms": ["threads", "fb"], "threads": {**FLASH["threads"], "images": ["01.png"]},
             "fb": {"message": "▞ 標題 ▚\n\n故事", "images": ["01.png"], "comment": "原文（SEJ）：https://x/"}}


def test_flash_with_images_carousel():
    """速報 3 張圖：主文是輪播（照順序、含 PNG），回覆仍是純文字；tmp/ 圖發完刪掉。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert set(st["done"]) == {"threads", "threads_reply", "threads_reply2"}, st
        assert f.th_media == "carousel" and [n for _, n in f.th_kids] == ["01.jpg", "02.png", "03.jpg"], f.th_kids
        assert len(f.th_texts) == 3, "子項不算貼文"
        assert not (c.ROOT / "tmp" / "abc123").exists(), "公開 repo 的圖要刪掉"
    _with_post(FLASH_IMG, go)


def test_flash_single_image_and_fb():
    """速報 1 張 PNG、勾 FB：Threads 單圖主文，FB 用 image/png 上傳成單圖貼文。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert {"threads", "fb_post", "fb_comment"} <= set(st["done"]), st
        assert f.th_media == "image:01.png"
        assert f.uploaded == ["01.png"]
    _with_post(FLASH_ONE, go)


# ── 網址圖／網址影片（scripts/media.py，2026-11-27）：下載與 R2 用假的 ──
class FakeMedia:
    def __init__(self, r2=True):
        self.r2 = r2; self.put = []; self.deleted = []; self.downloaded = []
    def install(self):
        import pathlib
        def download(url, dest, expect):
            self.downloaded.append((url, expect))
            if "404" in url: raise c.ApiError(f"媒體網址下載失敗（HTTP 404）：{url}")
            if expect == "image" and url.endswith(".mp4"): raise c.ApiError("網址抓到的不是圖片（video）")
            pathlib.Path(dest).mkdir(parents=True, exist_ok=True)
            ext = "mp4" if expect == "video" else "png"
            f = pathlib.Path(dest) / f"source.{ext}"; f.write_bytes(b"fake"); return f, ext
        def to_jpeg(src, dst):
            pathlib.Path(dst).write_bytes(JPG); return pathlib.Path(dst)
        def r2_put(key, path, ctype):
            self.put.append((key, ctype)); return "https://cdn.test/" + key
        m.download, m.to_jpeg, m.r2_put = download, to_jpeg, r2_put
        m.r2_delete = lambda keys: self.deleted.extend(keys)
        m.r2_enabled = lambda: self.r2
        return self


FLASH_URLS = {**FLASH, "threads": {**FLASH["threads"], "media": [{"type": "image", "file": "01.jpg"}, {"type": "image", "url": "https://pic.test/a.webp"}, {"type": "video", "url": "https://vid.test/b.mp4"}], "images": ["01.jpg"]}}
FLASH_VID = {**FLASH, "platforms": ["threads", "fb"], "threads": {**FLASH["threads"], "media": [{"type": "video", "url": "https://vid.test/only.mp4"}]},
             "fb": {"message": "▞ 標題 ▚\n\n故事", "media": [{"type": "video", "url": "https://vid.test/only.mp4"}], "comment": "原文（SEJ）：https://x/"}}
FLASH_URLIMG_NOR2 = {**FLASH, "platforms": ["threads", "fb"], "threads": {**FLASH["threads"], "media": [{"type": "image", "url": "https://pic.test/c.avif"}]},
                     "fb": {"message": "文", "media": [{"type": "image", "url": "https://pic.test/c.avif"}], "comment": ""}}


def test_media_mixed_carousel_with_r2():
    """上傳圖＋網址圖＋網址影片：Threads 輪播照順序（圖→R2 的 JPEG→R2 的影片），發完 R2 與 tmp/ 都清掉。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        fm = FakeMedia(r2=True).install(); f = Fake(); c.http = f
        assert publish.main([]) == 0
        assert f.th_media == "carousel" and [n for _, n in f.th_kids] == ["01.jpg", "02.jpg", "VIDEO:03.mp4"], f.th_kids
        assert [k for k, _ in fm.put] == ["flash/abc123/02.jpg", "flash/abc123/03.mp4"] and fm.deleted == ["flash/abc123/02.jpg", "flash/abc123/03.mp4"]
        assert fm.downloaded == [("https://pic.test/a.webp", "image"), ("https://vid.test/b.mp4", "video")]
        assert not (c.ROOT / "tmp" / "abc123").exists() and not (c.ROOT / "work" / "abc123").exists()
    _with_post(FLASH_URLS, go)


def test_media_video_threads_and_fb():
    """單支網址影片：Threads 單支影片主文、FB 影片貼文（file_url）。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        fm = FakeMedia(r2=True).install(); f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert f.th_media == "video:01.mp4" and f.fb_video.endswith("/01.mp4") and st["done"]["fb_post"] == "P_vid1"
        assert st["links"]["fb_post"].startswith("https://www.facebook.com/")
    _with_post(FLASH_VID, go)


def test_media_url_image_without_r2_uses_tmp():
    """沒設 R2：網址圖轉 JPEG 後放公開 repo 的 tmp/，Threads 與 FB 都用那個網址。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        fm = FakeMedia(r2=False).install(); f = Fake(); c.http = f
        assert publish.main([]) == 0
        assert f.th_media == "image:url-01.jpg" and f.uploaded == ["url:url-01.jpg"] and not fm.put
    _with_post(FLASH_URLIMG_NOR2, go)


def test_media_video_without_r2_fails_clearly():
    """沒設 R2 卻有影片網址：Threads 那則失敗、原因寫明，不會發出少了影片的主文。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        FakeMedia(r2=False).install(); f = Fake(); c.http = f
        assert publish.main([]) == 1
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert "R2" in st["errors"]["threads"] and "threads" not in st["done"]
        assert f.events[-1]["type"] == "publish_failed"
    _with_post({**FLASH_VID, "platforms": ["threads"]}, go)


def test_media_download_404_fails():
    def go():
        setup([{**item(2), "kind": "flash"}])
        FakeMedia(r2=True).install(); f = Fake(); c.http = f
        assert publish.main([]) == 1
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert "404" in st["errors"]["threads"]
    _with_post({**FLASH, "threads": {**FLASH["threads"], "media": [{"type": "image", "url": "https://pic.test/404.png"}]}}, go)


def test_threads_container_not_synced_yet_retries():
    """容器剛建好查狀態、或發佈時回 code 24 → 當作還沒好，等一下再試，不算失敗（2026-11-27 16:15 實發踩到）。"""
    def go():
        setup([{**item(2), "kind": "flash"}])
        f = Fake(fail={"code24:/tc1", "code24:/T/threads_publish"}); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert "threads" in st["done"] and f.code24 == 2
    _with_post(FLASH, go)


def test_retry_via_schedule_force():
    """重試走排程表：項目帶 force、已過 2 小時也照發，只補失敗的平台（主文已發不重發）。"""
    def go():
        setup([{**item(120), "kind": "flash", "force": True}])
        st0 = {"id": "abc123", "done": {"threads": "th0"}, "errors": {"threads_reply": "code 10"}, "links": {"threads": "https://x"}}
        c.STATE.mkdir(parents=True, exist_ok=True); (c.STATE / "abc123.json").write_text(json.dumps(st0))
        f = Fake(); c.http = f
        assert publish.main([]) == 0
        st = json.loads((c.STATE / "abc123.json").read_text())
        assert st["result"] == "done" and st["done"]["threads"] == "th0" and "threads_reply" in st["done"]
        assert len(f.th_texts) == 1, "只補回覆，主文不重發"
    _with_post(FLASH, go)


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

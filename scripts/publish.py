"""發佈：把到時間的貼文發到 FB（多圖＋粉專留言 vocus 連結）、IG（輪播）、Threads（純文字）。

用法：python scripts/publish.py [--ids id1,id2] [--force]
  沒給 --ids：處理排程表裡 status=publishing 的，以及已到時間的 scheduled／preview。
  --force：重試用（發文助手在 Derrick 按「重試」時帶上），不套用 60 分鐘逾時規則。

貼文內容在私人 repo 的 publish/<todo id>/（post.json＋JPEG），發文前一刻才讀。格式：
  {"id", "code", "line", "title", "vocus_url",
   "fb": {"message", "images": ["fb-01.jpg", …], "comment"},
   "ig": {"caption", "images": ["ig-01.jpg", …]},
   "threads": {"text"}}

不重發：每個平台發完立刻寫 state/<id>.json 並 push；重跑時已發的平台跳過（例：FB 已發、IG 失敗，重跑只補 IG）。
圖片順序：FB 照 fb.images、IG 照 ig.images 的順序上傳與組輪播（貼文包產生時已依圖卡編號排好並檢查過）。
"""
import argparse
import json
import os
import shutil
import sys
import time

import common as c

PLATFORMS = ("fb_post", "fb_comment", "ig", "threads")
LABEL = {"fb_post": "FB 貼文", "fb_comment": "FB 留言", "ig": "IG", "threads": "Threads"}


# ---------- 文字 ----------
def keep_blank_lines(text: str) -> str:
    """段落之間的空行放全形空白（U+3000），FB、IG、Threads 才不會把空行吃掉（Derrick 2026-11-26）。
    貼文包在私人 repo 產生時已經換好；這裡再保險一次（已換過的不會重複換）。"""
    lines = [l.rstrip() for l in (text or "").strip("\n").split("\n")]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join("\u3000" if not l.strip() else l for l in lines)


# ---------- 讀貼文 ----------
def fetch_package(todo_id: str) -> tuple[dict, dict[str, bytes]]:
    base = f"/repos/{c.OWNER}/{c.PRIV}/contents/publish/{todo_id}"
    listing = c.gh("GET", base + "?ref=main")
    names = {x["name"] for x in listing if x.get("type") == "file"}
    if "post.json" not in names:
        raise c.ApiError(f"私人 repo 找不到 publish/{todo_id}/post.json")
    post = json.loads(c.gh("GET", base + "/post.json?ref=main", raw=True))
    want = set((post.get("fb") or {}).get("images", [])) | set((post.get("ig") or {}).get("images", []))
    missing = want - names
    if missing:
        raise c.ApiError(f"缺圖：{', '.join(sorted(missing))}")
    files = {n: c.gh("GET", f"{base}/{n}?ref=main", raw=True) for n in sorted(want)}
    for n, b in files.items():
        if not b.startswith(b"\xff\xd8"):
            raise c.ApiError(f"{n} 不是 JPEG")
    return post, files


# ---------- 各平台 ----------
def fb_post(post, files) -> str:
    page, tok = c.env("META_PAGE_ID"), c.env("META_PAGE_TOKEN")
    ids = []
    for n in post["fb"]["images"]:
        r = c.meta("POST", f"{c.GRAPH}/{page}/photos", data={"published": "false", "access_token": tok},
                   files={"source": (n, files[n], "image/jpeg")})
        ids.append(r["id"])
    data = {"message": keep_blank_lines(post["fb"]["message"]), "access_token": tok}
    for k, i in enumerate(ids):
        data[f"attached_media[{k}]"] = '{"media_fbid":"%s"}' % i
    return c.meta("POST", f"{c.GRAPH}/{page}/feed", data=data)["id"]


def fb_comment(post, post_id) -> str:
    return c.meta("POST", f"{c.GRAPH}/{post_id}/comments",
                  data={"message": keep_blank_lines(post["fb"]["comment"]), "access_token": c.env("META_PAGE_TOKEN")})["id"]


def wait_ready(url_base: str, cid: str, tok: str, field: str, ok: str, limit=300) -> None:
    t0 = time.monotonic()
    while True:
        st = c.meta("GET", f"{url_base}/{cid}", params={"fields": field, "access_token": tok}).get(field, "")
        if st == ok:
            return
        if st in ("ERROR", "EXPIRED"):
            raise c.ApiError(f"容器狀態 {st}")
        if time.monotonic() - t0 > limit:
            raise c.ApiError(f"等容器處理逾時（最後狀態 {st or '未知'}）")
        time.sleep(5)


def stage_images(todo_id: str, names, files) -> list[str]:
    """IG 要公開網址：圖暫放到這個公開 repo 的 tmp/<id>/，發完刪掉。"""
    d = c.ROOT / "tmp" / todo_id
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(files[n])
    c.commit_push(f"tmp: {todo_id} 的 IG 圖（發完刪除）", [f"tmp/{todo_id}"])
    sha = c.git("rev-parse", "HEAD").strip() if not os.environ.get("DRY_GIT") else "main"
    urls = [f"https://raw.githubusercontent.com/{c.OWNER}/{c.PUB}/{sha}/tmp/{todo_id}/{n}" for n in names]
    for u in urls:   # 等 CDN 拿得到
        for _ in range(24):
            code, _b = c.http("HEAD", u, raw=True)
            if code == 200:
                break
            time.sleep(5)
        else:
            raise c.ApiError(f"公開圖片網址 2 分鐘內都讀不到（{u.rsplit('/', 1)[-1]}）")
    return urls


def unstage_images(todo_id: str) -> None:
    d = c.ROOT / "tmp" / todo_id
    if d.exists():
        shutil.rmtree(d)
        c.commit_push(f"tmp: 刪除 {todo_id} 的 IG 圖", [f"tmp/{todo_id}"])


def ig_post(post, files, todo_id) -> str:
    ig, tok = c.env("META_IG_USER_ID"), c.env("META_PAGE_TOKEN")
    urls = stage_images(todo_id, post["ig"]["images"], files)
    cap = keep_blank_lines(post["ig"]["caption"])
    if len(urls) == 1:
        cid = c.meta("POST", f"{c.GRAPH}/{ig}/media", data={"image_url": urls[0], "caption": cap, "access_token": tok})["id"]
    else:
        kids = []
        for u in urls:
            k = c.meta("POST", f"{c.GRAPH}/{ig}/media", data={"image_url": u, "is_carousel_item": "true", "access_token": tok})["id"]
            wait_ready(c.GRAPH, k, tok, "status_code", "FINISHED")
            kids.append(k)
        cid = c.meta("POST", f"{c.GRAPH}/{ig}/media", data={"media_type": "CAROUSEL", "children": ",".join(kids),
                                                             "caption": cap, "access_token": tok})["id"]
    wait_ready(c.GRAPH, cid, tok, "status_code", "FINISHED")
    return c.meta("POST", f"{c.GRAPH}/{ig}/media_publish", data={"creation_id": cid, "access_token": tok})["id"]


def threads_post(post) -> str:
    uid, tok = c.env("THREADS_USER_ID"), c.env("THREADS_TOKEN")
    text = keep_blank_lines(post["threads"]["text"])
    if len(text) > 500:
        raise c.ApiError(f"Threads 超過 500 字（{len(text)}）")
    cid = c.meta("POST", f"{c.THREADS}/{uid}/threads", data={"media_type": "TEXT", "text": text, "access_token": tok})["id"]
    wait_ready(c.THREADS, cid, tok, "status", "FINISHED", limit=120)
    return c.meta("POST", f"{c.THREADS}/{uid}/threads_publish", data={"creation_id": cid, "access_token": tok})["id"]


def permalink(kind: str, pid: str) -> str:
    try:
        if kind == "fb_post":
            return c.meta("GET", f"{c.GRAPH}/{pid}", params={"fields": "permalink_url", "access_token": c.env("META_PAGE_TOKEN")}).get("permalink_url", "")
        if kind == "ig":
            return c.meta("GET", f"{c.GRAPH}/{pid}", params={"fields": "permalink", "access_token": c.env("META_PAGE_TOKEN")}).get("permalink", "")
        if kind == "threads":
            return c.meta("GET", f"{c.THREADS}/{pid}", params={"fields": "permalink", "access_token": c.env("THREADS_TOKEN")}).get("permalink", "")
    except c.ApiError:
        pass
    return ""


# ---------- 一則 ----------
def needed(post) -> list[str]:
    out = []
    if post.get("fb"):
        out.append("fb_post")
        if (post["fb"].get("comment") or "").strip():
            out.append("fb_comment")
    if post.get("ig"):
        out.append("ig")
    if post.get("threads"):
        out.append("threads")
    return out


def publish_one(item: dict, force: bool) -> str:
    """回傳結果：done／failed／overdue／unconfirmed。會把這則從排程表拿掉並推事件。"""
    tid, at = item["id"], c.parse(item["at"])
    title = item.get("title", tid)
    status = item.get("prev_status", item.get("status"))
    st = c.load_state(tid)
    st.setdefault("done", {}); st.setdefault("errors", {}); st.setdefault("links", {})
    st.setdefault("scheduled_at", item["at"])

    if status == "preview":
        st["result"] = "unconfirmed"
        c.save_state(st, f"state: {tid} 沒確認，不發")
        c.push_event("publish_unconfirmed", tid, f"「{title}」排在 {at:%m/%d %H:%M}，時間到了還沒確認（沒填 vocus 連結），沒有發出；請改時間")
        return "unconfirmed"
    late = c.now() - at
    if late > c.LATE_LIMIT and not force:
        st["result"] = "overdue"
        c.save_state(st, f"state: {tid} 逾時未發")
        c.push_event("publish_overdue", tid, f"「{title}」排在 {at:%m/%d %H:%M}，已經晚了 {int(late.total_seconds() // 60)} 分鐘，依規則沒有自動發；請決定要不要發或改時間")
        return "overdue"

    try:
        post, files = fetch_package(tid)
    except c.ApiError as e:
        st["errors"]["package"] = str(e); st["result"] = "failed"
        c.save_state(st, f"state: {tid} 讀不到貼文")
        c.push_event("publish_failed", tid, f"「{title}」讀不到貼文內容：{e}")
        return "failed"
    st["errors"].pop("package", None)
    title = post.get("title") or title

    for k in needed(post):
        if st["done"].get(k):
            print(f"{LABEL[k]} 之前已發，跳過")
            continue
        try:
            if k == "fb_post":
                pid = fb_post(post, files)
            elif k == "fb_comment":
                if not st["done"].get("fb_post"):
                    raise c.ApiError("FB 貼文沒發成功，留言跳過")
                pid = fb_comment(post, st["done"]["fb_post"])
            elif k == "ig":
                try:
                    pid = ig_post(post, files, tid)
                finally:
                    unstage_images(tid)
            else:
                pid = threads_post(post)
            st["done"][k] = pid
            st["errors"].pop(k, None)
            link = permalink(k, pid)
            if link:
                st["links"][k] = link
            print(f"✓ {LABEL[k]}")
        except c.ApiError as e:
            st["errors"][k] = str(e)
            print(f"✗ {LABEL[k]}：{e}")
        c.save_state(st, f"state: {tid} {LABEL[k]}")

    bad = [k for k in needed(post) if not st["done"].get(k)]
    st["result"] = "failed" if bad else "done"
    st["finished_at"] = c.iso(c.now())
    c.save_state(st, f"state: {tid} {st['result']}")
    if bad:
        ok = [LABEL[k] for k in needed(post) if st["done"].get(k)]
        why = "；".join(f"{LABEL[k]}：{st['errors'].get(k, '未知')}" for k in bad)
        c.push_event("publish_failed", tid, f"「{title}」{('、'.join(ok) + ' 已發；') if ok else ''}{why}",
                     {"links": st["links"], "failed": bad})
        return "failed"
    c.push_event("publish_done", tid, f"「{title}」已發佈到 FB、IG、Threads", {"links": st["links"]})
    return "done"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    c.pull()
    s = c.load_schedule()
    want = {x for x in a.ids.split(",") if x}
    t = c.now()
    if want:
        todo = [i for i in s["items"] if i["id"] in want]
        # 重試：已不在排程表（失敗後會被拿掉），以現在時間重建；已發的平台由狀態檔跳過
        todo += [{"id": x, "at": c.iso(t), "status": "scheduled"} for x in sorted(want - {i["id"] for i in todo})]
    else:
        todo = [i for i in s["items"] if i.get("status") == "publishing"
                or (i.get("status") in ("scheduled", "preview") and c.parse(i["at"]) <= t)]
    if not todo:
        print("沒有要發的貼文")
        return 0
    results = {}
    for item in sorted(todo, key=lambda i: i["at"]):
        print(f"── {item['id']}（{item['at']}）")
        results[item["id"]] = publish_one(item, a.force)
        c.pull()
        s = c.load_schedule()
        s["items"] = [i for i in s["items"] if i["id"] != item["id"]]
        c.save_schedule(s, f"publish: {item['id']} {results[item['id']]}")
    print("結果：" + "、".join(f"{k} {v}" for k, v in results.items()))
    return 1 if any(v == "failed" for v in results.values()) else 0


if __name__ == "__main__":
    sys.exit(main())

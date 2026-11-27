"""發佈：把到時間的貼文發到 FB（多圖＋粉專留言 vocus 連結）、IG（輪播）、Threads（純文字；速報主文可帶圖）。

用法：python scripts/publish.py [--ids id1,id2] [--force]
  沒給 --ids：處理排程表裡 status=publishing 的，以及已到時間的 scheduled／preview。
  --force：重試用（發文助手在 Derrick 按「重試」時帶上），不套用 60 分鐘逾時規則。

貼文內容在私人 repo 的 publish/<todo id>/（post.json＋JPEG），發文前一刻才讀。格式：
  {"id", "code", "line", "title", "vocus_url",
   "fb": {"message", "images": ["fb-01.jpg", …], "comment"},
   "ig": {"caption", "images": ["ig-01.jpg", …]},
   "threads": {"text", "reply"}}   # reply（2026-11-27 起）：第一則回覆，放 CTA 與 vocus 連結；沒有就不發回覆
速報（2026-11-27 起，kind=flash／digest）：
  {"id", "kind", "code", "title", "platforms": ["threads", …],
   "threads": {"posts": ["主文", "回覆 1", "回覆 2", …], "images": ["01.jpg", …],   # 第一則主文（媒體全掛這則：1 個單則、2–10 個輪播），其餘依序串成純文字回覆
               "media": [{"type": "image", "file": "01.jpg"}, {"type": "image", "url": "https://…"}, {"type": "video", "url": "https://….mp4"}]},
   "fb": {"message", "images": ["01.jpg", …], "media": [同上], "comment"}}
   # images 是舊欄位（只有上傳的檔）；media 是 2026-11-27 起的完整清單（含網址圖、網址影片，照順序）。兩者都沒有就發純文字。
   # 網址媒體由 scripts/media.py 在發文前一刻下載：圖轉 JPEG 放 tmp/（有 R2 放 R2），影片一定放 R2；發完刪。
   # FB：只有圖 → 多圖貼文；只有 1 支影片 → 影片貼文（file_url）；影片＋圖或多支影片 → 這則 FB 失敗（Threads 照發）。

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
import media as m

PLATFORMS = ("fb_post", "fb_comment", "ig", "threads", "threads_reply")
_LABEL = {"fb_post": "FB 貼文", "fb_comment": "FB 留言", "ig": "IG", "threads": "Threads", "threads_reply": "Threads 回覆"}


class _Label(dict):
    """threads_reply2、threads_reply3… 是速報串文的第 2、3 則回覆。"""
    def __missing__(self, k):
        return f"Threads 回覆 {k[len('threads_reply'):]}" if k.startswith("threads_reply") else k


LABEL = _Label(_LABEL)


def reply_key(n: int) -> str:
    """第 n 則回覆（1 起算）的狀態鍵：第 1 則沿用 threads_reply，之後 threads_reply2…"""
    return "threads_reply" if n == 1 else f"threads_reply{n}"


def threads_posts(post) -> list[str]:
    """把 threads 欄位整理成 [主文, 回覆 1, 回覆 2, …]；舊格式 {text, reply} 與速報格式 {posts} 都接受。"""
    t = post.get("threads") or {}
    if t.get("posts"):
        return [x for x in t["posts"] if (x or "").strip()]
    out = [t.get("text", "")]
    if (t.get("reply") or "").strip():
        out.append(t["reply"])
    return out


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
    want = set((post.get("fb") or {}).get("images", [])) | set((post.get("ig") or {}).get("images", [])) \
        | set((post.get("threads") or {}).get("images", [])) \
        | {x["file"] for p in ("threads", "fb") for x in ((post.get(p) or {}).get("media") or []) if x.get("file")}
    missing = want - names
    if missing:
        raise c.ApiError(f"缺圖：{', '.join(sorted(missing))}")
    files = {n: c.gh("GET", f"{base}/{n}?ref=main", raw=True) for n in sorted(want)}
    for n, b in files.items():
        if not (b.startswith(b"\xff\xd8") or b.startswith(b"\x89PNG")):
            raise c.ApiError(f"{n} 不是 JPEG／PNG")
    return post, files


def mime(b: bytes) -> str:
    return "image/png" if b.startswith(b"\x89PNG") else "image/jpeg"


# ---------- 媒體清單（上傳的檔＋網址圖＋網址影片） ----------
def media_of(post, platform: str) -> list[dict]:
    """統一成 [{"type": "image"|"video", "file": 檔名} 或 {"type", "url"}]。沒有 media 欄位時用舊的 images。"""
    sec = post.get(platform) or {}
    if sec.get("media"):
        return [x for x in sec["media"] if isinstance(x, dict) and (x.get("file") or x.get("url"))]
    return [{"type": "image", "file": n} for n in (sec.get("images") or [])]


class Resolved:
    """發文用的公開網址清單，以及發完要刪的東西。"""
    def __init__(self):
        self.items: list[tuple[str, str]] = []   # (type, public_url)
        self.r2_keys: list[str] = []
        self.staged = False
        self.tmpdir: "Path | None" = None

    def cleanup(self, todo_id: str):
        if self.staged:
            unstage_images(todo_id)
        m.r2_delete(self.r2_keys)
        if self.tmpdir:
            m.cleanup_dir(self.tmpdir)


def resolve_media(todo_id: str, media: list[dict], files: dict[str, bytes]) -> Resolved:
    """把媒體清單變成公開網址：上傳的檔放 tmp/；網址圖下載轉 JPEG（有 R2 放 R2，沒有放 tmp/）；網址影片下載後放 R2。"""
    from pathlib import Path
    res = Resolved()
    if not media:
        return res
    if len(media) > 10:
        raise c.ApiError(f"媒體 {len(media)} 個，超過 10 個")
    videos = [x for x in media if x.get("type") == "video"]
    if videos and not m.r2_enabled():
        raise c.ApiError("有影片網址但沒有設定 Cloudflare R2（R2_ACCOUNT_ID／ACCESS_KEY／SECRET／BUCKET／PUBLIC_BASE）")
    work = Path(c.ROOT) / "work" / todo_id
    res.tmpdir = work
    stage_names: list[str] = []
    stage_files: dict[str, bytes] = {}
    plan: list[tuple[str, str | None, str | None]] = []   # (type, tmp 檔名, 直接網址)
    for n, x in enumerate(media, 1):
        kind = "video" if x.get("type") == "video" else "image"
        if x.get("file"):
            if kind == "video":
                raise c.ApiError("上傳的影片檔還不支援，影片請貼網址")
            stage_names.append(x["file"]); stage_files[x["file"]] = files[x["file"]]
            plan.append(("image", x["file"], None))
            continue
        src, ext = m.download(x["url"], work / f"{n:02d}", kind)
        if kind == "image":
            jpg = m.to_jpeg(src, work / f"{n:02d}" / f"{n:02d}.jpg")
            if m.r2_enabled():
                key = f"flash/{todo_id}/{n:02d}.jpg"
                plan.append(("image", None, m.r2_put(key, jpg, "image/jpeg"))); res.r2_keys.append(key)
            else:
                name = f"url-{n:02d}.jpg"
                stage_names.append(name); stage_files[name] = jpg.read_bytes()
                plan.append(("image", name, None))
        else:
            key = f"flash/{todo_id}/{n:02d}.{ext}"
            plan.append(("video", None, m.r2_put(key, src, "video/quicktime" if ext == "mov" else "video/mp4"))); res.r2_keys.append(key)
    urls: dict[str, str] = {}
    if stage_names:
        for name, u in zip(stage_names, stage_images(todo_id, stage_names, stage_files)):
            urls[name] = u
        res.staged = True
    res.items = [(kind, direct or urls[name]) for kind, name, direct in plan]
    return res


# ---------- 各平台 ----------
def fb_post(post, files) -> str:
    page, tok = c.env("META_PAGE_ID"), c.env("META_PAGE_TOKEN")
    media = media_of(post, "fb")
    videos = [x for x in media if x.get("type") == "video"]
    msg = keep_blank_lines(post["fb"]["message"])
    if videos:
        if len(media) != 1:
            raise c.ApiError("FB 一則貼文只能發 1 支影片、不能和圖片混發；請改成只有一支影片，或 FB 不勾")
        tid = post.get("id", "")
        res = resolve_media(f"{tid}-fb", media, files)
        try:
            _, url = res.items[0]
            r = c.meta("POST", f"{c.GRAPH}/{page}/videos", data={"file_url": url, "description": msg, "access_token": tok})
            return r["id"]
        finally:
            res.cleanup(f"{tid}-fb")
    ids = []
    externals = [x for x in media if x.get("url")]
    res = resolve_media(f"{post.get('id', '')}-fb", externals, files) if externals else None
    try:
        ext_iter = iter(res.items if res else [])
        for x in media:
            if x.get("file"):
                r = c.meta("POST", f"{c.GRAPH}/{page}/photos", data={"published": "false", "access_token": tok},
                           files={"source": (x["file"], files[x["file"]], mime(files[x["file"]]))})
            else:
                _, u = next(ext_iter)
                r = c.meta("POST", f"{c.GRAPH}/{page}/photos", data={"published": "false", "url": u, "access_token": tok})
            ids.append(r["id"])
    finally:
        if res:
            res.cleanup(f"{post.get('id', '')}-fb")
    data = {"message": msg, "access_token": tok}
    for k, i in enumerate(ids):   # 沒圖（速報）就只有文字
        data[f"attached_media[{k}]"] = '{"media_fbid":"%s"}' % i
    return c.meta("POST", f"{c.GRAPH}/{page}/feed", data=data)["id"]


def fb_comment(post, post_id) -> str:
    return c.meta("POST", f"{c.GRAPH}/{post_id}/comments",
                  data={"message": keep_blank_lines(post["fb"]["comment"]), "access_token": c.env("META_PAGE_TOKEN")})["id"]


def wait_ready(url_base: str, cid: str, tok: str, field: str, ok: str, limit=300) -> None:
    """等容器處理完。剛建好的容器查狀態有時會回「media cannot be found（code 24）」（還沒同步），當成還沒好、繼續等；
    2026-11-27 16:15 的速報就是這樣失敗的，之前成功只是運氣。"""
    t0 = time.monotonic()
    time.sleep(3)
    while True:
        try:
            st = c.meta("GET", f"{url_base}/{cid}", params={"fields": field, "access_token": tok}).get(field, "")
        except c.ApiError as e:
            if "code 24" in str(e) and time.monotonic() - t0 < limit:
                time.sleep(5)
                continue
            raise
        if st == ok:
            return
        if st in ("ERROR", "EXPIRED"):
            raise c.ApiError(f"容器狀態 {st}")
        if time.monotonic() - t0 > limit:
            raise c.ApiError(f"等容器處理逾時（最後狀態 {st or '未知'}）")
        time.sleep(5)


def stage_images(todo_id: str, names, files) -> list[str]:
    """IG 與 Threads 要公開網址：圖暫放到這個公開 repo 的 tmp/<id>/，發完刪掉。"""
    d = c.ROOT / "tmp" / todo_id
    d.mkdir(parents=True, exist_ok=True)
    for n in names:
        (d / n).write_bytes(files[n])
    c.commit_push(f"tmp: {todo_id} 的圖（發完刪除）", [f"tmp/{todo_id}"])
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
        c.commit_push(f"tmp: 刪除 {todo_id} 的圖", [f"tmp/{todo_id}"])


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


def threads_text(text: str, reply_to: str = "") -> str:
    """發一則 Threads 純文字；reply_to 有值就發成那則的回覆。"""
    uid, tok = c.env("THREADS_USER_ID"), c.env("THREADS_TOKEN")
    text = keep_blank_lines(text)
    if len(text) > 500:
        raise c.ApiError(f"Threads 超過 500 字（{len(text)}）")
    data = {"media_type": "TEXT", "text": text, "access_token": tok}
    if reply_to:
        data["reply_to_id"] = reply_to
    cid = c.meta("POST", f"{c.THREADS}/{uid}/threads", data=data)["id"]
    wait_ready(c.THREADS, cid, tok, "status", "FINISHED", limit=120)
    return threads_publish(uid, tok, cid)


def threads_publish(uid: str, tok: str, cid: str) -> str:
    """發佈容器。發佈那一刻也可能遇到「media cannot be found（code 24）」（容器剛 FINISHED 還沒同步），最多重試 6 次、每次等 10 秒。"""
    for attempt in range(6):
        try:
            return c.meta("POST", f"{c.THREADS}/{uid}/threads_publish", data={"creation_id": cid, "access_token": tok})["id"]
        except c.ApiError as e:
            if "code 24" in str(e) and attempt < 5:
                time.sleep(10)
                continue
            raise


def _th_item(kind: str, url: str) -> dict:
    return {"media_type": "VIDEO", "video_url": url} if kind == "video" else {"media_type": "IMAGE", "image_url": url}


def threads_media(text: str, items: list[tuple[str, str]]) -> str:
    """發一則帶媒體的 Threads 主文：1 個＝單圖或單支影片，2–10 個＝輪播（可混圖與影片）。回覆仍是純文字。
    影片要等平台轉檔，等待上限放寬到 10 分鐘。"""
    uid, tok = c.env("THREADS_USER_ID"), c.env("THREADS_TOKEN")
    text = keep_blank_lines(text)
    if len(text) > 500:
        raise c.ApiError(f"Threads 超過 500 字（{len(text)}）")
    has_video = any(k == "video" for k, _ in items)
    if len(items) == 1:
        kind, url = items[0]
        cid = c.meta("POST", f"{c.THREADS}/{uid}/threads", data={**_th_item(kind, url), "text": text, "access_token": tok})["id"]
    else:
        kids = []
        for kind, url in items:
            k = c.meta("POST", f"{c.THREADS}/{uid}/threads", data={**_th_item(kind, url), "is_carousel_item": "true", "access_token": tok})["id"]
            wait_ready(c.THREADS, k, tok, "status", "FINISHED", limit=600 if kind == "video" else 120)
            kids.append(k)
        cid = c.meta("POST", f"{c.THREADS}/{uid}/threads", data={"media_type": "CAROUSEL", "children": ",".join(kids), "text": text, "access_token": tok})["id"]
    wait_ready(c.THREADS, cid, tok, "status", "FINISHED", limit=600 if has_video else 180)
    return threads_publish(uid, tok, cid)


def threads_post(post, files=None, todo_id: str = "") -> str:
    media = media_of(post, "threads")
    if not media:
        return threads_text(threads_posts(post)[0])
    res = resolve_media(todo_id, media, files or {})
    try:
        return threads_media(threads_posts(post)[0], res.items)
    finally:
        res.cleanup(todo_id)


def threads_reply(post, parent_id: str, n: int = 1) -> str:
    return threads_text(threads_posts(post)[n], reply_to=parent_id)


def permalink(kind: str, pid: str) -> str:
    try:
        if kind == "fb_post":
            p = c.meta("GET", f"{c.GRAPH}/{pid}", params={"fields": "permalink_url", "access_token": c.env("META_PAGE_TOKEN")}).get("permalink_url", "")
            return p if not p or p.startswith("http") else "https://www.facebook.com" + p   # 影片貼文回的是相對路徑
        if kind == "ig":
            return c.meta("GET", f"{c.GRAPH}/{pid}", params={"fields": "permalink", "access_token": c.env("META_PAGE_TOKEN")}).get("permalink", "")
        if kind == "threads":
            return c.meta("GET", f"{c.THREADS}/{pid}", params={"fields": "permalink", "access_token": c.env("THREADS_TOKEN")}).get("permalink", "")
    except c.ApiError:
        pass
    return ""


# ---------- 一則 ----------
def needed(post) -> list[str]:
    plats = post.get("platforms")   # 速報會指定平台；沒有這欄（一般素材）就看有哪些內容
    def on(p):
        return post.get(p) and (plats is None or p in plats)
    out = []
    if on("fb"):
        out.append("fb_post")
        if (post["fb"].get("comment") or "").strip():
            out.append("fb_comment")
    if on("ig") and (post["ig"].get("images") or []):
        out.append("ig")
    if on("threads"):
        out.append("threads")
        out += [reply_key(n) for n in range(1, len(threads_posts(post)))]
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
            elif k.startswith("threads_reply"):
                n = int(k[len("threads_reply"):] or 1)
                parent = st["done"].get("threads" if n == 1 else reply_key(n - 1))
                if not parent:
                    raise c.ApiError("前一則 Threads 沒發成功，回覆跳過")
                pid = threads_reply(post, parent, n)
            else:
                pid = threads_post(post, files, tid)
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
                     {"links": st["links"], "failed": bad, "kind": post.get("kind", "material")})
        return "failed"
    where = "、".join(dict.fromkeys(LABEL[k].split(" ")[0] for k in needed(post) if k in ("fb_post", "ig", "threads")))
    c.push_event("publish_done", tid, f"「{title}」已發佈到 {where}", {"links": st["links"], "kind": post.get("kind", "material")})
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

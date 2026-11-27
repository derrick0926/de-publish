"""外部媒體（2026-11-27 起）：速報／集錦可以在指揮台貼圖片網址或影片網址，不經過發文助手下載，
由這裡在發文前一刻處理：下載 → 圖片轉 JPEG → 放到公開位置（影片一律 Cloudflare R2；圖片有 R2 就放 R2，
沒有就和上傳的圖一樣放這個 repo 的 tmp/）→ 發完刪掉。

R2 設定（de-publish 的 Secrets）：R2_ACCOUNT_ID、R2_ACCESS_KEY_ID、R2_SECRET_ACCESS_KEY、R2_BUCKET、
R2_PUBLIC_BASE（bucket 的公開網址，例 https://media.example.com 或 https://pub-xxxx.r2.dev，結尾不加斜線）。
五個都有才算啟用；沒有 R2 時影片網址會讓那則發佈失敗（PM 通知），圖片網址照常。

限制：圖片下載後 ≤8 MB（轉 JPEG、長邊 ≤2048）；影片 MP4／MOV、≤300 MB（Threads 上限 1 GB、5 分鐘，
FB 用 file_url 也收 MP4）。只用標準函式庫下載；轉圖要 Pillow（workflow 會 pip install pillow pillow-avif-plugin），
R2 要 boto3；兩者都是在用到時才 import，離線測試不需要。
"""
from __future__ import annotations

import os
import re
import shutil
import urllib.error
import urllib.request
from pathlib import Path

import common as c

IMG_MAX_BYTES = 8 * 1024 * 1024
VID_MAX_BYTES = 300 * 1024 * 1024
IMG_MAX_SIDE = 2048
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36 de-publish"


def sniff(head: bytes) -> tuple[str, str]:
    """回傳 (kind, ext)：kind 是 image／video／unknown。"""
    if head.startswith(b"\xff\xd8"):
        return "image", "jpg"
    if head.startswith(b"\x89PNG"):
        return "image", "png"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image", "webp"
    if head[:4] == b"GIF8":
        return "image", "gif"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "image", "avif"
        if brand[:2] == b"qt":
            return "video", "mov"
        return "video", "mp4"   # isom、mp42、M4V 等
    return "unknown", ""


def download(url: str, dest: Path, expect: str) -> tuple[Path, str]:
    """下載 url 到 dest（資料夾），回傳 (檔案路徑, 副檔名)。expect 是 image 或 video，不符就丟 ApiError。"""
    if not re.match(r"^https?://", url or ""):
        raise c.ApiError(f"媒體網址不是 http(s)：{str(url)[:80]}")
    dest.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    limit = VID_MAX_BYTES if expect == "video" else IMG_MAX_BYTES
    tmp = dest / "download.bin"
    try:
        with urllib.request.urlopen(req, timeout=120) as r, open(tmp, "wb") as f:
            head = r.read(64)
            kind, ext = sniff(head)
            if kind != expect:
                raise c.ApiError(f"網址抓到的不是{'影片' if expect == 'video' else '圖片'}（{kind or '認不出格式'}）：{url[:80]}")
            f.write(head)
            total = len(head)
            while True:
                chunk = r.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise c.ApiError(f"{'影片' if expect == 'video' else '圖片'}超過 {limit // 1048576} MB：{url[:80]}")
                f.write(chunk)
    except urllib.error.HTTPError as e:
        raise c.ApiError(f"媒體網址下載失敗（HTTP {e.code}）：{url[:80]}") from None
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise c.ApiError(f"媒體網址下載失敗（{type(e).__name__}）：{url[:80]}") from None
    out = dest / f"source.{ext}"
    tmp.replace(out)
    return out, ext


def to_jpeg(src: Path, dst: Path) -> Path:
    """任何格式的圖轉成 JPEG（長邊 ≤2048、品質 90，白底壓平透明）。Threads／FB 都只收 JPEG／PNG。"""
    try:
        from PIL import Image
        try:
            import pillow_avif  # noqa: F401  AVIF 解碼外掛，沒裝就只支援 Pillow 原生格式
        except Exception:
            pass
    except ImportError:
        raise c.ApiError("缺 Pillow，無法把網址圖轉成 JPEG（workflow 要 pip install pillow）") from None
    with Image.open(src) as im:
        im.load()
        if im.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im.convert("RGBA"), mask=im.convert("RGBA").split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        w, h = im.size
        if w / h > 10 or h / w > 10:
            raise c.ApiError(f"圖片長寬比 {w}×{h} 超過 10:1，Threads 不收")
        scale = min(1.0, IMG_MAX_SIDE / max(w, h))
        if scale < 1:
            im = im.resize((max(1, round(w * scale)), max(1, round(h * scale))))
        q = 90
        while True:
            im.save(dst, "JPEG", quality=q, optimize=True)
            if dst.stat().st_size <= IMG_MAX_BYTES or q <= 60:
                break
            q -= 10
    return dst


# ---------- Cloudflare R2 ----------
R2_VARS = ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET", "R2_PUBLIC_BASE")


def r2_enabled() -> bool:
    return all(os.environ.get(k) for k in R2_VARS)


def _r2():
    try:
        import boto3
    except ImportError:
        raise c.ApiError("缺 boto3，無法上傳到 R2（workflow 要 pip install boto3）") from None
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def r2_put(key: str, path: Path, content_type: str) -> str:
    """上傳一個檔案到 R2，回傳公開網址。"""
    _r2().upload_file(str(path), os.environ["R2_BUCKET"], key, ExtraArgs={"ContentType": content_type})
    return f"{os.environ['R2_PUBLIC_BASE'].rstrip('/')}/{key}"


def r2_delete(keys: list[str]) -> None:
    if not keys:
        return
    try:
        _r2().delete_objects(Bucket=os.environ["R2_BUCKET"], Delete={"Objects": [{"Key": k} for k in keys], "Quiet": True})
    except Exception as e:  # 刪不掉不影響發佈結果，印出來人工處理
        print(f"::warning::R2 刪檔失敗（{len(keys)} 個）：{e}")


def cleanup_dir(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)

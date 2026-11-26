"""Threads 長效權杖自動更新：權杖 60 天有效、滿 24 小時才能更新。每 10 天跑一次，換到的新權杖寫回 Secret THREADS_TOKEN。

需要 PyNaCl（GitHub Secrets 要用 repo 的公鑰加密）。失敗會推事件給 PM。
"""
import base64
import sys
from datetime import timedelta

import common as c


def main() -> int:
    try:
        old = c.env("THREADS_TOKEN")
        r = c.meta("GET", "https://graph.threads.net/refresh_access_token",
                   params={"grant_type": "th_refresh_token", "access_token": old})
        new, ttl = r["access_token"], int(r.get("expires_in", 0))
        print(f"::add-mask::{new}")
        from nacl import encoding, public
        key = c.gh("GET", f"/repos/{c.OWNER}/{c.PUB}/actions/secrets/public-key")
        box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
        enc = base64.b64encode(box.encrypt(new.encode())).decode()
        c.gh("PUT", f"/repos/{c.OWNER}/{c.PUB}/actions/secrets/THREADS_TOKEN",
             {"encrypted_value": enc, "key_id": key["key_id"]})
        exp = c.now() + timedelta(seconds=ttl)
        print(f"✓ Threads 權杖已更新，新到期日 {exp:%Y-%m-%d}")
        return 0
    except Exception as e:  # 任何失敗都要讓 PM 知道：超過 60 天沒更新就得重跑設定教學第四部分
        msg = str(e) if isinstance(e, c.ApiError) else type(e).__name__
        print(f"::error::Threads 權杖更新失敗：{msg}")
        c.push_event("publish_warn", "threads-token", f"Threads 權杖自動更新失敗：{msg}。舊權杖 60 天內仍可用，請 PM 安排處理")
        return 1


if __name__ == "__main__":
    sys.exit(main())

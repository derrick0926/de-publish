"""只讀檢查：確認 8 個 Secrets 都在、權杖有效。不發文、不寫入任何東西，也不印出金鑰。"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GRAPH = "https://graph.facebook.com/v25.0"
THREADS = "https://graph.threads.net/v1.0"
GH = "https://api.github.com"
OWNER = "derrick0926"
NAMES = ["META_APP_ID", "META_APP_SECRET", "META_PAGE_ID", "META_PAGE_TOKEN", "META_IG_USER_ID",
         "THREADS_USER_ID", "THREADS_TOKEN", "PUBLISH_GH_TOKEN"]
NEED_SCOPES = {"pages_manage_posts", "pages_manage_engagement", "pages_read_engagement",
               "instagram_basic", "instagram_content_publish"}

rows, failed = [], False
soon = ""   # 粉專資料存取期限 21 天內到期時記下日期


def report(ok, item, detail):
    global failed
    mark = {True: "✓", False: "✗", None: "⚠"}[ok]
    failed |= ok is False
    rows.append(f"| {mark} | {item} | {detail} |")
    print(f"{mark} {item}：{detail}")


def get(url, params=None, headers=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read() or "{}")
        except ValueError:
            body = {}
        return e.code, body


def err(body):
    e = body.get("error", body)
    return str(e.get("message", e))[:160] if isinstance(e, dict) else str(e)[:160]


S = {n: os.environ.get(n, "").strip() for n in NAMES}
missing = [n for n in NAMES if not S[n]]
report(not missing, "8 個 Secrets", "都讀得到" if not missing else "缺少或空白：" + "、".join(missing))

# ---- Facebook 粉專 ----
if S["META_APP_ID"] and S["META_APP_SECRET"] and S["META_PAGE_TOKEN"]:
    code, b = get(f"{GRAPH}/debug_token", {"input_token": S["META_PAGE_TOKEN"],
                                            "access_token": f'{S["META_APP_ID"]}|{S["META_APP_SECRET"]}'})
    d = b.get("data", {})
    if code != 200 or not d.get("is_valid"):
        report(False, "粉專權杖", "無效：" + err(d or b))
    else:
        report(d.get("type") == "PAGE", "粉專權杖類型", d.get("type"))
        report(d.get("expires_at", 0) == 0 or None, "粉專權杖到期",
               "永不" if d.get("expires_at", 0) == 0 else datetime.fromtimestamp(d["expires_at"], timezone.utc).strftime("%Y-%m-%d"))
        dax = d.get("data_access_expires_at")
        if dax:
            day = datetime.fromtimestamp(dax, timezone.utc)
            left = (day - datetime.now(timezone.utc)).days
            if left <= 21:
                soon = day.strftime("%Y-%m-%d")
            report(left > 21 or None, "資料存取期限", day.strftime("%Y-%m-%d") + f"（剩 {left} 天；之前要重跑設定教學第三部分）")
        lack = NEED_SCOPES - set(d.get("scopes", []))
        report(not lack, "權限", "發文、留言、IG 發佈都在" if not lack else "缺少：" + "、".join(sorted(lack)))
        report(d.get("app_id") == S["META_APP_ID"], "App 編號對得上", "是" if d.get("app_id") == S["META_APP_ID"] else "權杖不是這個 App 發的")
if S["META_PAGE_ID"] and S["META_PAGE_TOKEN"]:
    code, b = get(f"{GRAPH}/{S['META_PAGE_ID']}", {"fields": "name,instagram_business_account",
                                                   "access_token": S["META_PAGE_TOKEN"]})
    if code != 200:
        report(False, "讀取粉專", err(b))
    else:
        report(True, "粉專", b.get("name", "?"))
        ig = (b.get("instagram_business_account") or {}).get("id")
        report(ig == S["META_IG_USER_ID"], "粉專連結的 IG",
               "和 META_IG_USER_ID 一致" if ig == S["META_IG_USER_ID"] else f"不一致（粉專連的是 {ig or '無'}）")

# ---- Instagram ----
if S["META_IG_USER_ID"] and S["META_PAGE_TOKEN"]:
    code, b = get(f"{GRAPH}/{S['META_IG_USER_ID']}", {"fields": "username", "access_token": S["META_PAGE_TOKEN"]})
    report(code == 200, "IG 帳號", "@" + b.get("username", "?") if code == 200 else err(b))
    code, b = get(f"{GRAPH}/{S['META_IG_USER_ID']}/content_publishing_limit",
                  {"fields": "quota_usage,config", "access_token": S["META_PAGE_TOKEN"]})
    if code == 200 and b.get("data"):
        q = b["data"][0]
        report(True, "IG 發文額度", f"24 小時內已用 {q.get('quota_usage', 0)}／{q.get('config', {}).get('quota_total', 100)}")
    else:
        report(False, "IG 發文額度", err(b))

# ---- Threads ----
if S["THREADS_TOKEN"]:
    code, b = get(f"{THREADS}/me", {"fields": "id,username", "access_token": S["THREADS_TOKEN"]})
    if code != 200:
        report(False, "Threads 權杖", "無效：" + err(b))
    else:
        report(True, "Threads 帳號", "@" + b.get("username", "?"))
        report(b.get("id") == S["THREADS_USER_ID"], "THREADS_USER_ID",
               "一致" if b.get("id") == S["THREADS_USER_ID"] else "和權杖的帳號不一致")
        code, b = get(f"{THREADS}/{S['THREADS_USER_ID']}/threads_publishing_limit",
                      {"fields": "quota_usage,config", "access_token": S["THREADS_TOKEN"]})
        if code == 200 and b.get("data"):
            q = b["data"][0]
            report(True, "Threads 發文額度", f"24 小時內已用 {q.get('quota_usage', 0)}／{q.get('config', {}).get('quota_total', 250)}")
        else:
            report(None, "Threads 發文額度", "讀不到：" + err(b))
        # 回覆權限（threads_manage_replies）：速報串文與一般素材的第一則回覆都要用；缺它主文會發出、回覆被拒（code 10，2026-11-27 實發時遇到）
        code, b = get(f"{THREADS}/{S['THREADS_USER_ID']}/threads_publishing_limit",
                      {"fields": "reply_quota_usage,reply_config", "access_token": S["THREADS_TOKEN"]})
        if code == 200 and b.get("data"):
            q = b["data"][0]
            report(True, "Threads 回覆權限", f"有（24 小時內已回覆 {q.get('reply_quota_usage', 0)}／{q.get('reply_config', {}).get('quota_total', 1000)}）")
        else:
            report(False, "Threads 回覆權限", "缺 threads_manage_replies：請在 Meta 應用程式的 Threads 使用案例加這個權限，重跑 setup_tokens.py threads 換權杖（" + err(b) + "）")

# ---- GitHub 金鑰 ----
if S["PUBLISH_GH_TOKEN"]:
    h = {"Authorization": f"Bearer {S['PUBLISH_GH_TOKEN']}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    for repo in ("de-publish", "de-marketing-archive"):
        code, b = get(f"{GH}/repos/{OWNER}/{repo}", headers=h)
        report(code == 200, f"金鑰讀得到 {repo}", "是" if code == 200 else f"HTTP {code}：{err(b)}")
        code, b = get(f"{GH}/repos/{OWNER}/{repo}/actions/workflows", headers=h)
        report(code == 200, f"金鑰有 {repo} 的 Actions 權限", "是" if code == 200 else f"HTTP {code}：{err(b)}")
    code, b = get(f"{GH}/repos/{OWNER}/de-publish/actions/secrets/public-key", headers=h)
    report(code == 200, "金鑰有 de-publish 的 Secrets 權限", "是（日後自動更新 Threads 權杖要用）" if code == 200 else f"HTTP {code}：{err(b)}")
    code, b = get(f"{GH}/repos/{OWNER}/de-publish/actions/permissions/workflow", headers=h)
    if code == 200:
        p = b.get("default_workflow_permissions")
        report(p == "write", "Workflow permissions", "Read and write" if p == "write" else f"目前是 {p}，要改成 Read and write")
    else:
        report(None, "Workflow permissions", "這把金鑰讀不到設定，請到 Settings → Actions → General 目視確認")

summary = os.environ.get("GITHUB_STEP_SUMMARY")
if summary:
    with open(summary, "a", encoding="utf-8") as f:
        f.write("## 發文助手設定檢查（只讀，不發文）\n\n| | 項目 | 結果 |\n|---|---|---|\n" + "\n".join(rows) + "\n")
print("\n結果：" + ("有項目要修正" if failed else "全部通過"))
if os.environ.get("NOTIFY") and (failed or soon):   # 每週定時檢查：有問題才通知 PM
    sys.path.insert(0, os.path.dirname(__file__))
    import common
    bad = [r.split("|")[2].strip() for r in rows if r.startswith("| ✗")]
    text = ("設定檢查有問題：" + "、".join(bad) + "。" if bad else "") + \
           (f"粉專權杖的資料存取期限 {soon} 快到了，請在那之前重跑設定教學第三部分。" if soon else "")
    common.push_event("publish_warn", "setup-check", text)
sys.exit(1 if failed else 0)

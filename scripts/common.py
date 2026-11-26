"""共用：時間、HTTP、GitHub、排程表。只用標準函式庫。

這是公開 repo：任何 print 都會出現在公開的 Actions 紀錄裡，所以錯誤訊息只印 API 回的 message，
不印網址（網址可能帶 access_token）、不印金鑰。
"""
import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

TPE = timezone(timedelta(hours=8))
GRAPH = "https://graph.facebook.com/v25.0"
THREADS = "https://graph.threads.net/v1.0"
GH = "https://api.github.com"
OWNER = "derrick0926"
PUB = "de-publish"            # 這個 repo（公開）
PRIV = "de-marketing-archive"  # 貼文內容與事件（私人）
ROOT = Path(__file__).resolve().parent.parent
SCHEDULE = ROOT / "schedule.json"
STATE = ROOT / "state"

LATE_LIMIT = timedelta(minutes=60)   # 逾時規則（Derrick 2026-11-25）：晚 60 分鐘內照常補發，超過就不發


class ApiError(Exception):
    pass


def now() -> datetime:
    return datetime.now(TPE)


def parse(s: str) -> datetime:
    d = datetime.fromisoformat(s)
    return d if d.tzinfo else d.replace(tzinfo=TPE)


def iso(d: datetime) -> str:
    return d.astimezone(TPE).isoformat(timespec="seconds")


def env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise ApiError(f"缺少 Secret {name}")
    return v


# ---------- HTTP ----------
def _multipart(fields: dict, files: dict):
    boundary = uuid.uuid4().hex
    out = bytearray()
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, data, ctype) in files.items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                f"Content-Type: {ctype}\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return bytes(out), f"multipart/form-data; boundary={boundary}"


def http(method: str, url: str, params=None, data=None, files=None, headers=None, raw=False, timeout=90):
    """回傳 (狀態碼, JSON 或 bytes)。網路錯誤重試 3 次；4xx/5xx 不重試，由呼叫端決定。"""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    body, h = None, dict(headers or {})
    if files:
        body, h["Content-Type"] = _multipart(data or {}, files)
    elif isinstance(data, (bytes, bytearray)):
        body = bytes(data)
    elif data is not None:
        body = urllib.parse.urlencode(data).encode()
        h.setdefault("Content-Type", "application/x-www-form-urlencoded")
    for attempt in range(3):
        req = urllib.request.Request(url, data=body, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                b = r.read()
                return r.status, (b if raw else json.loads(b or b"{}"))
        except urllib.error.HTTPError as e:
            b = e.read()
            try:
                return e.code, (b if raw else json.loads(b or b"{}"))
            except ValueError:
                return e.code, {"error": {"message": b[:200].decode("utf-8", "replace")}}
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == 2:
                raise ApiError(f"網路錯誤：{type(e).__name__}") from None
            time.sleep(5 * (attempt + 1))


def err_msg(body) -> str:
    if isinstance(body, dict):
        e = body.get("error", body)
        if isinstance(e, dict):
            m = e.get("error_user_msg") or e.get("message") or json.dumps(e, ensure_ascii=False)
            code = e.get("code")
            return f"{m}（code {code}）" if code else str(m)
        return str(e)[:300]
    return str(body)[:300]


def meta(method: str, url: str, **kw):
    """呼叫 Meta／Threads API，失敗丟 ApiError（訊息不含權杖）。"""
    code, body = http(method, url, **kw)
    if code >= 400 or (isinstance(body, dict) and "error" in body):
        raise ApiError(err_msg(body))
    return body


# ---------- GitHub ----------
def gh(method: str, path: str, payload=None, token_env="PUBLISH_GH_TOKEN", raw=False, ok=(200, 201, 204)):
    h = {"Authorization": f"Bearer {env(token_env)}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "de-publish"}
    if raw:
        h["Accept"] = "application/vnd.github.raw+json"
    data = None
    if payload is not None:
        data = json.dumps(payload).encode()
        h["Content-Type"] = "application/json"
    code, body = http(method, GH + path, data=data, headers=h, raw=raw)
    if code not in ok:
        raise ApiError(f"GitHub {method} {path.split('?')[0]} → HTTP {code}：{err_msg(body) if not raw else ''}")
    return body


def dispatch(workflow: str, inputs: dict | None = None, tries: int = 3) -> bool:
    """啟動這個 repo 的另一個 workflow；失敗重試（間隔 30 秒）。"""
    for i in range(tries):
        try:
            gh("POST", f"/repos/{OWNER}/{PUB}/actions/workflows/{workflow}/dispatches",
               {"ref": "main", "inputs": inputs or {}})
            print(f"已啟動 {workflow} {inputs or ''}")
            return True
        except ApiError as e:
            print(f"::warning::啟動 {workflow} 失敗（第 {i + 1} 次）：{e}")
            if i < tries - 1:
                time.sleep(30)
    return False


def active_runs(workflow: str) -> int:
    n = 0
    for st in ("in_progress", "queued", "waiting", "pending"):
        r = gh("GET", f"/repos/{OWNER}/{PUB}/actions/workflows/{workflow}/runs?status={st}&per_page=20")
        n += len([x for x in r.get("workflow_runs", []) if str(x.get("id")) != os.environ.get("GITHUB_RUN_ID")])
    return n


def push_event(etype: str, todo_id: str, text: str, extra: dict | None = None) -> None:
    """推事件到私人 repo 的 events/，由那邊的 dispatch.yml 叫醒 PM 即時通知（docs/events.md）。"""
    t = now()
    ev = {"type": etype, "todo_id": todo_id, "date": t.strftime("%Y-%m-%d"), "by": "publisher",
          "at": iso(t), "text": text, **(extra or {})}
    name = f"events/{t:%Y%m%d-%H%M%S}-{etype}-{todo_id}.json"
    content = base64.b64encode(json.dumps(ev, ensure_ascii=False, indent=1).encode()).decode()
    for i in range(3):
        try:
            gh("PUT", f"/repos/{OWNER}/{PRIV}/contents/{name}",
               {"message": f"events: {etype} {todo_id}", "content": content,
                "committer": {"name": "derrick0926", "email": "derrick0926@users.noreply.github.com"}})
            print(f"已推事件 {etype} {todo_id}")
            return
        except ApiError as e:
            print(f"::warning::推事件失敗（第 {i + 1} 次）：{e}")
            time.sleep(5)


# ---------- git（這個 repo）----------
def git(*args, check=True) -> str:
    r = subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True)
    if check and r.returncode:
        raise ApiError(f"git {args[0]} 失敗：{r.stderr.strip()[:300]}")
    return r.stdout


def commit_push(msg: str, paths: list[str]) -> None:
    if os.environ.get("DRY_GIT"):
        return
    git("add", "-A", "--", *paths)
    if not git("status", "--porcelain", "--", *paths).strip():
        return
    git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit", "-m", msg, "--quiet")
    for i in range(5):
        if subprocess.run(["git", "-C", str(ROOT), "pull", "--rebase", "--quiet"]).returncode == 0 and \
           subprocess.run(["git", "-C", str(ROOT), "push", "--quiet"]).returncode == 0:
            return
        time.sleep(5)
    raise ApiError("push 失敗 5 次")


def pull() -> None:
    if not os.environ.get("DRY_GIT"):
        git("pull", "--rebase", "--quiet", check=False)


# ---------- 排程表與狀態 ----------
def load_schedule() -> dict:
    if not SCHEDULE.exists():
        return {"rev": 0, "items": []}
    return json.loads(SCHEDULE.read_text(encoding="utf-8"))


def save_schedule(s: dict, msg: str) -> None:
    s["rev"] = int(s.get("rev", 0)) + 1
    s["updated_at"] = iso(now())
    SCHEDULE.write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    commit_push(msg, ["schedule.json"])


def load_state(todo_id: str) -> dict:
    p = STATE / f"{todo_id}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"id": todo_id, "done": {}, "errors": {}}


def save_state(st: dict, msg: str) -> None:
    STATE.mkdir(exist_ok=True)
    st["updated_at"] = iso(now())
    (STATE / f"{st['id']}.json").write_text(json.dumps(st, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    commit_push(msg, [f"state/{st['id']}.json"])

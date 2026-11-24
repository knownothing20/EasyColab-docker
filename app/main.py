"""EasyColab Web Console - FastAPI backend.

Thin orchestration layer around the upstream `colab_bridge.py` CLI plus an
authenticated HTTP proxy to the Colab-side `app.py` FastAPI service.

No Qt, no browser, no VNC: everything the desktop client did over PySide6 is
re-exposed as JSON endpoints for a small static web page.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("EASYCOLAB_DATA_DIR", "/data"))
REPO_DIR = Path(os.environ.get("EASYCOLAB_REPO_DIR", str(BASE_DIR.parent / "easycolab")))
BRIDGE = REPO_DIR / "colab_bridge.py"
APP_PY = REPO_DIR / "app.py"
SETTINGS_PATH = DATA_DIR / "settings.json"
STATIC_DIR = BASE_DIR / "static"

DATA_DIR.mkdir(parents=True, exist_ok=True)

PASSWORD_RE = re.compile(r"[A-Za-z0-9_-]{24,256}")


# ---------------------------------------------------------------------------
# Settings (persisted in the mounted volume so a container restart keeps them)
# ---------------------------------------------------------------------------
_SETTINGS_LOCK = threading.Lock()


def _default_settings() -> Dict[str, Any]:
    return {
        "admin_password": "",
        "server_url": "",
        "gpu": "G4",
        "idle_minutes": 30,
        "keepalive": False,
        "theme": "light",
        # 代理：留空表示直连。网页上可改，保存后立即生效，无需重建容器
        "proxy_url": "",
        "no_proxy": "localhost,127.0.0.1,192.168.0.0/16,10.0.0.0/8,172.16.0.0/12",
    }


def load_settings() -> Dict[str, Any]:
    base = _default_settings()
    try:
        raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            base.update({k: v for k, v in raw.items() if k in base})
    except Exception:
        pass
    return base


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    with _SETTINGS_LOCK:
        cur = load_settings()
        for k, v in patch.items():
            if k in cur:
                cur[k] = v
        SETTINGS_PATH.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        return cur


def ensure_password() -> str:
    cfg = load_settings()
    pw = str(cfg.get("admin_password") or "")
    if PASSWORD_RE.fullmatch(pw):
        return pw
    pw = secrets.token_urlsafe(32)
    save_settings({"admin_password": pw})
    return pw


# ---------------------------------------------------------------------------
# Bridge CLI plumbing
# ---------------------------------------------------------------------------

def _bridge_env() -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("HOME", str(DATA_DIR))
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    # 网页上配置的代理优先于容器环境变量；留空则回退到容器环境变量
    cfg = load_settings()
    proxy = str(cfg.get("proxy_url") or "").strip()
    no_proxy = str(cfg.get("no_proxy") or "").strip()
    if proxy:
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    else:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
    if no_proxy:
        env["NO_PROXY"] = no_proxy
        env["no_proxy"] = no_proxy
    return env


def _run_bridge(args: List[str], timeout: int = 120) -> Dict[str, Any]:
    """Run a short bridge CLI command and return parsed stdout/stderr."""
    if not BRIDGE.exists():
        return {"ok": False, "error": f"bridge missing at {BRIDGE}"}
    try:
        proc = subprocess.run(
            [sys.executable, "-u", str(BRIDGE), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_bridge_env(),
            cwd=str(REPO_DIR),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "bridge command timed out"}
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    payload: Any = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except Exception:
                continue
    return {"ok": proc.returncode == 0, "code": proc.returncode, "stdout": out, "stderr": err, "data": payload}


# ---------------------------------------------------------------------------
# Long-running bridge jobs (wake / keepalive) with a shared log ring
# ---------------------------------------------------------------------------
class BridgeJob:
    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.lines: deque = deque(maxlen=600)
        self.proc: Optional[subprocess.Popen] = None
        self.state = "idle"
        self.url = ""
        self.error = ""
        self.started_at = 0.0
        self._thread: Optional[threading.Thread] = None

    def log(self, text: str) -> None:
        with self.lock:
            self.lines.append(f"[{time.strftime('%H:%M:%S')}] {text}")

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "name": self.name,
                "state": self.state,
                "url": self.url,
                "error": self.error,
                "running": self.proc is not None and self.proc.poll() is None,
                "startedAt": self.started_at,
                "lines": list(self.lines)[-260:],
            }

    def start(self, args: List[str], env_extra: Optional[Dict[str, str]] = None) -> None:
        if self.proc is not None and self.proc.poll() is None:
            raise RuntimeError("job already running")
        env = _bridge_env()
        if env_extra:
            env.update(env_extra)
        self.lines.clear()
        self.url = ""
        self.error = ""
        self.state = "starting"
        self.started_at = time.time()
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(BRIDGE), *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
            cwd=str(REPO_DIR),
        )
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        proc = self.proc
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=8)
            except Exception:
                proc.kill()
        self.state = "stopped"

    def _pump(self) -> None:
        proc = self.proc
        assert proc is not None and proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            if not line:
                continue
            if line.startswith("KEEPALIVE_JSON:"):
                try:
                    data = json.loads(line[len("KEEPALIVE_JSON:"):])
                except Exception:
                    data = {"raw": line}
                self.log("keepalive " + json.dumps(data, ensure_ascii=False)[:400])
                if data.get("paid_balance") is not None:
                    self.state = "alive"
                continue
            if line.startswith("WAKE_LOG:"):
                self.log(line[len("WAKE_LOG:"):].strip())
                continue
            if line.startswith("WAKE_URL:"):
                self.url = line[len("WAKE_URL:"):].strip()
                self.state = "ready"
                save_settings({"server_url": self.url})
                self.log("隧道已就绪: " + self.url)
                continue
            if line.startswith("WAKE_ERROR:"):
                self.error = line[len("WAKE_ERROR:"):].strip()
                self.state = "error"
                self.log("失败: " + self.error)
                continue
            self.log(line)
        rc = proc.wait()
        if self.state not in ("error",):
            self.state = "done" if rc == 0 else "error"
        self.log(f"进程退出 (code={rc})")


WAKE = BridgeJob("wake")
KEEPALIVE = BridgeJob("keepalive")


# ---------------------------------------------------------------------------
# Cloud proxy helpers (Colab-side app.py, HTTP Basic as user "easycolab")
# ---------------------------------------------------------------------------
_LOCAL = threading.local()


def _session() -> requests.Session:
    sess = getattr(_LOCAL, "sess", None)
    if sess is None:
        sess = requests.Session()
        sess.trust_env = False
        _LOCAL.sess = sess
    # 每次调用前同步代理，保证网页上改完立即生效
    cfg = load_settings()
    proxy = str(cfg.get("proxy_url") or "").strip()
    if proxy:
        sess.proxies = {"http": proxy, "https": proxy}
    else:
        # 未配置时回退到容器环境变量（若有）
        env_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
        sess.proxies = {"http": env_proxy, "https": env_proxy} if env_proxy else {}
    return sess


def _cloud_base() -> str:
    cfg = load_settings()
    url = str(cfg.get("server_url") or "").strip()
    return url.rstrip("/")


def _cloud_call(method: str, path: str, body: Any = None, timeout: int = 40, stream: bool = False):
    base = _cloud_base()
    if not base:
        raise HTTPException(status_code=409, detail="尚未连接 Colab 服务，请先唤醒实例并部署")
    pw = ensure_password()
    url = base + path
    try:
        resp = _session().request(
            method,
            url,
            json=body,
            auth=("easycolab", pw),
            timeout=timeout,
            stream=stream,
            verify=False,
        )
    except requests.exceptions.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"云端请求失败: {exc}") from exc
    if stream:
        return resp
    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="管理密码不匹配，请重新唤醒部署以同步密码")
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:400])
    try:
        return resp.json()
    except Exception:
        return {"ok": True, "raw": resp.text[:2000]}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="EasyColab Web Console", docs_url=None, redoc_url=None)


@app.get("/api/state")
def api_state() -> Dict[str, Any]:
    cfg = load_settings()
    auth = _run_bridge(["auth_status"], timeout=90)
    data = auth.get("data") or {}
    logged_in = bool(data.get("logged_in"))
    sessions = data.get("sessions") or []
    return {
        "ok": True,
        "loggedIn": logged_in,
        # 上游 bridge 的 auth_status 返回 email / paid_balance / burn_rate_hourly，
        # 并没有 account 这个嵌套对象，此前取错字段导致前端拿不到邮箱
        "email": data.get("email") or "",
        "cuBalance": data.get("paid_balance"),
        "cuRate": data.get("burn_rate_hourly"),
        "activeAssignments": data.get("active_assignments", 0),
        "account": {"email": data.get("email") or ""},
        "sessions": sessions,
        "serverUrl": cfg.get("server_url") or "",
        "passwordConfigured": bool(PASSWORD_RE.fullmatch(str(cfg.get("admin_password") or ""))),
        "gpu": cfg.get("gpu"),
        "idleMinutes": cfg.get("idle_minutes"),
        "wake": WAKE.snapshot(),
        "keepalive": KEEPALIVE.snapshot(),
        "bridgeOk": BRIDGE.exists(),
        "repoDir": str(REPO_DIR),
        "proxyUrl": cfg.get("proxy_url") or "",
        "noProxy": cfg.get("no_proxy") or "",
    }


@app.post("/api/settings")
def api_settings(patch: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    allowed = {k: v for k, v in patch.items() if k in {"server_url", "gpu", "idle_minutes", "theme", "proxy_url", "no_proxy"}}
    if "admin_password" in patch and PASSWORD_RE.fullmatch(str(patch["admin_password"])):
        allowed["admin_password"] = str(patch["admin_password"])
    cfg = save_settings(allowed)
    return {"ok": True, "settings": cfg}


@app.post("/api/auth/url")
def api_auth_url() -> Dict[str, Any]:
    res = _run_bridge(["auth_url"], timeout=90)
    data = res.get("data") or {}
    if not data.get("auth_url"):
        raise HTTPException(status_code=502, detail=res.get("stderr") or "生成授权链接失败")
    return {"ok": True, "authUrl": data["auth_url"]}


@app.post("/api/auth/code")
def api_auth_code(body: Dict[str, str] = Body(...)) -> Dict[str, Any]:
    code = (body.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="请粘贴授权码")
    res = _run_bridge(["auth_code", code], timeout=45)
    if not res.get("ok"):
        detail = res.get("stderr") or res.get("stdout") or res.get("error") or "授权失败"
        if "timed out" in str(detail).lower():
            detail = (
                "访问 Google OAuth 服务超时。请打开右上角「设置」，"
                "在「网络代理」里填入可用的代理地址（例如 http://192.168.x.x:7890），"
                "保存后点「测试连接」确认能访问 Google，再重新提交授权码"
            )
        raise HTTPException(status_code=400, detail=detail)
    return {"ok": True, "detail": res.get("stdout", "")[-800:]}


@app.post("/api/settings/test_proxy")
def api_test_proxy(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    """用提交的草稿代理测试连通性，不影响已保存的配置。"""
    proxy = str(body.get("proxy_url") or "").strip()
    target = str(body.get("target") or "https://oauth2.googleapis.com/").strip()
    if not target.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="测试地址必须是 http/https URL")

    proxies = {"http": proxy, "https": proxy} if proxy else {}
    sess = requests.Session()
    sess.trust_env = False
    started = time.time()
    try:
        resp = sess.get(target, proxies=proxies, timeout=12, verify=False)
        elapsed = round(time.time() - started, 2)
        return {
            "ok": True,
            "reachable": True,
            "status": resp.status_code,
            "elapsed": elapsed,
            "detail": f"连接成功，HTTP {resp.status_code}，耗时 {elapsed}s",
        }
    except requests.exceptions.ProxyError as exc:
        return {"ok": False, "reachable": False, "detail": f"代理连接失败，请检查代理地址与端口: {str(exc)[:200]}"}
    except requests.exceptions.ConnectTimeout:
        return {"ok": False, "reachable": False, "detail": "连接超时（12s）。代理不可用或目标不可达"}
    except requests.exceptions.SSLError as exc:
        return {"ok": False, "reachable": False, "detail": f"TLS 握手失败: {str(exc)[:200]}"}
    except requests.exceptions.RequestException as exc:
        return {"ok": False, "reachable": False, "detail": f"请求失败: {str(exc)[:200]}"}


@app.post("/api/auth/logout")
def api_auth_logout() -> Dict[str, Any]:
    res = _run_bridge(["auth_logout"], timeout=60)
    return {"ok": res.get("ok", False), "detail": res.get("stdout", "")[-400:]}


@app.post("/api/wake")
def api_wake(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    gpu = str(body.get("gpu") or load_settings().get("gpu") or "G4").upper()
    idle = body.get("idleMinutes")
    pw = ensure_password()
    env = {"EASYCOLAB_ADMIN_PASSWORD": pw}
    if idle is not None:
        try:
            env["EASYCOLAB_IDLE_TIMEOUT_MINUTES"] = str(int(idle))
            save_settings({"idle_minutes": int(idle)})
        except Exception:
            pass
    save_settings({"gpu": gpu})
    try:
        WAKE.start(["wake", "--gpu", gpu, "--app-py", str(APP_PY)], env)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "gpu": gpu}


@app.get("/api/wake")
def api_wake_status() -> Dict[str, Any]:
    return {"ok": True, "wake": WAKE.snapshot()}


@app.post("/api/wake/stop")
def api_wake_stop() -> Dict[str, Any]:
    WAKE.stop()
    return {"ok": True, "wake": WAKE.snapshot()}


@app.post("/api/keepalive")
def api_keepalive(body: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
    want = bool(body.get("on"))
    if want:
        try:
            KEEPALIVE.start(["keepalive_daemon"])
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        save_settings({"keepalive": True})
    else:
        KEEPALIVE.stop()
        save_settings({"keepalive": False})
    return {"ok": True, "keepalive": KEEPALIVE.snapshot()}


@app.get("/api/keepalive")
def api_keepalive_status() -> Dict[str, Any]:
    return {"ok": True, "keepalive": KEEPALIVE.snapshot()}


@app.post("/api/stop_all")
def api_stop_all() -> Dict[str, Any]:
    KEEPALIVE.stop()
    res = _run_bridge(["stop_all"], timeout=180)
    save_settings({"keepalive": False, "server_url": ""})
    return {"ok": True, "detail": res.get("stdout", "")[-800:], "stderr": res.get("stderr", "")[-400:]}


# ---- cloud proxy (1:1 mirrors the upstream /minimax_api/* contract) --------
@app.get("/api/cloud/status")
def api_cloud_status(model_id: Optional[str] = None) -> Any:
    q = f"?model_id={model_id}" if model_id else ""
    return _cloud_call("GET", f"/minimax_api/status{q}", timeout=45)


@app.get("/api/cloud/{path:path}")
def api_cloud_get(path: str, request: Request) -> Any:
    params = dict(request.query_params)
    qs = ""
    if params:
        qs = "?" + "&".join(f"{k}={v}" for k, v in params.items())
    return _cloud_call("GET", f"/minimax_api/{path}{qs}", timeout=45)


@app.post("/api/cloud/{path:path}")
async def api_cloud_post(path: str, request: Request) -> Any:
    try:
        body = await request.json()
    except Exception:
        body = None
    return _cloud_call("POST", f"/minimax_api/{path}", body=body, timeout=60)


@app.get("/api/media/{kind}/{filename}")
def api_media(kind: str, filename: str) -> StreamingResponse:
    resp = _cloud_call("GET", f"/minimax_api/{kind}/{filename}", timeout=120, stream=True)
    headers = {}
    if resp.headers.get("content-length"):
        headers["content-length"] = resp.headers["content-length"]
    return StreamingResponse(
        resp.iter_content(chunk_size=262144),
        media_type=resp.headers.get("content-type") or "application/octet-stream",
        headers=headers,
    )


@app.get("/api/comfy_media/{rel_path:path}")
def api_comfy_media(rel_path: str) -> StreamingResponse:
    resp = _cloud_call("GET", f"/minimax_api/comfyui/output_media/{rel_path}", timeout=300, stream=True)
    return StreamingResponse(resp.iter_content(chunk_size=262144), media_type=resp.headers.get("content-type") or "application/octet-stream")


# ---- local artifacts ------------------------------------------------------
OUTPUT_DIR = Path(os.environ.get("EASYCOLAB_OUTPUT_DIR", str(DATA_DIR / "outputs")))


@app.get("/api/local_outputs")
def api_local_outputs() -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    if OUTPUT_DIR.exists():
        for p in sorted(OUTPUT_DIR.rglob("*"), key=lambda x: x.stat().st_mtime, reverse=True)[:200]:
            if not p.is_file():
                continue
            ext = p.suffix.lower()
            mtype = "video" if ext in (".mp4", ".webm", ".mov") else "image" if ext in (".png", ".jpg", ".jpeg", ".webp") else "other"
            st = p.stat()
            items.append(
                {
                    "name": p.name,
                    "rel": str(p.relative_to(OUTPUT_DIR)),
                    "media_type": mtype,
                    "size_mb": round(st.st_size / 1048576, 2),
                    "mtime": int(st.st_mtime),
                }
            )
    return {"ok": True, "items": items}


@app.get("/api/local_file/{rel:path}")
def api_local_file(rel: str) -> FileResponse:
    target = (OUTPUT_DIR / rel).resolve()
    if not str(target).startswith(str(OUTPUT_DIR.resolve())) or not target.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    cfg = load_settings()
    return {
        "ok": True,
        "bridge": BRIDGE.exists(),
        "appPy": APP_PY.exists(),
        "repo": str(REPO_DIR),
        "serverUrl": cfg.get("server_url") or "",
        "python": sys.version.split()[0],
    }


if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
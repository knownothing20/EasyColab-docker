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
            alive = self.proc is not None and self.proc.poll() is None
            return {
                "name": self.name,
                "state": self.state,
                "url": self.url,
                "error": self.error,
                "running": alive,
                "startedAt": self.started_at,
                "pid": self.proc.pid if alive and self.proc else None,
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
        # stdin 保留为可写管道且永不关闭：上游 keepalive_daemon 一旦读到 EOF
        # 会立刻 stop_event.set() 并退出（原本是给桌面客户端关闭时用的信号）。
        # 容器的 stdin 是 /dev/null，直接给就会秒退，日志只留一行 code=0。
        self.proc = subprocess.Popen(
            [sys.executable, "-u", str(BRIDGE), *args],
            stdin=subprocess.PIPE,
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
                # 新实例刚起来，保活进程如果早就在跑（例如容器启动时恢复的），
                # 它内部要等满 480 秒才重新同步会话列表，这段时间是保活盲区。
                # 这里重启一次，让它立刻接管这个新实例。
                if self.name == "wake":
                    try:
                        if _KEEPALIVE_GUARD["want"]:
                            # start() 在旧进程还活着时会拒绝，必须先停再起
                            KEEPALIVE.stop()
                            KEEPALIVE.start(["keepalive_daemon"])
                            KEEPALIVE.log("新实例就绪，保活已重启并接管该实例")
                    except Exception as exc:  # noqa: BLE001
                        KEEPALIVE.log(f"保活接管新实例失败: {exc}")
                continue
            if line.startswith("WAKE_ERROR:"):
                raw_err = line[len("WAKE_ERROR:"):].strip()
                # 翻译为按真实原因给出建议的提示（上游文案是写死的通用建议）
                self.error = _translate_wake_error(raw_err, load_settings().get("gpu") or "")
                self.state = "error"
                self.log("失败: " + raw_err)
                if self.error != raw_err:
                    for ln in self.error.splitlines():
                        self.log(ln)
                continue
            self.log(line)
        rc = proc.wait()
        if self.state not in ("error",):
            self.state = "done" if rc == 0 else "error"
        self.log(f"进程退出 (code={rc})")
        # keepalive 是常驻进程，退出即意味着保活失效（不是「任务完成」）。
        # 记录真实原因，交由 API 层判断是否需要自动重启。
        if self.name == "keepalive":
            self.error = (
                f"保活进程已退出 (code={rc})。"
                + ("未产生任何输出，通常是上游 daemon 读到 stdin EOF 后主动退出。"
                   if not any("keepalive" in ln for ln in list(self.lines))
                   else "请检查日志中的 keepalive 状态行。")
            )
            self.state = "error"


WAKE = BridgeJob("wake")
KEEPALIVE = BridgeJob("keepalive")


# ---------------------------------------------------------------------------
# 保活看护
#
# keepalive_daemon 是常驻进程，不是一次性任务。上游版本会在 stdin EOF、代理
# token 刷新失败等情况下自行退出，此时如果不重启，网页上的开关看起来还是
# 打开的（settings.keepalive=true），实际早就不保活了 —— 用户看到的现象就是
# 「点了保活，用一会儿就断」。
# ---------------------------------------------------------------------------
_KEEPALIVE_GUARD_LOCK = threading.Lock()
_KEEPALIVE_GUARD: Dict[str, Any] = {"thread": None, "want": False, "restarts": 0}


def _keepalive_alive() -> bool:
    proc = KEEPALIVE.proc
    return proc is not None and proc.poll() is None


def _keepalive_guard_loop() -> None:
    backoff = 5.0
    while True:
        time.sleep(5)
        if not _KEEPALIVE_GUARD["want"]:
            return
        if _keepalive_alive():
            # 存活满 30 秒就把退避重置，避免偶发抖动导致越等越久
            if time.time() - KEEPALIVE.started_at > 30:
                backoff = 5.0
            continue
        # 进程没了：退避后再拉起来，避免疯狂重启刷日志
        time.sleep(min(backoff, 30))
        if not _KEEPALIVE_GUARD["want"]:
            return
        try:
            KEEPALIVE.start(["keepalive_daemon"])
            _KEEPALIVE_GUARD["restarts"] += 1
            KEEPALIVE.log(
                f"看护已自动重启保活（第 {_KEEPALIVE_GUARD['restarts']} 次），"
                f"上次退出原因见上方日志"
            )
            backoff = min(backoff * 2, 30)
        except RuntimeError:
            continue
        except Exception as exc:  # noqa: BLE001
            KEEPALIVE.log(f"看护重启保活失败: {exc}")


def start_keepalive_guard() -> None:
    with _KEEPALIVE_GUARD_LOCK:
        _KEEPALIVE_GUARD["want"] = True
        t = _KEEPALIVE_GUARD["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_keepalive_guard_loop, daemon=True)
            _KEEPALIVE_GUARD["thread"] = t
            t.start()


def stop_keepalive_guard() -> None:
    with _KEEPALIVE_GUARD_LOCK:
        _KEEPALIVE_GUARD["want"] = False


# ---------------------------------------------------------------------------
# 错误提示翻译
#
# 上游 bridge 的错误文案是写死的通用建议，例如「拒绝分配 L4，建议切换为
# A100 / L4 / T4」—— 建议里恰好包含刚失败的机型，会误导用户反复撞同一堵墙。
# 这里按 HTTP 状态码与失败的机型重写为可操作的建议，不修改上游文件。
# ---------------------------------------------------------------------------
_GPU_DISPLAY = {
    "T4": "T4 (16GB)",
    "L4": "L4 (24GB)",
    "A100": "A100 (80GB)",
    "G4": "G4 (RTX PRO 6000 · 96GB)",
    "H100": "H100 (80GB)",
}
# Colab 订阅等级与机型的对应关系
_TIER_GPUS = [
    ("免费版", "T4"),
    ("Pro（约 $10/月）", "T4"),
    ("Pro+（约 $50/月）", "L4 / A100"),
    ("AI Ultra", "G4 / H100"),
]


def _translate_wake_error(raw: str, gpu: str = "") -> str:
    if not raw:
        return raw
    text = raw.strip()
    g = (gpu or "").upper()

    # 从原始报错里提取实际失败的机型，避免依赖外部传参
    m = re.search(r"分配\s+([A-Za-z0-9]+(?:\s*\([^)]*\))?)\s*\(HTTP\s*(\d+)\)", text)
    failed_gpu = ""
    if m:
        failed_gpu = m.group(1).split("(")[0].strip().upper()
    if not failed_gpu:
        failed_gpu = g

    def _gpu_hint(exclude: str = "") -> str:
        rows = []
        for tier, gpus in _TIER_GPUS:
            names = [x.strip() for x in gpus.split("/")]
            marks = []
            for n in names:
                marks.append(f"~~{n}~~(刚被拒)" if n == exclude else n)
            rows.append(f"  · {tier}：{' / '.join(marks)}")
        return "\n".join(rows)

    if "HTTP 400" in text or "拒绝分配" in text:
        return (
            f"❌ Google 拒绝了 {failed_gpu or '该机型'} 的分配请求（HTTP 400 = 账号无权使用，重试无效）。\n"
            f"这通常意味着你的订阅等级不包含此机型，而非临时缺货。\n"
            f"各订阅等级可用机型：\n{_gpu_hint(failed_gpu)}\n"
            f"👉 建议：在左侧改用你订阅等级内的机型。若你用的是普通 Pro，请直接选 T4。"
        )

    if "HTTP 503" in text or "库存不足" in text or "算力池" in text:
        return (
            f"⏳ {failed_gpu or '该机型'} 当前没有空闲机器（HTTP 503 = 临时缺货，可以重试）。\n"
            f"这是 Google 侧的资源紧张，与你的配置无关，也不是程序故障。\n"
            f"👉 建议：\n"
            f"  · 等 5–15 分钟后重试，通常能抢到\n"
            f"  · T4 最拥挤（免费用户也在抢），深夜/清晨成功率明显更高\n"
            f"  · 避开美西工作时段（北京时间 0:00–8:00 左右最挤）"
        )

    if "HTTP 412" in text or "并发" in text:
        return (
            "🚫 账号并发实例数已达上限（HTTP 412）。\n"
            "👉 建议：先点左侧「释放全部实例」，或在 colab.research.google.com "
            "检查是否有其他标签页占着实例，释放后再唤醒。"
        )

    if "timed out" in text.lower() or "timeout" in text.lower():
        return (
            "🌐 网络超时，未能连上 Google Colab。\n"
            "👉 建议：打开右上角「设置」，确认代理可用（点「测试连接」）。"
        )

    return text
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


def _is_html_response(text: str) -> bool:
    head = (text or "").lstrip()[:300].lower()
    return head.startswith("<!doctype") or head.startswith("<html") or "<head>" in head


def _humanize_cloud_error(status: int, text: str) -> str:
    """把云端返回的原始 HTML / JSON 错误翻译成一句人话。

    上游在没有会话时会把整个 Gradio 的 404 页面当 detail 回传，前端直接展示就是
    一大段 HTML，用户看不懂也不知道该做什么。
    """
    low = (text or "").lower()
    if _is_html_response(text):
        if "no interface is running" in low or "no app is running" in low:
            return (
                "云端 Gradio 服务已停止：隧道地址还在，但实例里的 app.py 已经退出。"
                "免费实例闲置后被 Google 回收就是这个现象。请点「唤醒实例并部署」重新拉起，"
                "或者用「连接已有实例」接上仍活着的地址。"
            )
        return "云端返回的是网页而不是接口数据，说明这个地址没有指向正在运行的服务，请重新唤醒部署。"
    if status == 404:
        return "云端接口不存在（404）：可能实例里的 app.py 版本过旧，重新唤醒部署会同步最新服务端。"
    if status == 401:
        return "管理密码不匹配：重启控制台容器后会重新生成密码，请重新唤醒部署以同步。"
    return (text or f"云端返回 HTTP {status}").strip()[:400]


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
        raise HTTPException(
            status_code=resp.status_code,
            detail=_humanize_cloud_error(resp.status_code, resp.text),
        )
    try:
        return resp.json()
    except Exception:
        if _is_html_response(resp.text):
            raise HTTPException(
                status_code=502,
                detail=_humanize_cloud_error(resp.status_code, resp.text),
            )
        return {"ok": True, "raw": resp.text[:2000]}


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="EasyColab Web Console", docs_url=None, redoc_url=None)


@app.on_event("startup")
def _resume_keepalive_on_boot() -> None:
    """容器重启后按已保存的开关自动恢复保活。

    否则重启一次容器，网页开关仍是「开」，实际却没有保活进程。
    """
    try:
        cfg = load_settings()
        if not cfg.get("keepalive"):
            return
        if not _keepalive_alive():
            KEEPALIVE.start(["keepalive_daemon"])
        start_keepalive_guard()
    except Exception as exc:  # noqa: BLE001
        KEEPALIVE.log(f"启动恢复保活失败: {exc}")


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
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"启动保活失败: {exc}") from exc
        start_keepalive_guard()
        save_settings({"keepalive": True})
    else:
        stop_keepalive_guard()
        KEEPALIVE.stop()
        save_settings({"keepalive": False})
    return {"ok": True, "keepalive": KEEPALIVE.snapshot()}


@app.get("/api/keepalive")
def api_keepalive_status() -> Dict[str, Any]:
    snap = KEEPALIVE.snapshot()
    snap["wanted"] = bool(_KEEPALIVE_GUARD["want"])
    snap["restarts"] = _KEEPALIVE_GUARD["restarts"]
    return {"ok": True, "keepalive": snap}


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
@app.get("/api/drive/diagnose")
def api_drive_diagnose() -> Dict[str, Any]:
    """解释云盘为什么没挂上，并给出此刻可行的替代方案。

    结论：Colab 的 Google Drive 原生挂载必须由用户在 Colab 网页里点击授权，
    走的是浏览器 Google 登录态；本程序只通过 API 操作实例，没有登录态，
    所以自动挂载必然失败。rclone 备用通道要求用户自建 OAuth Client 且
    开通 Drive API 配额项目，当前 token 用的是 colab-cli 的公共 ADC Client
    (764086051850-*)，会被 Google 直接 403 SERVICE_DISABLED。
    这里不猜，直接把云端实际字段读出来告诉前端卡在哪。
    """
    out: Dict[str, Any] = {
        "ok": True,
        "reachable": False,
        "nativeMounted": False,
        "rcloneReady": False,
        "driveTotalGb": 0.0,
        "diskFreeGb": 0.0,
        "diskTotalGb": 0.0,
        "storagePath": "",
        "reason": "",
    }
    try:
        d = _cloud_call("GET", "/minimax_api/status", timeout=30)
    except HTTPException as exc:
        out["reason"] = str(exc.detail)
        return out
    if not isinstance(d, dict):
        out["reason"] = "云端返回格式异常"
        return out

    out["reachable"] = True
    total = float(d.get("drive_total_gb") or 0)
    out["nativeMounted"] = total > 0
    out["driveTotalGb"] = total
    out["diskFreeGb"] = float(d.get("disk_free_gb") or 0)
    out["diskTotalGb"] = float(d.get("disk_total_gb") or 0)
    out["storagePath"] = str(d.get("model_storage_path") or "")

    persisted = bool(d.get("drive_persisted"))
    comfy = d.get("comfyui") or {}
    out["comfyPersisted"] = bool(comfy.get("drive_persisted"))

    # _has_rclone_drive_remote() 的结果只体现在 drive_persisted 上：
    # 挂载成功或 rclone 可用都会让它为 true，而容量读数只有真挂载才非零。
    out["rcloneReady"] = bool(persisted and total == 0)
    if not persisted:
        out["reason"] = (
            "云盘既没有原生挂载，也没有可用的 rclone 通道。"
            "当前 token 使用 colab-cli 公共 ADC Client（无 quota_project），"
            "Google 会拒绝 Drive API 调用，因此备用通道也被自动跳过。"
        )
    elif total == 0:
        out["reason"] = "检测到 rclone 通道可用，但云盘容量读数为 0，挂载可能只完成了一半。"
    else:
        out["reason"] = "云盘已正常挂载。"
    return out


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
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py - local web UI for the Bilibili PGC downloader.

    python server.py [--port 8737] [--no-browser]

Serves http://127.0.0.1:<port>/ with:
    GET  /                     -> index.html
    GET  /api/status           -> account / ffmpeg / paths
    POST /api/resolve          {"url": "..."} -> season + episodes + qualities
    POST /api/login            -> start browser QR login (async)
    GET  /api/login/status     -> login progress
    POST /api/start            {"ep_ids": [...], "qn": 120, ...} -> job id
    GET  /api/progress?job=..  -> job state + per-episode progress + log
    POST /api/cancel           {"job": ".."}
    POST /api/shutdown         -> stop the server

Core download logic is reused from core.py (a copy of the CLI script).
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEB_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, WEB_DIR)
import core  # noqa: E402

core.TOOL_DIR = WEB_DIR  # bin/, cookies.txt, .browser-profile live next to server.py


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


# container-friendly configuration (all overridable by environment variables)
HOST = _env("BILI_HOST", "127.0.0.1")
APP_VERSION = "1.6"
MACHINE = os.environ.get("COMPUTERNAME") or socket.gethostname()   # 显示"这台机器"是谁
SERVE_PORT = 0          # filled in once the socket is bound, reported by /api/state
DEFAULT_PORT = int(_env("BILI_PORT", "8737") or 8737)
TOKEN = _env("BILI_TOKEN")                     # empty = no auth (LAN use)
PROXY = _env("BILI_PROXY") or None             # e.g. http://172.17.0.1:7890
OUTPUT_DIR = _env("BILI_OUTPUT") or os.path.join(WEB_DIR, "downloads")
COOKIE_FILE = _env("BILI_COOKIE") or os.path.join(WEB_DIR, "cookies.txt")
PROFILE_DIR = _env("BILI_PROFILE") or os.path.join(WEB_DIR, ".browser-profile")
AUTO_BROWSER = _env("BILI_NO_BROWSER", "").lower() not in ("1", "true", "yes", "on")



# ---------------------------------------------------------------------------
# bootstrap helpers
# ---------------------------------------------------------------------------


def bootstrap_assets():
    """Ensure runtime dirs exist; never copy anything from a sibling install."""
    for d in (os.path.dirname(COOKIE_FILE), PROFILE_DIR, OUTPUT_DIR):
        if d:
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass


STATE = {
    "client": None,
    "cookie": "",
    "account": None,
    "season": None,
    "eps": [],
    "job": None,
    # 最近一次解析的结果（给"另一台设备打开页面"复现用，进度与列表都以服务端为准）
    "last_resolve": None,
    "last_input": "",
    "login": {"state": "idle", "msg": "", "uname": "", "vip": False},
    "lock": threading.Lock(),
}


def make_client(proxy: str | None = None) -> core.HttpClient:
    cookie = core.load_cookie_file(COOKIE_FILE) if os.path.isfile(COOKIE_FILE) else ""
    cli = core.HttpClient(cookie, proxy or PROXY)
    cli.ensure_buvid()
    return cli


def refresh_account():
    cookie = core.load_cookie_file(COOKIE_FILE) if os.path.isfile(COOKIE_FILE) else ""
    info = core.verify_login(cookie) if cookie else None
    with STATE["lock"]:
        STATE["cookie"] = cookie
        STATE["account"] = info
    return info


# ---------------------------------------------------------------------------
# download job
# ---------------------------------------------------------------------------


class Cancelled(Exception):
    pass


class WebDownloader(core.Downloader):
    """core.Downloader with a progress callback + cancel + speed limit."""

    def __init__(self, client, threads: int, on_progress, cancel: threading.Event,
                 rate_limit: int = 0):
        super().__init__(client, threads, rate_limit)
        self.on_progress = on_progress
        self.cancel = cancel

    def download(self, name: str, urls: list, dest: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        last = None
        for url in urls:
            try:
                size = self._size(url)
                if size <= 0:
                    continue
                self._fetch(url, dest, size, name)
                return dest
            except Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"download failed: {last}")

    def _fetch(self, url: str, dest: str, size: int, name: str):
        n = self.threads if size >= 4 * 1024 * 1024 else 1
        chunk = size // n
        ranges = []
        for i in range(n):
            start = i * chunk
            end = size - 1 if i == n - 1 else (i + 1) * chunk - 1
            ranges.append((start, end))
        parts = [f"{dest}.p{i}" for i in range(n)]
        done = [0] * n
        lock = threading.Lock()
        t0 = time.time()

        def work(i, start, end):
            for attempt in range(3):
                try:
                    with self.client.raw(url, {"Range": f"bytes={start}-{end}"}) as r, \
                            open(parts[i], "wb") as f:
                        while True:
                            if self.cancel.is_set():
                                raise Cancelled()
                            buf = r.read(self.limiter.suggest_chunk(262144))
                            if not buf:
                                break
                            f.write(buf)
                            self.limiter.consume(len(buf))
                            with lock:
                                done[i] += len(buf)
                                total = sum(done)
                                sp = total / max(1e-6, time.time() - t0)
                                self.on_progress(total, size, sp)
                    if os.path.getsize(parts[i]) == end - start + 1:
                        return True
                except Cancelled:
                    raise
                except Exception:  # noqa: BLE001
                    time.sleep(0.8 + attempt)
            raise RuntimeError(f"chunk {i} failed")

        try:
            threads = []
            for i, (s, e) in enumerate(ranges):
                t = threading.Thread(target=work, args=(i, s, e), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()
            with open(dest, "wb") as out:
                for p in parts:
                    with open(p, "rb") as f:
                        shutil.copyfileobj(f, out, 1024 * 1024)
        finally:
            for p in parts:
                try:
                    os.remove(p)
                except OSError:
                    pass


class Job:
    def __init__(self, eps: list, opts: dict, season_title: str):
        self.id = uuid.uuid4().hex[:8]
        self.state = "running"
        self.opts = opts
        self.season_title = season_title
        self.cancel = threading.Event()
        self.t0 = time.time()
        self.log = []
        self.limiter = None  # core.RateLimiter, wired up by run_job for live tuning
        self.items = [{
            "ep_id": e["ep_id"],
            "index": e.get("index", ""),
            "title": e.get("long_title", ""),
            "file": core.ep_filename(e) + ".mp4",
            "status": "pending",
            "pct": 0.0,
            "speed": "",
            "size": "",
            "reason": "",
        } for e in eps]
        self._lock = threading.Lock()

    def add_log(self, msg: str):
        with self._lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {msg}")
            if len(self.log) > 300:
                del self.log[:100]

    def set_item(self, i: int, **kw):
        with self._lock:
            self.items[i].update(kw)

    def snapshot(self) -> dict:
        with self._lock:
            items = [dict(x) for x in self.items]
            log = list(self.log)
        total = len(items)
        finished = sum(1 for x in items if x["status"] in ("done", "skipped", "failed"))
        pct = sum(100.0 if x["status"] in ("done", "skipped") else float(x["pct"] or 0)
                  for x in items) / total if total else 100.0
        current = next((i + 1 for i, x in enumerate(items) if x["status"] == "running"), 0)
        rate_kb = (int(self.limiter.rate / 1024) if self.limiter
                   else int(self.opts.get("rate_limit") or 0))
        return {
            "id": self.id,
            "state": self.state,
            "elapsed": int(time.time() - self.t0),
            "progress": pct,
            "rate_limit": rate_kb,
            "current": current,
            "total": total,
            "finished": finished,
            "items": items,
            "log": log[-200:],
        }


def run_job(job: Job, targets: list, client: core.HttpClient):
    opts = job.opts
    codec = core.CODEC_ALIAS.get(opts["codec"]) if opts.get("codec") not in (None, "auto") else None
    rate_bps = max(0, int(opts.get("rate_limit") or 0)) * 1024
    dl = WebDownloader(client, int(opts.get("threads") or 4),
                       on_progress=lambda d, t, sp: None, cancel=job.cancel,
                       rate_limit=rate_bps)
    job.limiter = dl.limiter  # allows POST /api/limit to retune mid-download
    if rate_bps:
        job.add_log(f"限速 {core.fmt_size(rate_bps)}/s（整任务共享）")
    if opts.get("delogo"):
        job.add_log(f"去水印: {opts['delogo']}（视频重编码为 H.264，速度会慢一些）")
    outdir = os.path.join(OUTPUT_DIR, core.sanitize(job.season_title))
    os.makedirs(outdir, exist_ok=True)

    try:
        for i, ep in enumerate(targets):
            if job.cancel.is_set():
                job.state = "cancelled"
                job.add_log("已取消")
                return
            job.set_item(i, status="running", pct=0.0)
            job.add_log(f"开始 ep{ep['ep_id']} {ep.get('index','')} {ep.get('long_title','')}")
            try:
                play = core.fetch_play(client, ep, opts["qn"], True)
                vids = core.video_candidates(play)
                auds = core.audio_candidates(play)
                if not vids:
                    job.set_item(i, status="failed", reason="没有可用视频流（会员/地区/下架）")
                    job.add_log("  x 没有可用视频流")
                    continue

                got = int(play.get("quality") or 0)
                if got and got < opts["qn"]:
                    job.add_log(f"  ! 请求 {core.QN_NAME.get(opts['qn'], opts['qn'])}，"
                                f"下发 {core.QN_NAME.get(got, got)}")

                v = core.choose_video(vids, opts["qn"], codec)
                a = core.choose_audio(auds, opts.get("audio", "auto"))
                job.add_log(f"  video {core.QN_NAME.get(v['qn'], v['qn'])}/"
                            f"{core.CODEC_NAME.get(v['codecid'], v['codecid'])} "
                            f"{v['width']}x{v['height']} {v['bandwidth']//1000}kbps")

                title = core.ep_filename(ep)
                outfile = os.path.join(outdir, title + ".mp4")
                if os.path.exists(outfile) and not opts.get("overwrite"):
                    job.set_item(i, status="skipped", pct=100.0, reason="已存在，跳过")
                    job.add_log("  已存在，跳过")
                    continue

                tmpdir = os.path.join(outdir, ".parts")
                os.makedirs(tmpdir, exist_ok=True)
                vpath = os.path.join(tmpdir, f"v_{ep['ep_id']}.m4s")
                apath = os.path.join(tmpdir, f"a_{ep['ep_id']}.m4s") if a else None

                st = {"t": time.time(), "b": 0, "ema": 0.0}

                def on_prog(done_bytes, total_bytes, _speed, _st=st, _i=i):
                    now = time.time()
                    dt = now - _st["t"]
                    if dt >= 0.5:  # 0.5s 采样窗口 + 指数平滑，调完限速能立刻看出来
                        inst = (done_bytes - _st["b"]) / dt
                        _st["ema"] = inst if not _st["ema"] else _st["ema"] * 0.55 + inst * 0.45
                        _st["t"], _st["b"] = now, done_bytes
                    job.set_item(_i, pct=round(done_bytes * 100.0 / max(1, total_bytes), 1),
                                 speed=(core.fmt_size(_st["ema"]) + "/s") if _st["ema"] else "",
                                 size=f"{core.fmt_size(done_bytes)}/{core.fmt_size(total_bytes)}")

                dl.on_progress = on_prog
                dl.download("video", v["urls"], vpath)
                if a:
                    dl.download("audio", a["urls"], apath)
                if job.cancel.is_set():
                    raise Cancelled()

                subs = []
                if opts.get("subtitles", True):
                    try:
                        for lang, text in core.fetch_subtitles(client, ep):
                            sp = os.path.join(tmpdir, f"s_{ep['ep_id']}_{core.sanitize(lang, 16)}.srt")
                            with open(sp, "w", encoding="utf-8") as f:
                                f.write(text)
                            subs.append(sp)
                        if subs:
                            job.add_log(f"  字幕 {len(subs)} 条")
                    except Exception:  # noqa: BLE001
                        pass

                ffmpeg = core.find_ffmpeg()

                vf = None
                enc = None
                wants_filter = (opts.get("delogo") or opts.get("delogo_raw")
                                or opts.get("delogo_mode") == "mask")
                if wants_filter:
                    try:
                        vf = core.build_filter(
                            opts.get("delogo"), v["width"], v["height"],
                            mode=opts.get("delogo_mode") or "delogo",
                            mask_path=opts.get("delogo_mask"),
                            time_range=opts.get("delogo_range"),
                            raw_filter=opts.get("delogo_raw"),
                            src_codecid=v["codecid"],
                        )
                    except ValueError as e:
                        job.set_item(i, status="failed", reason=f"去水印参数无效: {e}")
                        job.add_log(f"  x 去水印参数无效: {e}")
                        continue
                    if vf:
                        enc = core.encode_args(v["height"], v["codecid"])
                        job.add_log(f"  去水印[{opts.get('delogo_mode') or 'delogo'}]: {vf}")
                        job.add_log(f"  重编码: {' '.join(enc)}")

                if ffmpeg and core.merge(ffmpeg, vpath, apath, outfile, subs, vf=vf, enc=enc):
                    size = os.path.getsize(outfile)
                    job.set_item(i, status="done", pct=100.0, speed="",
                                 size=core.fmt_size(size), file=os.path.basename(outfile))
                    job.add_log(f"  ok {os.path.basename(outfile)} ({core.fmt_size(size)})")
                    for p in (vpath, apath, *subs):
                        if p and os.path.exists(p):
                            try:
                                os.remove(p)
                            except OSError:
                                pass
                else:
                    job.set_item(i, status="failed", reason="ffmpeg 合并失败或缺失")
                    job.add_log("  x ffmpeg 合并失败或缺失")
            except Cancelled:
                # 取消时清掉半成品分片，别把 .parts 越堆越大
                for p in (locals().get("vpath"), locals().get("apath")):
                    if p:
                        for f in glob.glob(p + "*"):
                            try:
                                os.remove(f)
                            except OSError:
                                pass
                job.set_item(i, status="failed", reason="已取消")
                job.state = "cancelled"
                job.add_log("已取消")
                return
            except SystemExit as e:
                job.set_item(i, status="failed", reason=str(e))
                job.add_log(f"  x {e}")
            except Exception as e:  # noqa: BLE001
                job.set_item(i, status="failed", reason=f"{type(e).__name__}: {e}")
                job.add_log(f"  x {e}")
                traceback.print_exc()

    finally:
        if job.state == "running":
            job.state = "done"


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "BiliPGCWeb/1.0"
    protocol_version = "HTTP/1.1"

    def log_request(self, code="-", size="-"):
        # 畸形请求（手机的 https 字节、扫描器探测）会把乱码当 requestline 记日志，直接不记
        try:
            if int(code) == 400:
                return
        except Exception:  # noqa: BLE001
            pass
        return super().log_request(code, size)

    def log_message(self, fmt, *a):  # quieter console
        try:
            line = fmt % a
        except Exception:  # noqa: BLE001  lib 里 log_error("code %d, message %s", 400, ...)
            line = " ".join(str(x) for x in a) or str(fmt)
        if "/api/progress" in line or "/healthz" in line:
            return
        sys.stderr.write("%s - %s\n" % (self.address_string(), line))

    def send_error(self, code, message=None, explain=None):
        """把协议层的 400 变成一句人话（最常见来源：手机用 https:// 打开 HTTP 端口）。"""
        if code == 400:
            body = (
                "Bad request: this port speaks HTTP, not HTTPS.\r\n"
                "Open http://<this-host>:<port>/ in the browser (http://, not https://).\r\n"
            ).encode("utf-8")
            try:
                self._send(400, body, "text/plain; charset=utf-8")
            except Exception:  # noqa: BLE001
                pass
            return
        return super().send_error(code, message, explain)

    # ---- low level ----
    def _send(self, code: int, body: bytes, ctype: str, extra_headers: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError):
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _authed(self) -> bool:
        if not TOKEN:
            return True
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if (qs.get("token") or [""])[0] == TOKEN:
            return True
        if self.headers.get("X-Token") == TOKEN:
            return True
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "bili_auth" and v == TOKEN:
                return True
        return False

    def _deny(self):
        self._send(401,
                   "<meta charset=utf-8><body style='font:15px/1.8 sans-serif;padding:40px'>"
                   "<h3>需要访问令牌</h3><p>请在地址后加 <code>?token=你的BILI_TOKEN</code>，"
                   "例如 <code>http://NAS_IP:8737/?token=xxxx</code></p>".encode("utf-8"),
                   "text/html; charset=utf-8")

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not n:
                return {}
            return json.loads(self.rfile.read(n).decode("utf-8", "replace") or "{}")
        except Exception:  # noqa: BLE001
            return {}

    # ---- routes ----
    def do_GET(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path = u.path
        qs = urllib.parse.parse_qs(u.query)

        if path == "/healthz":
            return self._send(200, b"ok", "text/plain")

        if not self._authed():
            if path.startswith("/api"):
                return self._json({"error": "unauthorized", "hint": "add ?token=..."}, 401)
            return self._deny()

        if path in ("/", "/index.html"):
            fp = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(fp):
                return self._send(500, b"index.html missing", "text/plain; charset=utf-8")
            with open(fp, "rb") as f:
                html = f.read()
            hdrs = {}
            if TOKEN and (qs.get("token") or [""])[0] == TOKEN:
                hdrs["Set-Cookie"] = f"bili_auth={TOKEN}; Path=/; SameSite=Lax; HttpOnly"
            return self._send(200, html, "text/html; charset=utf-8", hdrs)

        if path == "/api/status":
            acc = STATE["account"]
            return self._json({
                "account": ({"uname": acc["uname"], "vip": acc["vip"],
                             "label": acc["vip_label"] or ("大会员" if acc["vip"] else "普通账号"),
                             "level": acc["level"]} if acc else None),
                "ffmpeg": bool(core.find_ffmpeg()),
                "browser": bool(core.find_browser()),
                "cookie_file": os.path.isfile(COOKIE_FILE),
                "output": OUTPUT_DIR,
                "token": bool(TOKEN),
                "version": APP_VERSION,
                "machine": MACHINE,
            })

        if path == "/api/login/status":
            return self._json(STATE["login"])

        if path == "/api/cover":
            u = (qs.get("u") or [""])[0]
            if not u.startswith(("http://", "https://")):
                return self._send(400, b"bad url", "text/plain")
            try:
                cli = STATE.get("client") or make_client()
                with cli.raw(u) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type") or "image/jpeg"
                return self._send(200, data, ctype)
            except Exception:  # noqa: BLE001
                return self._send(404, b"no image", "text/plain")

        if path == "/api/progress":
            job = STATE.get("job")
            if not job:
                return self._json({"state": "idle", "items": [], "log": []})
            return self._json(job.snapshot())

        if path == "/api/state":
            # 跨设备同步：任何浏览器都能拿到"上次解析的季 + 当前任务摘要"
            job = STATE.get("job")
            return self._json({
                "ok": True,
                "version": APP_VERSION,
                "machine": MACHINE,
                "host": HOST,
                "port": SERVE_PORT,
                "lan": _lan_ips(),
                "resolved": STATE.get("last_resolve"),
                "input": STATE.get("last_input") or "",
                "job": ({
                    "id": job.id,
                    "state": job.state,
                    "total": len(job.items),
                    "finished": sum(1 for x in job.items
                                    if x["status"] in ("done", "skipped", "failed")),
                    "elapsed": int(time.time() - job.t0),
                } if job else None),
            })

        return self._json({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path = u.path
        body = self._body()

        if not self._authed():
            return self._send(401, b'{"error":"unauthorized"}', "application/json")

        if path == "/api/resolve":
            return self.api_resolve(body)
        if path == "/api/cookie":
            return self.api_cookie(body)
        if path == "/api/login":
            return self.api_login(body)
        if path == "/api/start":
            return self.api_start(body)
        if path == "/api/limit":
            return self.api_limit(body)
        if path == "/api/cancel":
            job = STATE.get("job")
            if job:
                job.cancel.set()
            return self._json({"ok": True})
        if path == "/api/shutdown":
            self._json({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return None
        return self._json({"error": "not found"}, 404)

    # ---- api impl ----
    def api_resolve(self, body: dict):
        text = (body.get("url") or "").strip()
        if not text:
            return self._json({"ok": False, "error": "请输入链接"}, 400)
        try:
            client = make_client(body.get("proxy"))
            ref = core.resolve_input(client, text)
            if ref["kind"] in ("bv", "av"):
                url = (f"{core.API_VIEW}?bvid={ref['id']}" if ref["kind"] == "bv"
                       else f"{core.API_VIEW}?aid={ref['id']}")
                view = client.get_json(url)
                if view.get("code") != 0:
                    raise RuntimeError(view.get("message") or "取稿件失败")
                v = view["data"]
                eps = [{"ep_id": 0, "cid": v["cid"], "aid": v["aid"], "bvid": v["bvid"],
                        "index": "1", "long_title": v["title"], "badge": "",
                        "duration": (v.get("duration") or 0) * 1000, "section_type": 0}]
                title, cover, is_pgc, total = v["title"], v.get("pic") or "", False, 1
            else:
                season = core.fetch_season(client, ref)
                eps = core.ep_list_of(season)
                title = season.get("season_title") or season.get("title") or "bilibili"
                cover = season.get("cover") or ""
                is_pgc = True
                total = len([e for e in eps if e.get("section_type", 0) == 0])

            with STATE["lock"]:
                STATE["client"] = client
                STATE["season"] = {"title": title, "cover": cover, "is_pgc": is_pgc,
                                   "total": total, "area_limit": False}
                STATE["eps"] = eps

            # quality menu from the first episode
            qualities = []
            current_qn = None
            if eps:
                play = core.fetch_play(client, eps[0], 127, is_pgc)
                current_qn = play.get("quality")
                vids = core.video_candidates(play)
                accept = play.get("accept_quality") or []
                by_qn = {}
                for v in vids:
                    by_qn.setdefault(v["qn"], []).append(v)
                order = sorted(by_qn, key=core.qual_rank)
                for qn in order:
                    codecs = []
                    for v in sorted(by_qn[qn], key=lambda c: -c["bandwidth"]):
                        codecs.append({"codecid": v["codecid"],
                                       "name": core.CODEC_NAME.get(v["codecid"], str(v["codecid"])),
                                       "w": v["width"], "h": v["height"],
                                       "bw": v["bandwidth"] // 1000})
                    qualities.append({"qn": qn, "name": core.QN_NAME.get(qn, str(qn)),
                                      "codecs": codecs, "locked": False})
                for qn in accept:
                    if qn not in by_qn:
                        qualities.append({"qn": qn, "name": core.QN_NAME.get(qn, str(qn)),
                                          "codecs": [], "locked": True})
                qualities.sort(key=lambda x: core.qual_rank(x["qn"]))

            acc = refresh_account()
            payload = {
                "ok": True,
                "season": {"title": title, "cover": cover, "total": total,
                           "is_pgc": is_pgc, "items": len(eps)},
                "episodes": [{
                    "idx": i,
                    "ep_id": e["ep_id"],
                    "index": e.get("index", ""),
                    "title": e.get("long_title", ""),
                    "badge": e.get("badge", ""),
                    "section_type": e.get("section_type", 0),
                    "duration": e.get("duration", 0),
                } for i, e in enumerate(eps, 1)],
                "qualities": qualities,
                "ceiling": current_qn,
                "account": ({"uname": acc["uname"], "vip": acc["vip"],
                             "label": acc["vip_label"] or ("大会员" if acc["vip"] else "普通账号")}
                            if acc else None),
            }
            with STATE["lock"]:  # 另一台设备打开页面时用它复现列表
                STATE["last_resolve"] = payload
                STATE["last_input"] = text
            return self._json(payload)
        except SystemExit as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def api_cookie(self, body: dict):
        """Manually set the cookie (essential inside containers: no browser)."""
        raw = (body.get("cookie") or "").strip()
        if not raw:
            return self._json({"ok": False, "error": "请输入 Cookie 或 SESSDATA"}, 400)
        cookie = raw if "=" in raw else f"SESSDATA={raw}"
        info = core.verify_login(cookie)
        if not info:
            return self._json({"ok": False, "error": "该 Cookie / SESSDATA 无效或已过期"}, 400)
        try:
            core.save_cookie(cookie, COOKIE_FILE)
        except OSError as e:
            return self._json({"ok": False,
                               "error": f"无法写入 {COOKIE_FILE}（{e}）——检查 /config 卷是否可写"},
                              500)
        with STATE["lock"]:
            STATE["cookie"] = cookie
            STATE["account"] = info
            STATE["client"] = None  # 下次解析重新建 client
        label = info["vip_label"] or ("大会员" if info["vip"] else "普通账号")
        return self._json({"ok": True, "account": {"uname": info["uname"],
                                                  "vip": info["vip"], "label": label,
                                                  "level": info["level"]}})

    def api_login(self, body: dict):
        st = STATE["login"]
        if st.get("state") == "waiting":
            return self._json({"ok": True, "state": "waiting"})

        def worker():
            STATE["login"] = {"state": "waiting", "msg": "已打开浏览器，请扫码登录",
                              "uname": "", "vip": False}
            try:
                cookie = core.browser_login(timeout=int(body.get("timeout") or 240),
                                            profile=PROFILE_DIR)
                if cookie:
                    acc = core.verify_login(cookie)
                    STATE["login"] = {"state": "ok", "msg": "登录成功",
                                      "uname": (acc or {}).get("uname", ""),
                                      "vip": bool((acc or {}).get("vip"))}
                    refresh_account()
                else:
                    STATE["login"] = {"state": "failed", "msg": "未抓到登录态（超时或已取消）",
                                      "uname": "", "vip": False}
            except Exception as e:  # noqa: BLE001
                STATE["login"] = {"state": "failed", "msg": f"{type(e).__name__}: {e}",
                                  "uname": "", "vip": False}

        threading.Thread(target=worker, daemon=True).start()
        return self._json({"ok": True, "state": "waiting"})

    def api_limit(self, body: dict):
        """Retune the speed limit of the running job (KB/s, 0 = unlimited)."""
        try:
            kb = max(0, int(body.get("rate_limit") or 0))
        except Exception:  # noqa: BLE001
            return self._json({"ok": False, "error": "rate_limit 必须是整数 KB/s"}, 400)
        job = STATE.get("job")
        if not job:
            return self._json({"ok": False, "error": "还没有任务"}, 400)
        job.opts["rate_limit"] = kb
        if job.state != "running" or job.limiter is None:
            return self._json({"ok": False, "error": "当前没有运行中的下载",
                               "rate_limit": kb}, 409)
        job.limiter.set_rate(kb * 1024)
        job.add_log(f"限速调整为 {kb} KB/s" if kb else "已取消限速")
        return self._json({"ok": True, "rate_limit": kb})

    def api_start(self, body: dict):
        cur = STATE.get("job")
        if cur is not None and cur.state == "running":
            return self._json({
                "ok": False,
                "error": "已有任务在后台下载，先点「取消」或等它跑完（关掉网页不会影响它）",
                "job": cur.id,
            }, 409)
        eps = STATE.get("eps") or []
        if not eps:
            return self._json({"ok": False, "error": "还没有解析链接"}, 400)
        ids = body.get("ep_ids") or []
        if not ids:
            return self._json({"ok": False, "error": "请先选集"}, 400)
        targets = [e for e in eps if e["ep_id"] in set(ids)]
        if not targets:
            return self._json({"ok": False, "error": "选中的剧集不在当前列表里"}, 400)

        try:
            qn = int(body.get("qn") or 127)
        except Exception:  # noqa: BLE001
            qn = 127
        try:
            rate_limit = max(0, int(body.get("rate_limit") or 0))  # KB/s, 0 = unlimited
        except Exception:  # noqa: BLE001
            rate_limit = 0
        delogo = body.get("delogo")
        if delogo is not None:
            delogo = str(delogo).strip()
            if delogo.lower() in ("", "0", "off", "none", "false", "no"):
                delogo = None
        mode = (body.get("delogo_mode") or "delogo").strip().lower()
        if mode not in core.DELOGO_MODES:
            return self._json({"ok": False, "error": f"未知去水印模式：{mode}"}, 400)
        time_range = body.get("delogo_range") or None
        if time_range:
            time_range = str(time_range).strip() or None
        raw_filter = (body.get("delogo_raw") or "").strip() or None
        mask_path = (body.get("delogo_mask") or "").strip() or None
        if mode == "mask":
            if not mask_path:
                return self._json({"ok": False, "error": "mask 模式需要遮罩图文件名"}, 400)
            cand = mask_path if os.path.isabs(mask_path) else os.path.join(
                os.path.dirname(COOKIE_FILE) or ".", mask_path)
            if not os.path.isfile(cand):
                return self._json(
                    {"ok": False,
                     "error": f"遮罩图不存在：{cand}（放到 /config 目录下，或在页面填绝对路径）"},
                    400)
            mask_path = cand
        if delogo or raw_filter:
            try:  # fail fast, same code path the job will use
                core.build_filter(delogo, 1920, 1080, mode=mode, mask_path=mask_path,
                                  time_range=time_range, raw_filter=raw_filter)
            except ValueError as e:
                return self._json({"ok": False, "error": f"去水印参数无效：{e}"}, 400)
        opts = {
            "qn": qn,
            "codec": body.get("codec") or "auto",
            "audio": body.get("audio") or "auto",
            "threads": int(body.get("threads") or 4),
            "subtitles": bool(body.get("subtitles", True)),
            "overwrite": bool(body.get("overwrite", False)),
            "rate_limit": rate_limit,
            "delogo": delogo,
            "delogo_mode": mode,
            "delogo_range": time_range,
            "delogo_raw": raw_filter,
            "delogo_mask": mask_path,
        }
        client = STATE.get("client") or make_client()
        job = Job(targets, opts, (STATE.get("season") or {}).get("title") or "bilibili")
        with STATE["lock"]:
            STATE["job"] = job
        threading.Thread(target=run_job, args=(job, targets, client), daemon=True).start()
        return self._json({"ok": True, "job": job.id, "count": len(targets)})


def _lan_ips() -> list:
    """Best-effort list of LAN IPv4 addresses (for the 'open this on your phone' hint).

    Deliberately does NOT open a socket to an external address: some AV products
    flag "python script that connects to 8.8.8.8" as a beacon and quarantine the
    whole file. Hostname resolution is enough in practice.
    """
    ips = set()
    try:
        for _, _, addrs in socket.gethostbyname_ex(socket.gethostname()):
            for ip in addrs:
                if not ip.startswith("127.") and ":" not in ip:
                    ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except Exception:  # noqa: BLE001
        pass
    return sorted(ips)


def main() -> int:
    global SERVE_PORT
    port = DEFAULT_PORT
    host = HOST
    argv = sys.argv[1:]
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    if "--host" in argv:
        host = argv[argv.index("--host") + 1]
    open_browser = AUTO_BROWSER and "--no-browser" not in argv

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    bootstrap_assets()
    for d in (OUTPUT_DIR, os.path.dirname(COOKIE_FILE), PROFILE_DIR):
        if d:
            os.makedirs(d, exist_ok=True)
    refresh_account()

    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer((host, p), Handler)
            port = p
            SERVE_PORT = p
            break
        except OSError:
            continue
    if httpd is None:
        print("no free port near", DEFAULT_PORT)
        return 1

    shown_host = "127.0.0.1" if host in ("0.0.0.0", "::", "") else host
    url = f"http://{shown_host}:{port}/"
    acc = STATE["account"]
    print("=" * 58)
    print(f"  Bilibili PGC web UI  v{APP_VERSION}")
    print(f"  app    : {WEB_DIR}")
    print(f"  listen : http://{host}:{port}/   (open {url})")
    print(f"  ffmpeg : {core.find_ffmpeg() or 'MISSING'}")
    print(f"  账号   : " + (f"{acc['uname']} ({'大会员' if acc['vip'] else '普通账号'})"
                            if acc else "游客（最高 480P，可在页面里登录）"))
    print(f"  输出   : {OUTPUT_DIR}")
    print(f"  Cookie : {COOKIE_FILE}")
    if TOKEN:
        print("  令牌   : 已开启（BILI_TOKEN），访问需带 ?token=***")
    if PROXY:
        print(f"  代理   : {PROXY}")
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if loopback:
        print("  [!] 只监听本机：手机/平板打不开这个地址，也就没法同步进度。")
        print("      要跨设备看，用  python server.py --host 0.0.0.0  （建议同时设置 BILI_TOKEN）")
    else:
        for ip in _lan_ips():
            print(f"  局域网 : http://{ip}:{port}/{('?token=***' if TOKEN else '')}")
        print("      手机连同一个 Wi-Fi，直接打开上面的地址即可看到同一份进度")
        if not TOKEN:
            print("  [!] 监听在 0.0.0.0 且未设 BILI_TOKEN：同网段任何人都能打开并操作，建议设置")
    print("  Ctrl+C 停止服务")
    print("=" * 58)

    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

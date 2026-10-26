#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bili_pgc_dl.py - Bilibili PGC (bangumi / movie / documentary / course) downloader.
Pure standard library, no pip install needed. ffmpeg is auto-located or auto-downloaded.

Examples:
    python bili_pgc_dl.py                        # wizard: login -> link -> scope -> quality
    python bili_pgc_dl.py https://www.bilibili.com/bangumi/play/ep693247
    python bili_pgc_dl.py ss43164 --all -q 1080p
    python bili_pgc_dl.py ss43164 --all --with-pv -q 720p
    python bili_pgc_dl.py ss43164 --eps 1-3,7 -q 4k --codec hevc
    python bili_pgc_dl.py ss43164 --all --dry-run
    python bili_pgc_dl.py ep693247 -l
    python bili_pgc_dl.py --login
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
REFERER = "https://www.bilibili.com/"

API_NAV = "https://api.bilibili.com/x/web-interface/nav"
API_FINGER = "https://api.bilibili.com/x/frontend/finger/spi"
API_SEASON = "https://api.bilibili.com/pgc/view/web/season"
API_PLAYURL_PGC = "https://api.bilibili.com/pgc/player/web/playurl"
API_PLAYURL_UG = "https://api.bilibili.com/x/player/wbi/playurl"
API_PLAYER_V2 = "https://api.bilibili.com/x/player/wbi/v2"
API_PLAYER_V2_PLAIN = "https://api.bilibili.com/x/player/v2"
API_VIEW = "https://api.bilibili.com/x/web-interface/view"

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

QN_NAME = {
    127: "8K", 126: "Dolby", 125: "HDR", 120: "4K", 116: "1080P60",
    112: "1080P+", 80: "1080P", 74: "720P60", 64: "720P", 32: "480P", 16: "360P",
}
QN_ALIAS = {
    "8k": 127, "dolby": 126, "hdr": 125, "4k": 120, "2160p": 120,
    "1080p60": 116, "1080p+": 112, "1080plus": 112, "1080p": 80,
    "720p60": 74, "720p": 64, "480p": 32, "360p": 16,
}
QN_ORDER = [127, 126, 125, 120, 116, 112, 80, 74, 64, 32, 16]

AUDIO_NAME = {30251: "Hi-Res", 30250: "Dolby", 30280: "192K", 30232: "132K", 30216: "64K"}
CODEC_NAME = {7: "AVC", 12: "HEVC", 13: "AV1"}
CODEC_ALIAS = {"avc": 7, "h264": 7, "hevc": 12, "h265": 12, "av1": 13}

CODE_HINT = {
    -101: "not logged in - add a SESSDATA cookie (or run --login)",
    -111: "csrf check failed",
    -352: "risk control triggered - wait, or change IP / add --proxy",
    -400: "bad request",
    -403: "permission denied (VIP only, or the item is gone)",
    -404: "not found",
    -10403: "region locked - need a mainland China IP, use --proxy",
    87007: "VIP only (need a VIP SESSDATA)",
    87008: "VIP only (need a VIP SESSDATA)",
    87009: "charge-only content (need a charged account)",
}

FFMPEG_WIN_URLS = [
    "https://gh-proxy.com/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
    "https://ghfast.top/https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
    "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl.zip",
    "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip",
]

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COOKIE_FILE = os.path.join(TOOL_DIR, "cookies.txt")
DEFAULT_PROFILE_DIR = os.path.join(TOOL_DIR, ".browser-profile")
LOGIN_URL = "https://passport.bilibili.com/login"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.2f}GB"


def sanitize(name: str, maxlen: int = 120) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    return (name[:maxlen] or "untitled").rstrip(" .")


def pick_quality(text: str) -> int:
    t = str(text).strip().lower()
    if t in ("best", "max"):
        return 127
    if t.isdigit():
        return int(t)
    if t in QN_ALIAS:
        return QN_ALIAS[t]
    raise SystemExit(f"unknown quality: {text} "
                     f"(best/8k/4k/1080p+/1080p/720p/480p/360p or qn number)")


def qual_rank(qn: int) -> int:
    try:
        return QN_ORDER.index(qn)
    except ValueError:
        return len(QN_ORDER)


def parse_ep_range(spec: str, total: int) -> list:
    out = []
    for part in re.split(r"[,，\s]+", spec.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)?\s*-\s*(\d+)?", part)
        if m and (m.group(1) or m.group(2)):
            a = int(m.group(1) or 1)
            b = int(m.group(2) or total)
            out.extend(range(a, b + 1))
        elif part.isdigit():
            out.append(int(part))
    return sorted({x for x in out if 1 <= x <= total})


def ask(prompt: str, default: str = "") -> str:
    try:
        s = input(prompt).strip()
    except EOFError:
        return default
    return s if s else default


# ---------------------------------------------------------------------------
# http client
# ---------------------------------------------------------------------------


class HttpClient:
    def __init__(self, cookie: str = "", proxy: str | None = None, timeout: int = 30):
        self.cookie = (cookie or "").strip()
        self.timeout = timeout
        handlers = [urllib.request.HTTPCookieProcessor()]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        self.opener = urllib.request.build_opener(*handlers)
        self.hdr = {
            "User-Agent": UA,
            "Referer": REFERER,
            "Origin": "https://www.bilibili.com",
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
        }
        if self.cookie:
            self.hdr["Cookie"] = self.cookie
        self._wbi_key = None
        self._wbi_ts = 0.0
        self._lock = threading.Lock()

    def raw(self, url: str, headers: dict | None = None, retries: int = 3):
        h = dict(self.hdr)
        if headers:
            h.update(headers)
        last = None
        for i in range(retries):
            try:
                req = urllib.request.Request(url, headers=h)
                return self.opener.open(req, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                last = e
                if e.code in (403, 404, 412):
                    raise
                time.sleep(1.2 * (i + 1))
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(1.2 * (i + 1))
        raise last  # type: ignore[misc]

    def get_text(self, url: str, headers: dict | None = None) -> str:
        with self.raw(url, headers) as r:
            return r.read().decode("utf-8", "replace")

    def get_json(self, url: str, headers: dict | None = None) -> dict:
        return json.loads(self.get_text(url, headers))

    def wbi_key(self, refresh: bool = False) -> str:
        with self._lock:
            if self._wbi_key and not refresh and time.time() - self._wbi_ts < 1800:
                return self._wbi_key
            d = self.get_json(API_NAV).get("data") or {}
            img = d.get("wbi_img") or {}
            iu = str(img.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
            su = str(img.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
            if not iu or not su:
                raise SystemExit("failed to fetch WBI keys (nav API), retry later")
            raw = iu + su
            self._wbi_key = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB if i < len(raw))[:32]
            self._wbi_ts = time.time()
            return self._wbi_key

    def sign(self, params: dict) -> dict:
        p = {k: v for k, v in params.items() if v not in (None, "")}
        p["wts"] = int(time.time())
        q = urllib.parse.urlencode(sorted(p.items()))
        p["w_rid"] = hashlib.md5((q + self.wbi_key()).encode()).hexdigest()
        return p

    def api(self, url: str, params: dict, wbi: bool = False, tries: int = 2) -> dict:
        j = {}
        for attempt in range(tries):
            p = self.sign(params) if wbi else {k: v for k, v in params.items() if v is not None}
            full = url + "?" + urllib.parse.urlencode(p)
            try:
                j = self.get_json(full)
            except urllib.error.HTTPError as e:
                j = {"code": -e.code, "message": f"HTTP {e.code}"}
            if j.get("code") == -403 and wbi and attempt + 1 < tries:
                self.wbi_key(refresh=True)
                continue
            return j
        return j

    def ensure_buvid(self):
        if "buvid3=" in self.cookie:
            return
        try:
            d = self.get_json(API_FINGER).get("data") or {}
            b3 = d.get("b_3")
            if b3:
                extra = f"buvid3={b3}; buvid4={d.get('b_4', '')}"
                self.cookie = (self.cookie + "; " + extra).strip("; ")
                self.hdr["Cookie"] = self.cookie
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# url parsing / metadata
# ---------------------------------------------------------------------------

RE_BV = re.compile(r"(BV[0-9A-Za-z]{10})")
RE_AV = re.compile(r"av(\d+)", re.I)
RE_EP = re.compile(r"ep(\d+)", re.I)
RE_SS = re.compile(r"ss(\d+)", re.I)
RE_MD = re.compile(r"md(\d+)", re.I)


def resolve_input(client: HttpClient, text: str) -> dict:
    s = text.strip()
    if re.fullmatch(r"\d+", s):
        raise SystemExit("bare number is ambiguous, use ep123 / ss123 / BVxxxx")
    if "b23.tv" in s or "bili2233" in s:
        try:
            with client.raw(s) as r:
                s = r.geturl()
        except Exception:  # noqa: BLE001
            pass
    m = RE_EP.search(s)
    if m:
        return {"kind": "ep", "id": int(m.group(1))}
    m = RE_SS.search(s)
    if m:
        return {"kind": "ss", "id": int(m.group(1))}
    m = RE_MD.search(s)
    if m:
        return {"kind": "md", "id": int(m.group(1))}
    m = RE_BV.search(s)
    if m:
        return {"kind": "bv", "id": m.group(1)}
    m = RE_AV.search(s)
    if m:
        return {"kind": "av", "id": int(m.group(1))}
    raise SystemExit(f"unrecognised input: {text}")


def md_to_season(client: HttpClient, md_id: int) -> int:
    try:
        html = client.get_text(f"https://www.bilibili.com/bangumi/media/md{md_id}/")
        m = re.search(r'"season_id":(\d+)', html) or re.search(r'"seasonId":(\d+)', html)
        if m:
            return int(m.group(1))
    except Exception:  # noqa: BLE001
        pass
    raise SystemExit(f"cannot map media md{md_id} to a season")


def fetch_season(client: HttpClient, ref: dict) -> dict:
    if ref["kind"] == "ep":
        j = client.api(API_SEASON, {"ep_id": ref["id"]})
    elif ref["kind"] == "ss":
        j = client.api(API_SEASON, {"season_id": ref["id"]})
    elif ref["kind"] == "md":
        j = client.api(API_SEASON, {"season_id": md_to_season(client, ref["id"])})
    else:
        raise SystemExit("not a PGC link (bangumi/movie expected)")
    if j.get("code") != 0:
        code = j.get("code")
        raise SystemExit(f"season lookup failed: code={code} {j.get('message')}\n"
                         f"hint: {CODE_HINT.get(code, '')}")
    return j["result"]


def ep_list_of(season: dict) -> list:
    raw = list(season.get("episodes") or [])
    for sec in season.get("section") or []:
        raw.extend(sec.get("episodes") or [])
    out = []
    for e in raw:
        out.append({
            "ep_id": e.get("ep_id"),
            "cid": e.get("cid"),
            "aid": e.get("aid"),
            "bvid": e.get("bvid"),
            "index": e.get("title") or "",
            "long_title": e.get("long_title") or "",
            "badge": e.get("badge") or "",
            "duration": e.get("duration") or 0,
            "section_type": e.get("section_type", 0),
        })
    return out


def fetch_play(client: HttpClient, ep: dict, qn: int, is_pgc: bool = True) -> dict:
    if is_pgc:
        params = {
            "ep_id": ep["ep_id"], "cid": ep["cid"], "avid": ep["aid"],
            "qn": qn, "fnval": 4048, "fnver": 0, "fourk": 1,
            "drm_tech_type": 1, "from_client": "BROWSER", "session": "",
            "support_multi_audio": 1, "voice_balance": 1,
        }
        j = client.api(API_PLAYURL_PGC, params, wbi=True)
        if j.get("code") != 0:
            j = client.api(API_PLAYURL_PGC, params, wbi=False)
    else:
        params = {
            "avid": ep["aid"], "bvid": ep.get("bvid"), "cid": ep["cid"],
            "qn": qn, "fnval": 4048, "fnver": 0, "fourk": 1,
        }
        j = client.api(API_PLAYURL_UG, params, wbi=True)
    if j.get("code") != 0:
        code = j.get("code")
        raise SystemExit(f"playurl failed: code={code} {j.get('message')}\n"
                         f"hint: {CODE_HINT.get(code, '')}")
    res = j.get("result") or j.get("data") or {}
    if not res:
        raise SystemExit("empty playurl (paid / delisted content?)")
    return res


# ---------------------------------------------------------------------------
# stream selection
# ---------------------------------------------------------------------------


def video_candidates(play: dict) -> list:
    dash = play.get("dash") or {}
    out = []
    for v in dash.get("video") or []:
        urls = []
        if v.get("baseUrl") or v.get("base_url"):
            urls.append(v.get("baseUrl") or v.get("base_url"))
        urls += [u for u in (v.get("backupUrl") or v.get("backup_url") or []) if u]
        out.append({
            "qn": int(v.get("id") or 0),
            "codecid": int(v.get("codecid") or 0),
            "bandwidth": int(v.get("bandwidth") or 0),
            "width": int(v.get("width") or 0),
            "height": int(v.get("height") or 0),
            "urls": [u for u in urls if u],
        })
    return [c for c in out if c["urls"]]


def audio_candidates(play: dict) -> list:
    dash = play.get("dash") or {}
    out = []
    for a in dash.get("audio") or []:
        urls = [a.get("baseUrl") or a.get("base_url")] + list(
            a.get("backupUrl") or a.get("backup_url") or [])
        out.append({"id": int(a.get("id") or 0), "bandwidth": int(a.get("bandwidth") or 0),
                    "urls": [u for u in urls if u], "kind": "dash"})
    flac = (dash.get("flac") or {}).get("audio")
    if flac and (flac.get("baseUrl") or flac.get("base_url")):
        urls = [flac.get("baseUrl") or flac.get("base_url")] + list(flac.get("backupUrl") or [])
        out.append({"id": 30251, "bandwidth": int(flac.get("bandwidth") or 0),
                    "urls": [u for u in urls if u], "kind": "flac"})
    dolby = (dash.get("dolby") or {}).get("audio") or []
    if isinstance(dolby, list):
        for a in dolby:
            urls = [a.get("baseUrl") or a.get("base_url")] + list(a.get("backupUrl") or [])
            out.append({"id": int(a.get("id") or 30250),
                        "bandwidth": int(a.get("bandwidth") or 0),
                        "urls": [u for u in urls if u], "kind": "dolby"})
    return [a for a in out if a["urls"]]


def choose_video(cands: list, want_qn: int, codec: int | None) -> dict:
    ok = [c for c in cands if c["qn"] <= want_qn] if want_qn else list(cands)
    if not ok:
        ok = list(cands)
    best_qn = min((c["qn"] for c in ok), key=qual_rank)
    pool = [c for c in ok if c["qn"] == best_qn]
    if codec:
        same = [c for c in pool if c["codecid"] == codec]
        if same:
            pool = same
    return max(pool, key=lambda c: c["bandwidth"])


def choose_audio(cands: list, want: str) -> dict | None:
    if not cands:
        return None
    if want in ("flac", "hires"):
        for c in cands:
            if c["kind"] == "flac":
                return c
    if want in ("dolby", "atmos"):
        for c in cands:
            if c["kind"] == "dolby":
                return c
    if want.isdigit():
        for c in cands:
            if c["id"] == int(want):
                return c
    for pid in (30251, 30250, 30280, 30232, 30216):
        for c in cands:
            if c["id"] == pid:
                return c
    return max(cands, key=lambda c: c["bandwidth"])


# ---------------------------------------------------------------------------
# downloader
# ---------------------------------------------------------------------------


class RateLimiter:
    """Token bucket shared by all range threads, so the *total* speed is capped.

    rate_bytes == 0 means unlimited. Burst is 50 ms of budget: small enough that
    the average is honest, big enough that we don't sleep on every read.
    """

    def __init__(self, rate_bytes: int = 0):
        self.rate = max(0, int(rate_bytes))
        self.cap = self.rate * 0.05
        self.budget = self.cap
        self.t0 = time.time()
        self.lock = threading.Lock()

    def set_rate(self, rate_bytes: int):
        with self.lock:
            self.rate = max(0, int(rate_bytes))
            self.cap = self.rate * 0.05
            self.budget = self.cap
            self.t0 = time.time()

    def suggest_chunk(self, default: int = 256 * 1024) -> int:
        """Read size worth ~125 ms of budget, so throttling stays smooth."""
        if self.rate <= 0:
            return default
        return max(16 * 1024, min(default, self.rate // 8))

    def consume(self, n: int):
        if self.rate <= 0 or n <= 0:
            return
        with self.lock:
            now = time.time()
            self.budget = min(self.cap, self.budget + (now - self.t0) * self.rate)
            self.t0 = now
            self.budget -= n
            wait = (-self.budget / self.rate) if self.budget < 0 else 0.0
        if wait > 0:
            time.sleep(wait)


class Downloader:
    def __init__(self, client: HttpClient, threads: int = 4, rate_limit: int = 0):
        self.client = client
        self.threads = max(1, threads)
        self.limiter = RateLimiter(rate_limit)

    @property
    def rate_limit(self) -> int:
        return self.limiter.rate

    def _size(self, url: str) -> int:
        try:
            with self.client.raw(url, {"Range": "bytes=0-0"}) as r:
                cr = r.headers.get("Content-Range") or ""
                if "/" in cr:
                    return int(cr.rsplit("/", 1)[-1])
                return int(r.headers.get("Content-Length") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def download(self, name: str, urls: list, dest: str) -> str:
        os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
        if os.path.exists(dest) and self._size(urls[0]) == os.path.getsize(dest):
            print("   [ok] already downloaded")
            return dest
        last_err = None
        for url in urls:
            try:
                size = self._size(url)
                if size <= 0:
                    continue
                if self._fetch_parallel(url, dest, size, name):
                    return dest
            except Exception as e:  # noqa: BLE001
                last_err = e
        for url in urls:
            try:
                self._stream_to(url, dest, name)
                return dest
            except Exception as e:  # noqa: BLE001
                last_err = e
        raise RuntimeError(f"download failed: {last_err}")

    def _fetch_parallel(self, url: str, dest: str, size: int, name: str) -> bool:
        n = self.threads
        if size < 4 * 1024 * 1024:
            n = 1
        chunk = size // n
        ranges = []
        for i in range(n):
            start = i * chunk
            end = size - 1 if i == n - 1 else (i + 1) * chunk - 1
            ranges.append((start, end))
        tmpdir = tempfile.mkdtemp(prefix="bili_")
        part_paths = [os.path.join(tmpdir, f"p{i}") for i in range(n)]
        done = [0] * n
        lock = threading.Lock()
        t0 = time.time()

        def work(i, start, end):
            path = part_paths[i]
            for attempt in range(3):
                try:
                    with self.client.raw(url, {"Range": f"bytes={start}-{end}"}) as r, \
                            open(path, "wb") as f:
                        while True:
                            buf = r.read(self.limiter.suggest_chunk())
                            if not buf:
                                break
                            f.write(buf)
                            self.limiter.consume(len(buf))
                            with lock:
                                done[i] += len(buf)
                                total = sum(done)
                                sp = total / max(1e-6, time.time() - t0)
                                sys.stdout.write(
                                    f"\r   {name}  {total * 100.0 / size:5.1f}%  "
                                    f"{fmt_size(total)}/{fmt_size(size)}  {fmt_size(sp)}/s   ")
                                sys.stdout.flush()
                    if os.path.getsize(path) == end - start + 1:
                        return True
                except Exception:  # noqa: BLE001
                    time.sleep(1.0 + attempt)
            return False

        ok = True
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = [ex.submit(work, i, s, e) for i, (s, e) in enumerate(ranges)]
            for f in as_completed(futs):
                ok = f.result() and ok
        sys.stdout.write("\n")
        if not ok:
            shutil.rmtree(tmpdir, ignore_errors=True)
            return False
        with open(dest, "wb") as out:
            for p in part_paths:
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, out, 1024 * 1024)
        shutil.rmtree(tmpdir, ignore_errors=True)
        return True

    def _stream_to(self, url: str, dest: str, name: str):
        size = self._size(url)
        done = 0
        t0 = time.time()
        with self.client.raw(url) as r, open(dest, "wb") as f:
            while True:
                buf = r.read(self.limiter.suggest_chunk())
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                self.limiter.consume(len(buf))
                sp = done / max(1e-6, time.time() - t0)
                if size:
                    sys.stdout.write(f"\r   {name}  {done * 100.0 / size:5.1f}%  "
                                     f"{fmt_size(done)}/{fmt_size(size)}  {fmt_size(sp)}/s   ")
                else:
                    sys.stdout.write(f"\r   {name}  {fmt_size(done)}  {fmt_size(sp)}/s   ")
                sys.stdout.flush()
        sys.stdout.write("\n")


# ---------------------------------------------------------------------------
# ffmpeg
# ---------------------------------------------------------------------------


def find_ffmpeg() -> str | None:
    for name in ("ffmpeg", "ffmpeg.exe"):
        p = shutil.which(name)
        if p:
            return p
    cands = [
        os.path.join(TOOL_DIR, "ffmpeg.exe"),
        os.path.join(TOOL_DIR, "bin", "ffmpeg.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "bin", "ffmpeg.exe"),
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/opt/homebrew/bin/ffmpeg",
    ]
    for c in cands:
        if c and os.path.isfile(c):
            return c
    return None


def install_ffmpeg(target_dir: str = TOOL_DIR) -> str | None:
    print("ffmpeg not found, downloading (~100MB, override with FFMPEG_MIRROR) ...")
    env_url = os.environ.get("FFMPEG_MIRROR")
    urls = ([env_url] if env_url else []) + FFMPEG_WIN_URLS
    bin_dir = os.path.join(target_dir, "bin")
    os.makedirs(bin_dir, exist_ok=True)
    zpath = os.path.join(tempfile.gettempdir(), "ffmpeg-dl.zip")
    for url in urls:
        try:
            print(f"   source: {url.split('/')[2]}")
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r, open(zpath, "wb") as f:
                total = int(r.headers.get("Content-Length") or 0)
                done, t0 = 0, time.time()
                while True:
                    buf = r.read(1024 * 1024)
                    if not buf:
                        break
                    f.write(buf)
                    done += len(buf)
                    if total:
                        sys.stdout.write(
                            f"\r   {done * 100.0 / total:5.1f}%  {fmt_size(done)}/"
                            f"{fmt_size(total)}  "
                            f"{fmt_size(done / max(1e-6, time.time() - t0))}/s   ")
                        sys.stdout.flush()
            sys.stdout.write("\n")
            hit = 0
            with zipfile.ZipFile(zpath) as z:
                for member in z.namelist():
                    base = os.path.basename(member)
                    if base in ("ffmpeg.exe", "ffprobe.exe"):
                        with z.open(member) as src, open(os.path.join(bin_dir, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        hit += 1
            if hit:
                p = os.path.join(bin_dir, "ffmpeg.exe")
                print(f"ffmpeg ready: {p}")
                return p if os.path.isfile(p) else None
        except Exception as e:  # noqa: BLE001
            print(f"   failed: {e}")
            continue
        finally:
            try:
                os.remove(zpath)
            except OSError:
                pass
    print("auto download failed, install manually: winget install -e --id Gyan.FFmpeg")
    return None


DELOGO_PRESETS = {
    "tl": (0.03, 0.03, 0.24, 0.10),
    "tr": (0.73, 0.03, 0.24, 0.10),
    "bl": (0.03, 0.87, 0.24, 0.10),
    "br": (0.73, 0.87, 0.24, 0.10),
}
DELOGO_OFF = ("", "0", "off", "none", "false", "no")
# delogo = 插值（画质最好，适合小块静态 logo）
# blur   = 局部高斯模糊（半透明/大字更自然，不会出现插值色块）
# mosaic = 马赛克（"明确遮盖"场景，不追求还原）
# crop   = 直接裁掉水印所在的那条边（区域内零损失，代价是构图变化）
# mask   = removelogo + 遮罩图（任意形状/大面积，需要自己画 mask）
DELOGO_MODES = ("delogo", "blur", "mosaic", "crop", "mask")


def parse_regions(spec, width: int, height: int) -> list:
    """Parse "tl" / "x:y:w:h" / "x:y:w:h;x:y:w:h" into pixel boxes."""
    s = str(spec or "").strip().lower()
    if s in DELOGO_OFF:
        return []
    w_px, h_px = int(width or 0), int(height or 0)
    boxes = []
    for part in s.split(";"):
        part = part.strip()
        if not part:
            continue
        if ":" in part or "," in part:
            nums = re.split(r"[:,]", part)
            if len(nums) != 4:
                raise ValueError(f"水印区域要写成 x:y:w:h，收到 {part!r}")
            x, y, w, h = (int(n) for n in nums)
        elif part in DELOGO_PRESETS:
            if w_px <= 8 or h_px <= 8:
                raise ValueError("用预设角落需要先知道分辨率")
            px, py, pw, ph = DELOGO_PRESETS[part]
            x, y = int(w_px * px), int(h_px * py)
            w, h = int(w_px * pw), int(h_px * ph)
        else:
            raise ValueError(f"无法识别的水印区域 {part!r}（可用 tl/tr/bl/br 或 x:y:w:h）")
        if w_px > 0 and h_px > 0:
            x = max(1, min(x, w_px - 4))
            y = max(1, min(y, h_px - 4))
            w = max(4, min(w, w_px - x - 1))
            h = max(4, min(h, h_px - y - 1))
        x -= x % 2
        y -= y % 2
        w -= w % 2
        h -= h % 2
        boxes.append((max(1, x), max(1, y), max(4, w), max(4, h)))
    return boxes


def build_delogo(spec, width: int, height: int) -> str | None:
    """Backwards-compatible helper: rectangle delogo for one or more boxes."""
    return build_filter(spec, width, height, mode="delogo")


def build_filter(spec, width: int, height: int, mode: str = "delogo",
                 mask_path: str | None = None, time_range: str | None = None,
                 raw_filter: str | None = None, src_codecid: int = 0) -> str | None:
    """Build the -vf chain that removes the watermark.

    spec: "tl" / "x:y:w:h" / "x:y:w:h;x:y:w:h"   (None/off => no filter)
    mode: delogo | blur | mosaic | crop | mask
    time_range: "0-30" => only those seconds (b站片头 logo 常见)
    raw_filter: pass a hand-written avfilter string straight through
    """
    if raw_filter:
        return str(raw_filter).strip() or None
    w_px, h_px = int(width or 0), int(height or 0)
    mode = (mode or "delogo").strip().lower()
    if mode not in DELOGO_MODES:
        raise ValueError(f"未知去水印模式 {mode!r}（可选 {'/'.join(DELOGO_MODES)}）")

    if mode == "mask":
        if not mask_path:
            raise ValueError("mask 模式需要一张遮罩图（白色=要抹掉的位置）")
        esc = str(mask_path).replace("\\", "/").replace(":", "\\:")
        return f"removelogo=filename='{esc}'"

    boxes = parse_regions(spec, width, height)
    if not boxes:
        return None

    enable = ""
    if time_range:
        a, _, b = str(time_range).partition("-")
        try:
            t0 = float(a or 0)
            t1 = float(b) if b else 1e6
        except ValueError:
            raise ValueError(f"时间段要写成 起-止 秒，收到 {time_range!r}")
        enable = f":enable='between(t,{t0:g},{t1:g})'"

    if mode == "crop":
        x, y, w, h = boxes[0]
        edges = {"top": y, "bottom": max(0, h_px - (y + h)),
                 "left": x, "right": max(0, w_px - (x + w))}
        side = min(edges, key=edges.get)
        if side == "top" and h_px - (y + h) >= 16:
            return f"crop={w_px}:{h_px - (y + h)}:0:{y + h}"
        if side == "bottom" and y >= 16:
            return f"crop={w_px}:{y}:0:0"
        if side == "left" and w_px - (x + w) >= 16:
            return f"crop={w_px - (x + w)}:{h_px}:{x + w}:0"
        if x >= 16:
            return f"crop={x}:{h_px}:0:0"
        raise ValueError("水印区域贴着画面中间，crop 模式没法裁，请改用 delogo/mask")

    if mode == "delogo":
        return ",".join(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0{enable}" for x, y, w, h in boxes)

    # blur / mosaic: 每个区域 split 一次，处理完 overlay 回去，支持多区域串联
    parts = []
    for i, (x, y, w, h) in enumerate(boxes):
        parts.append(f"split[s{i}][t{i}]")
        if mode == "blur":
            parts.append(f"[t{i}]crop={w}:{h}:{x}:{y},gblur=sigma=22{enable}[b{i}]")
        else:  # mosaic
            bw, bh = max(2, w // 16), max(2, h // 16)
            parts.append(f"[t{i}]crop={w}:{h}:{x}:{y},"
                         f"scale={bw}:{bh}:flags=neighbor,scale={w}:{h}:flags=neighbor[b{i}]")
        parts.append(f"[s{i}][b{i}]overlay={x}:{y}")
    return ";".join(parts)


def encode_args(height: int, src_codecid: int = 0) -> list:
    """Re-encode settings: keep 4K/HEVC sources on x265 so the file stays sane."""
    if (height or 0) >= 1800 or src_codecid == 12:
        return ["-c:v", "libx265", "-crf", "21", "-preset", "veryfast",
                "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-crf", "18", "-preset", "veryfast", "-pix_fmt", "yuv420p"]


def merge(ffmpeg: str, vpath: str, apath: str | None, out: str,
          subs: list | None = None, vf: str | None = None,
          enc: list | None = None) -> bool:
    """Mux video+audio(+subs). With `vf` (e.g. delogo) the video is re-encoded."""
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", vpath]
    if apath:
        cmd += ["-i", apath]
    for s in subs or []:
        cmd += ["-i", s]
    cmd += ["-map", "0:v:0"]
    if apath:
        cmd += ["-map", "1:a:0"]
    for i, _ in enumerate(subs or []):
        cmd += ["-map", f"{2 + i}:s:0"]
    if vf:
        cmd += ["-vf", vf] + (enc or encode_args(0)) + ["-c:a", "copy"]
    else:
        cmd += ["-c", "copy"]
    if subs:
        cmd += ["-c:s", "mov_text"]
    cmd += ["-movflags", "+faststart", out]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return True
    except subprocess.CalledProcessError as e:
        msg = (e.stderr or b"").decode("utf-8", "replace").strip()
        print(f"   [x] merge failed: {msg[:400]}")
        return False


def srt_from_bili_json(data: dict) -> str:
    def ts(sec: float) -> str:
        ms = int(round(sec * 1000))
        h, ms = divmod(ms, 3600000)
        m, ms = divmod(ms, 60000)
        s, ms = divmod(ms, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    lines = []
    for i, item in enumerate(data.get("body") or [], 1):
        content = (item.get("content") or "").strip()
        if not content:
            continue
        lines.append(f"{i}\n{ts(item.get('from', 0))} --> {ts(item.get('to', 0))}\n{content}\n")
    return "\n".join(lines)


def fetch_subtitles(client: HttpClient, ep: dict) -> list:
    base = {"aid": ep["aid"], "cid": ep["cid"], "ep_id": ep["ep_id"]}
    for url in (API_PLAYER_V2, API_PLAYER_V2_PLAIN):
        try:
            p = client.sign(base) if "wbi" in url else base
            j = client.get_json(url + "?" + urllib.parse.urlencode(p))
        except Exception:  # noqa: BLE001
            continue
        subs = ((j.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        out = []
        for s in subs:
            su = s.get("subtitle_url") or ""
            if not su:
                continue
            if su.startswith("//"):
                su = "https:" + su
            try:
                body = json.loads(client.get_text(su))
            except Exception:  # noqa: BLE001
                continue
            text = srt_from_bili_json(body)
            if text.strip():
                out.append((s.get("lan") or "zh-CN", text))
        if out:
            return out
    return []


# ---------------------------------------------------------------------------
# login: browser + CDP cookie capture
# ---------------------------------------------------------------------------


def find_browser() -> str | None:
    cands = []
    for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
        root = os.environ.get(env)
        if not root:
            continue
        cands += [
            os.path.join(root, "Microsoft", "Edge", "Application", "msedge.exe"),
            os.path.join(root, "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(root, "Chromium", "Application", "chrome.exe"),
        ]
    for name in ("msedge", "chrome", "chromium", "brave", "vivaldi"):
        p = shutil.which(name)
        if p:
            cands.append(p)
    for c in cands:
        if os.path.isfile(c):
            return c
    return None


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class _WS:
    """Minimal WebSocket client (text frames, ping, close)."""

    def __init__(self, sock, leftover: bytes = b""):
        self.sock = sock
        self.buf = leftover

    def _read(self, n: int) -> bytes:
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _frame(self, opcode: int, data: bytes):
        header = bytearray([0x80 | opcode])
        n = len(data)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += n.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += n.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def send_text(self, text: str):
        self._frame(0x1, text.encode("utf-8"))

    def recv_text(self, timeout: float = 30.0) -> str:
        self.sock.settimeout(max(0.5, timeout))
        while True:
            b1, b2 = self._read(2)
            opcode = b1 & 0x0F
            masked = b2 & 0x80
            ln = b2 & 0x7F
            if ln == 126:
                ln = int.from_bytes(self._read(2), "big")
            elif ln == 127:
                ln = int.from_bytes(self._read(8), "big")
            mask = self._read(4) if masked else None
            payload = self._read(ln) if ln else b""
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x9:
                self._frame(0xA, payload)
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket close frame")
            if opcode in (0x1, 0x0):
                return payload.decode("utf-8", "replace")


class CDP:
    def __init__(self, ws_url: str):
        u = urllib.parse.urlparse(ws_url)
        host, port = u.hostname or "127.0.0.1", u.port or 80
        sock = socket.create_connection((host, port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {u.path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("CDP handshake failed")
            head += chunk
        status = head.split(b"\r\n", 1)[0]
        if b"101" not in status:
            raise ConnectionError(f"CDP rejected: {status.decode('utf-8', 'replace')}")
        self.ws = _WS(sock, head.split(b"\r\n\r\n", 1)[1])
        self._id = 0

    def call(self, method: str, params: dict | None = None, timeout: float = 30.0) -> dict:
        self._id += 1
        mid = self._id
        self.ws.send_text(json.dumps({"id": mid, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            msg = self.ws.recv_text(timeout=max(0.5, end - time.time()))
            try:
                j = json.loads(msg)
            except Exception:  # noqa: BLE001
                continue
            if j.get("id") == mid:
                return j
        raise TimeoutError(f"CDP timeout: {method}")

    def close(self):
        try:
            self.ws.sock.close()
        except Exception:  # noqa: BLE001
            pass


def _http_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}),
                                timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def verify_login(cookie: str) -> dict | None:
    try:
        d = HttpClient(cookie).get_json(API_NAV).get("data") or {}
    except Exception:  # noqa: BLE001
        return None
    if not d.get("isLogin"):
        return None
    return {
        "uname": d.get("uname") or "",
        "vip": int(d.get("vipStatus") or 0) == 1 or int(d.get("vipType") or 0) > 0,
        "vip_label": ((d.get("vip_label") or {}).get("text") or ""),
        "level": (d.get("level_info") or {}).get("current_level", 0),
    }


def load_cookie_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        return ""
    if "\t" in raw:  # Netscape format
        pairs = []
        for line in raw.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            cols = line.split("\t")
            if len(cols) >= 7:
                pairs.append(f"{cols[5].strip()}={cols[6].strip()}")
        return "; ".join(pairs)
    cookie = raw.strip()
    if cookie and "=" not in cookie and len(cookie) > 20:
        cookie = f"SESSDATA={cookie}"
    return cookie


def save_cookie(cookie: str, path: str = DEFAULT_COOKIE_FILE) -> str:
    with open(path, "w", encoding="utf-8") as f:
        f.write(cookie.strip() + "\n")
    try:
        os.chmod(path, 0o600)
    except Exception:  # noqa: BLE001
        pass
    return path


def browser_login(timeout: int = 240, profile: str = DEFAULT_PROFILE_DIR,
                  headless: bool = False) -> str | None:
    exe = find_browser()
    if not exe:
        print("   [!] no Edge/Chrome found, cannot capture cookies automatically")
        return None
    os.makedirs(profile, exist_ok=True)
    port = _free_port()
    cmd = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-popup-blocking",
    ]
    if headless:
        cmd.append("--headless=new")
    cmd.append(LOGIN_URL)

    print(f"   browser: {exe}")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    target = None
    try:
        t0 = time.time()
        while time.time() - t0 < 30:
            if proc.poll() is not None:
                print("   [!] browser exited unexpectedly")
                return None
            try:
                pages = [t for t in _http_json(base + "/json/list")
                         if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
            except Exception:  # noqa: BLE001
                time.sleep(0.4)
                continue
            if pages:
                pages.sort(key=lambda t: 0 if "bilibili" in (t.get("url") or "") else 1)
                target = pages[0]
                break
        if not target:
            print("   [!] cannot reach the browser debug port (blocked?)")
            return None

        cdp = CDP(target["webSocketDebuggerUrl"])
        try:
            print("\n   Login page opened. Scan the QR code / sign in in the browser.")
            print(f"   This window continues automatically (max {timeout}s) ...\n")
            deadline = time.time() + timeout
            tick = 0
            while time.time() < deadline:
                try:
                    j = cdp.call("Storage.getCookies", {}, timeout=15)
                except Exception:  # noqa: BLE001
                    j = {}
                jar = {}
                for c in (j.get("result") or {}).get("cookies") or []:
                    if "bilibili" in (c.get("domain") or ""):
                        jar[c.get("name", "")] = c.get("value", "")
                if jar.get("SESSDATA"):
                    cookie = "; ".join(f"{k}={v}" for k, v in jar.items() if k and v)
                    info = verify_login(cookie)
                    if info:
                        path = save_cookie(cookie)
                        label = info["vip_label"] or ("VIP" if info["vip"] else "normal")
                        print(f"   [ok] logged in: {info['uname']} ({label}, Lv{info['level']})")
                        print(f"   [ok] cookie saved: {path}")
                        return cookie
                tick += 1
                if jar and tick % 4 == 0:
                    print(f"      got {len(jar)} bilibili cookies, waiting for SESSDATA ... "
                          f"{int(deadline - time.time())}s left")
                elif tick % 5 == 0:
                    print(f"      waiting for login ... {int(deadline - time.time())}s left")
                time.sleep(1.5)
            print("   [!] timeout, no valid login captured")
            return None
        finally:
            cdp.close()
    finally:
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        except Exception:  # noqa: BLE001
            try:
                proc.terminate()
            except Exception:  # noqa: BLE001
                pass


def cmd_login(args) -> str | None:
    if os.path.isfile(DEFAULT_COOKIE_FILE) and not args.force_login:
        cached = load_cookie_file(DEFAULT_COOKIE_FILE)
        if cached:
            info = verify_login(cached)
            if info:
                label = info["vip_label"] or ("VIP" if info["vip"] else "normal")
                print(f"   [ok] reusing saved login: {info['uname']} ({label})")
                print(f"        -> {DEFAULT_COOKIE_FILE}  (use --force-login to switch)")
                return cached
            print("   [!] saved cookie expired, logging in again")

    cookie = browser_login(timeout=args.login_timeout, profile=args.profile)
    if cookie:
        return cookie

    print("\n   Automatic capture failed. Paste the cookie manually:")
    print("   F12 -> Application -> Cookies -> https://www.bilibili.com -> copy SESSDATA")
    manual = ask("   SESSDATA (empty = skip): ")
    if manual:
        cookie = manual if "=" in manual else f"SESSDATA={manual}"
        info = verify_login(cookie)
        if info:
            path = save_cookie(cookie)
            label = info["vip_label"] or ("VIP" if info["vip"] else "normal")
            print(f"   [ok] logged in: {info['uname']} ({label})")
            print(f"   [ok] cookie saved: {path}")
            return cookie
        print("   [!] that SESSDATA is invalid or expired")
    return None


# ---------------------------------------------------------------------------
# interactive wizard
# ---------------------------------------------------------------------------


def interactive_wizard(args) -> str | None:
    print("=" * 62)
    print("  Bilibili PGC downloader - bangumi / movie / documentary / course")
    print("=" * 62)
    print("  1) login (optional)   2) link   3) episodes (single/pick/all)   4) quality")

    info = None
    if os.path.isfile(DEFAULT_COOKIE_FILE):
        info = verify_login(load_cookie_file(DEFAULT_COOKIE_FILE))

    if not getattr(args, "login_done", False):
        if info:
            label = info["vip_label"] or ("VIP" if info["vip"] else "normal")
            print(f"\n   saved login found: {info['uname']} ({label})")
            if ask("   login again / switch account? [y/N] (Enter = keep): ",
                   "n").lower().startswith("y"):
                args.force_login = True
                c = cmd_login(args)
                if c:
                    args.cookie = c
        else:
            print("\n   Login first? (1080P / 1080P+ / 4K / HDR need login + VIP)")
            print("     [1] Yes - open browser, scan QR, cookie saved automatically")
            print("     [2] No  - guest mode, PGC max 480P")
            if ask("   choose [1/2], Enter = 2: ", "2") in ("1", "y", "Y"):
                c = cmd_login(args)
                if c:
                    args.cookie = c
                else:
                    print("   login not completed, continuing as guest.")

    print()
    link = ask("   paste ep/ss/md link or BV id, Enter to start: ")
    if not link:
        print("   no link given, bye.")
        return None
    return link


def choose_scope(eps: list, main_only: list, current: dict | None) -> list:
    """Interactive episode picker. Returns the episodes to download."""
    n_pv = len(eps) - len(main_only)
    tip = f"{len(main_only)} main episodes"
    if n_pv:
        tip += f" + {n_pv} PV/extras"
    print(f"\n  This season has {tip}:")
    print("   idx   ep_id      type  no.    title")
    for i, e in enumerate(eps, 1):
        kind = "PV" if e.get("section_type") == 1 else "EP"
        mark = "  [VIP]" if e.get("badge") else ""
        print(f"  {i:>4}  {str(e['ep_id']):<9}  {kind:<4}  {str(e.get('index', '')):<5}  "
              f"{e.get('long_title', '')}{mark}")

    cur_idx = next((i for i, e in enumerate(eps, 1) if e is current), None)
    print("\n  What to download:")
    if cur_idx:
        print(f"    [Enter] only the current episode (idx {cur_idx})")
    else:
        print("    [Enter] only the 1st episode")
    print(f"    all     the whole season ({len(main_only)} main episodes)")
    if n_pv:
        print(f"    all+    whole season + PV/extras ({len(eps)} items)")
        print("    pv      only PV / extras")
    print("    2-5,8   pick episodes by idx (also 1-3,7 style)")

    while True:
        s = ask("  choice: ").strip().lower()
        if s in ("", "cur", "c"):
            return [current] if current else main_only[:1]
        if s in ("all", "a", "full"):
            return main_only
        if s in ("all+", "a+", "+", "allpv"):
            return eps
        if s in ("pv", "extra", "extras"):
            pv = [e for e in eps if e.get("section_type") == 1]
            if pv:
                return pv
            print("   [!] no PV/extras here, choose again")
            continue
        nums = set(parse_ep_range(s, len(eps)))
        if nums:
            return [e for i, e in enumerate(eps, 1) if i in nums]
        print("   invalid input (all / all+ / 2-5,8 / Enter)")


# ---------------------------------------------------------------------------
# per-episode work
# ---------------------------------------------------------------------------


def ep_filename(ep: dict) -> str:
    idx = (ep.get("index") or "").strip()
    lt = (ep.get("long_title") or "").strip()
    if idx.isdigit():
        head = f"EP{idx.zfill(3)}"
    else:
        head = idx or "EP"
    parts = [head]
    if lt and lt != idx:
        parts.append(lt)
    return sanitize(" ".join(parts))


def list_qualities(client: HttpClient, ep: dict) -> dict:
    play = fetch_play(client, ep, 127)
    vids = video_candidates(play)
    auds = audio_candidates(play)
    accept = play.get("accept_quality") or []
    got = play.get("quality")
    print(f"\nAvailable quality (account ceiling: {QN_NAME.get(got, got)}):")
    print("  idx    qn   quality     codec   resolution     bitrate")
    by_qn = {}
    for v in vids:
        by_qn.setdefault(v["qn"], []).append(v)
    order = sorted(by_qn, key=qual_rank)
    for i, qn in enumerate(order, 1):
        for k, v in enumerate(sorted(by_qn[qn], key=lambda c: -c["bandwidth"])):
            res = f"{v['width']}x{v['height']}" if v["width"] else "-"
            head = f"  {i:>4}  {qn:>4}" if k == 0 else " " * 10
            print(f"{head}  {QN_NAME.get(qn, str(qn)):<9}  "
                  f"{CODEC_NAME.get(v['codecid'], str(v['codecid'])):<6}  {res:<13}  "
                  f"{v['bandwidth'] // 1000}kbps")
    if auds:
        print("  audio: " + ", ".join(f"{AUDIO_NAME.get(a['id'], a['id'])}"
                                      f"({a['bandwidth'] // 1000}k)" for a in auds))
    locked = [q for q in accept if q not in by_qn]
    if locked:
        print("  needs higher permission (login/VIP): "
              + ", ".join(QN_NAME.get(q, str(q)) for q in locked))
    if (play.get("dash") or {}).get("drm_tech_type"):
        print("  [!] DRM protected - use --drm-key KID:KEY after download")
    return {"play": play, "video": vids, "audio": auds, "qns": order}


def pick_qn_interactive(qns: list) -> int:
    default = qns[0] if qns else 80
    print("\nSelect quality (Enter = best " + QN_NAME.get(default, str(default)) + "):")
    for i, qn in enumerate(qns, 1):
        print(f"  {i}. {QN_NAME.get(qn, str(qn))}")
    while True:
        s = ask("  idx: ")
        if not s:
            return default
        if s.isdigit():
            n = int(s)
            if 1 <= n <= len(qns):
                return qns[n - 1]
            if n in QN_NAME:
                return n
        print("  invalid, try again")


def download_one(client: HttpClient, dl: Downloader, ffmpeg: str | None,
                 ep: dict, want_qn: int, args) -> tuple[bool, str]:
    label = f"{ep['index']} {ep['long_title']}".strip()
    print(f"\n=== {label} (ep{ep['ep_id']}) ===")
    play = fetch_play(client, ep, want_qn, getattr(args, "is_pgc", True))
    vids = video_candidates(play)
    auds = audio_candidates(play)
    if not vids:
        print("   [x] no usable video stream (VIP only / region / delisted?)")
        return False, "no usable video stream (VIP only / region locked / delisted)"

    got_qn = int(play.get("quality") or 0)
    if got_qn and got_qn < want_qn:
        accept = play.get("accept_quality") or []
        if want_qn in accept:
            print(f"   [!] asked {QN_NAME.get(want_qn, want_qn)}, got "
                  f"{QN_NAME.get(got_qn, got_qn)} (this title has nothing higher)")
        else:
            print(f"   [!] asked {QN_NAME.get(want_qn, want_qn)}, got "
                  f"{QN_NAME.get(got_qn, got_qn)} (needs login/VIP, see --login)")

    codec = CODEC_ALIAS.get(args.codec) if args.codec and args.codec != "auto" else None
    v = choose_video(vids, want_qn, codec)
    a = choose_audio(auds, args.audio)
    print(f"   video: {QN_NAME.get(v['qn'], v['qn'])} / "
          f"{CODEC_NAME.get(v['codecid'], v['codecid'])} / "
          f"{v['width']}x{v['height']} / {v['bandwidth'] // 1000}kbps")
    if a:
        print(f"   audio: {AUDIO_NAME.get(a['id'], a['id'])} / {a['bandwidth'] // 1000}kbps")

    drm = bool((play.get("dash") or {}).get("drm_tech_type"))
    title = ep_filename(ep)
    outdir = os.path.join(args.output, sanitize(args.season_title or "bilibili"))
    os.makedirs(outdir, exist_ok=True)
    outfile = os.path.join(outdir, title + ".mp4")

    if not args.overwrite and os.path.exists(outfile):
        print(f"   [ok] exists, skip: {outfile}")
        return True, ""

    tmpdir = os.path.join(outdir, ".parts")
    os.makedirs(tmpdir, exist_ok=True)
    vpath = os.path.join(tmpdir, f"v_{ep['ep_id']}.m4s")
    apath = os.path.join(tmpdir, f"a_{ep['ep_id']}.m4s") if a else None

    try:
        dl.download("video", v["urls"], vpath)
        if a:
            dl.download("audio", a["urls"], apath)
    except Exception as e:  # noqa: BLE001
        print(f"   [x] download failed: {e}")
        return False, f"download failed: {e}"

    sub_files = []
    if args.subtitles:
        try:
            for lang, text in fetch_subtitles(client, ep):
                sp = os.path.join(tmpdir, f"s_{ep['ep_id']}_{sanitize(lang, 16)}.srt")
                with open(sp, "w", encoding="utf-8") as f:
                    f.write(text)
                sub_files.append(sp)
            print(f"   subtitles: {len(sub_files)}" if sub_files
                  else "   subtitles: none (PGC subs usually need VIP login)")
        except Exception:  # noqa: BLE001
            print("   subtitles: failed, skipped")

    if drm:
        enc = os.path.join(tmpdir, f"enc_{ep['ep_id']}.mp4")
        if ffmpeg and merge(ffmpeg, vpath, apath, enc, None):
            if args.drm_key:
                kid, _, key = args.drm_key.partition(":")
                try:
                    subprocess.run(["mp4decrypt", "--key", f"{kid}:{key}", enc, outfile],
                                   check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    print(f"   [ok] decrypted -> {outfile}")
                    return True, ""
                except Exception as e:  # noqa: BLE001
                    print(f"   [x] mp4decrypt failed ({e}), encrypted kept: {enc}")
                    return False, "mp4decrypt failed"
            print(f"   [!] DRM encrypted file: {enc}")
            print(f"       decrypt: mp4decrypt --key KID:KEY \"{enc}\" \"{outfile}\"")
            return False, "DRM encrypted, needs --drm-key"

    vf = None
    enc = None
    if getattr(args, "delogo", None) or getattr(args, "delogo_raw", None):
        try:
            vf = build_filter(
                getattr(args, "delogo", None), v["width"], v["height"],
                mode=getattr(args, "delogo_mode", "delogo") or "delogo",
                mask_path=getattr(args, "delogo_mask", None),
                time_range=getattr(args, "delogo_range", None),
                raw_filter=getattr(args, "delogo_raw", None),
                src_codecid=v["codecid"],
            )
        except ValueError as e:
            print(f"   [x] 去水印参数无效: {e}")
            return False, f"bad delogo: {e}"
        if vf:
            enc = encode_args(v["height"], v["codecid"])
            print(f"   去水印[{args.delogo_mode}]: {vf}")
            print(f"   重编码参数: {' '.join(enc)}")

    if not ffmpeg:
        raw = os.path.join(outdir, title + ".m4s")
        shutil.copyfile(vpath, raw)
        if apath:
            shutil.copyfile(apath, os.path.join(outdir, title + ".audio.m4s"))
        print(f"   [!] no ffmpeg, raw streams saved: {raw}")
        return True, ""

    if merge(ffmpeg, vpath, apath, outfile, sub_files, vf=vf, enc=enc):
        print(f"   [ok] done: {outfile}  ({fmt_size(os.path.getsize(outfile))})")
        for p in (vpath, apath, *sub_files):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return True, ""
    print(f"   [x] merge failed, parts kept in {tmpdir}")
    return False, "ffmpeg merge failed"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run_round(args, client: HttpClient, dl: Downloader, ffmpeg: str | None,
              interactive: bool):
    """One download task: resolve -> scope -> quality -> download.

    Returns an exit code, or None when there is nothing to do.
    """
    ref = resolve_input(client, args.inputs[0])
    if ref["kind"] in ("bv", "av"):
        print("[i] this is a normal UGC video, handled as a single episode.")
        url = (f"{API_VIEW}?bvid={ref['id']}" if ref["kind"] == "bv"
               else f"{API_VIEW}?aid={ref['id']}")
        view = client.get_json(url)
        if view.get("code") != 0:
            raise SystemExit(f"view failed: {view.get('message')}")
        v = view["data"]
        eps = [{"ep_id": 0, "cid": v["cid"], "aid": v["aid"], "bvid": v["bvid"],
                "index": "1", "long_title": v["title"], "badge": "",
                "duration": (v.get("duration") or 0) * 1000, "section_type": 0}]
        args.season_title = v["title"]
        is_pgc = False
    else:
        season = fetch_season(client, ref)
        eps = ep_list_of(season)
        args.season_title = season.get("season_title") or season.get("title") or "bilibili"
        is_pgc = True
        print(f"<<{args.season_title}>> {len(eps)} items (episodes + PV/extras)")
        if (season.get("rights") or {}).get("area_limit"):
            print("[!] region limited; use --proxy with a mainland China node if needed")
    args.is_pgc = is_pgc
    main_only = [e for e in eps if e.get("section_type", 0) == 0] or eps

    # ---- scope: single / pick / whole season ----
    if interactive and not (args.all or args.eps or args.ep):
        cur = next((e for e in eps if e["ep_id"] == ref["id"]), None) if ref["kind"] == "ep" else None
        target = choose_scope(eps, main_only, cur) if len(eps) > 1 else (cur or eps[:1])
        if not target:
            print("nothing selected.")
            return None
    elif args.ep:
        want_ids = {int(x) for x in re.findall(r"\d+", args.ep)}
        target = [e for e in eps if e["ep_id"] in want_ids]
    elif args.eps:
        if args.eps.strip().lower() in ("all", "*"):
            target = eps if args.with_pv else main_only
        else:
            nums = set(parse_ep_range(args.eps, len(eps)))
            target = [e for i, e in enumerate(eps, 1) if i in nums]
    elif args.all:
        target = eps if args.with_pv else main_only
    elif ref["kind"] == "ep":
        target = [e for e in eps if e["ep_id"] == ref["id"]] or eps[:1]
    elif ref["kind"] == "ss" or args.list:
        target = main_only
    else:
        target = [eps[0]] if eps else []

    if not target:
        print("no matching episodes")
        return 1

    if args.list:
        print("\nidx   ep_id      type  no.    title")
        for i, e in enumerate(eps, 1):
            kind = "PV" if e.get("section_type") == 1 else "EP"
            print(f"{i:>4}  {str(e['ep_id']):<9}  {kind:<4}  {str(e.get('index', '')):<5}  "
                  f"{e.get('long_title', '')}{'  [VIP]' if e.get('badge') else ''}")
        if is_pgc:
            list_qualities(client, target[0])
        return 0

    if args.dry_run:
        print(f"\n[dry-run] {len(target)} item(s):")
        for i, e in enumerate(target, 1):
            kind = "PV" if e.get("section_type") == 1 else "EP"
            print(f"  {i:>3}. ep{e['ep_id']}  {kind}  {e.get('index', '')} "
                  f"{e.get('long_title', '')}{'  [VIP]' if e.get('badge') else ''}")
        return 0

    # ---- quality ----
    if args.quality:
        want_qn = pick_quality(args.quality)
    elif interactive:
        info = list_qualities(client, target[0])
        want_qn = pick_qn_interactive(info["qns"]) if info["qns"] else 80
    else:
        want_qn = 127
        print("[i] no -q given, requesting the highest quality your account allows")

    print(f"\ntarget quality: {QN_NAME.get(want_qn, want_qn)}   "
          f"episodes: {len(target)}   output: {os.path.abspath(args.output)}")

    okc = 0
    fails = []
    t_start = time.time()
    for i, ep in enumerate(target, 1):
        print(f"\n[{i}/{len(target)}]", end="")
        try:
            ok, reason = download_one(client, dl, ffmpeg, ep, want_qn, args)
        except SystemExit:
            raise
        except Exception as e:  # noqa: BLE001
            ok, reason = False, f"exception: {e}"
            print(f"   [x] {e}")
        if ok:
            okc += 1
        else:
            fails.append((ep, reason))
        time.sleep(0.6)

    cost = time.time() - t_start
    print(f"\ndone {okc}/{len(target)}  in {int(cost // 60)}m{int(cost % 60)}s")
    if fails:
        print("failed:")
        for ep, reason in fails:
            tag = "PV" if ep.get("section_type") == 1 else f"EP{ep.get('index')}"
            print(f"  - {tag} {ep.get('long_title', '')}: {reason}")
        print("hint: VIP episodes need a VIP cookie (--login); "
              "re-run to resume, finished files are skipped.")
    return 0 if okc else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Bilibili PGC downloader with quality / episode selection",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("inputs", nargs="*", help="ep/ss/md link, BV/av id")
    ap.add_argument("-q", "--quality", default=None,
                    help="best/8k/4k/1080p+/1080p/720p/480p/360p or qn number")
    ap.add_argument("-l", "--list", action="store_true", help="list episodes + qualities, exit")
    ap.add_argument("-c", "--cookie", default="", help='cookie string "SESSDATA=xxx; bili_jct=yyy"')
    ap.add_argument("-C", "--cookie-file", default=None, help="cookie file (Netscape or one-line)")
    ap.add_argument("-o", "--output", default="downloads", help="output dir (default: downloads)")
    ap.add_argument("-p", "--proxy", default=None, help="http proxy, e.g. http://127.0.0.1:7890")
    ap.add_argument("--all", action="store_true", help="whole season (main episodes)")
    ap.add_argument("--with-pv", action="store_true", help="include PV/extras with --all / --eps all")
    ap.add_argument("--eps", default=None, help='episode idx list: 1-3,7  or  "all"')
    ap.add_argument("--ep", default=None, help="pick by ep_id: 693247,693248")
    ap.add_argument("--audio", default="auto", help="auto/best/flac/dolby/30280/30232/30216")
    ap.add_argument("--codec", default="auto", help="auto/avc/hevc/av1")
    ap.add_argument("--threads", type=int, default=4, help="range threads per stream (default 4)")
    ap.add_argument("--limit", type=int, default=0,
                    help="download speed limit in KB/s for the whole task (0 = unlimited)")
    ap.add_argument("--delogo", default=None,
                    help="remove watermark: tl/tr/bl/br preset, x:y:w:h, or several boxes "
                         "joined by ';' (re-encodes video)")
    ap.add_argument("--delogo-mode", default="delogo",
                    choices=list(DELOGO_MODES),
                    help="delogo=inpaint (best quality), blur, mosaic, crop (lossless but "
                         "reframes), mask (needs --delogo-mask)")
    ap.add_argument("--delogo-range", default=None,
                    help="only remove inside this time span, e.g. 0-30 (seconds)")
    ap.add_argument("--delogo-mask", default=None,
                    help="mask image for --delogo-mode mask (white = erase)")
    ap.add_argument("--delogo-raw", default=None,
                    help="pass a raw ffmpeg -vf chain through, e.g. \"delogo=x=10:y=10:w=100:h=40\"")
    ap.add_argument("--subtitles", dest="subtitles", action="store_true", default=True)
    ap.add_argument("--no-subtitles", dest="subtitles", action="store_false")
    ap.add_argument("--drm-key", default=None, help="drm key KID:KEY for mp4decrypt")
    ap.add_argument("--overwrite", action="store_true", help="overwrite existing files")
    ap.add_argument("--get-ffmpeg", action="store_true", help="auto download ffmpeg into ./bin")
    ap.add_argument("--yes", "-y", action="store_true", help="non-interactive, defaults")
    ap.add_argument("--ask", action="store_true", help="force the quality menu even when piped")
    ap.add_argument("--dry-run", action="store_true", help="print what would be downloaded")
    ap.add_argument("--login", action="store_true", help="login via browser, save cookies.txt")
    ap.add_argument("--force-login", action="store_true", help="ignore saved cookie, login again")
    ap.add_argument("--login-timeout", type=int, default=240, help="seconds to wait for QR login")
    ap.add_argument("--profile", default=DEFAULT_PROFILE_DIR, help="browser profile dir for login")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    # ---- login first ----
    if args.login:
        print("=== Login ===")
        cookie = cmd_login(args)
        args.login_done = True
        if cookie:
            args.cookie = cookie
        else:
            print("login not completed, continuing as guest (PGC max 480P).")
        if not args.inputs:
            return 0

    # ---- wizard when no args ----
    if not args.inputs:
        if not (sys.stdin.isatty() or args.ask):
            ap.print_help()
            return 2
        link = interactive_wizard(args)
        if not link:
            return 0
        args.inputs = [link]
        args.wizard = True
        print()

    # ---- cookie ----
    cookie = args.cookie
    if not cookie and not args.cookie_file and os.path.isfile(DEFAULT_COOKIE_FILE):
        args.cookie_file = DEFAULT_COOKIE_FILE
    if args.cookie_file and os.path.isfile(args.cookie_file):
        cookie = load_cookie_file(args.cookie_file)

    client = HttpClient(cookie, args.proxy)
    client.ensure_buvid()

    if "SESSDATA" in cookie:
        info = verify_login(cookie)
        if info:
            label = info["vip_label"] or ("VIP" if info["vip"] else "normal")
            print(f"account: {info['uname']} ({label}, Lv{info['level']})")
        else:
            print("[!] cookie invalid/expired -> guest mode (PGC max 480P). "
                  "Run --login to refresh.")
    else:
        print("[i] no SESSDATA -> guest mode, PGC max 480P; "
              "login unlocks 1080P, VIP unlocks 4K/HDR.")

    ffmpeg = find_ffmpeg()
    if args.get_ffmpeg and not ffmpeg:
        ffmpeg = install_ffmpeg()
    if not ffmpeg:
        print("[i] ffmpeg not found; streams stay unmerged. "
              "Install: winget install -e --id Gyan.FFmpeg  (or --get-ffmpeg)")

    dl = Downloader(client, threads=args.threads,
                    rate_limit=max(0, int(getattr(args, "limit", 0) or 0)) * 1024)
    if dl.rate_limit:
        print(f"限速: {fmt_size(dl.rate_limit)}/s（整任务共享）")
    interactive = bool(sys.stdin.isatty() or args.ask)
    ask_after = bool(getattr(args, "wizard", False))  # 双击/无参数启动才问「继续/退出」
    cli_quality = args.quality

    while True:
        rc = run_round(args, client, dl, ffmpeg, interactive)
        if rc is None:
            return 0
        if not ask_after or args.list or args.dry_run:
            return rc

        # ---- after a finished task: continue or quit ----
        print("\n接下来：")
        print("  [1] 继续下载（回到输入链接）")
        print("  [2] 退出（回车）")
        again = ask("  请选择 [1/2]: ", "2").lower()
        if again not in ("1", "y", "yes"):
            print("已退出。")
            return rc
        link = ask("  粘贴 ep/ss/md 链接或 BV 号（直接回车 = 退出）: ")
        if not link or link.lower() in ("q", "quit", "exit"):
            print("已退出。")
            return rc

        # next round: forget the previous episode/quality choice, ask again
        args.inputs = [link]
        args.all = False
        args.with_pv = False
        args.eps = None
        args.ep = None
        args.list = False
        args.dry_run = False
        args.quality = cli_quality
        print()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ninterrupted")
        sys.exit(130)

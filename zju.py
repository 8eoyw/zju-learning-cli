#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "img2pdf", "pillow", "keyring", "numpy"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 8eoyw
# Portions ported from PeiPei233/zju-learning-assistant, Copyright (c) 2023 PeiPei233 (MIT).
"""學在浙大 / 智雲課堂 命令列工具。

API 邏輯移植自 PeiPei233/zju-learning-assistant (ZLA) 的 src-tauri/src/zju_assist.rs，
改成可腳本化、可排程、可被 AI agent 直接呼叫的單檔 CLI。

  zju.py login                         # 首次：存學號，密碼進 macOS Keychain
  zju.py courses [--all]               # 學在浙大課程列表
  zju.py sync [課程...] [--dry-run]     # 增量同步課程附件（含排程中的活動）
  zju.py todo                          # 待辦
  zju.py activities [課程...] [--type forum homework ...]  # 所有活動（含測驗）
  zju.py show <活動id> [--read]         # 活動詳情；作業顯示自己的提交狀態；--read 印出附件文字
  zju.py forum list|read|post|reply ... # 討論區
  zju.py upload 檔案...                 # 上傳，印 upload id
  zju.py submit <作業id> --file ... [--body ...] [--draft] [-y]  # 交作業
  zju.py classroom search 關鍵字        # 智雲課堂找課（id 與學在浙大不同）
  zju.py classroom subs <cid>          # 列出每堂課
  zju.py classroom day [日期] [--days N]   # 課表的課＋追蹤中的旁聽課
  zju.py classroom add|rm <cid>...      # 追蹤課表外的課（申請聽課核准的），--days 也會抓
  zju.py classroom tracked             # 列出追蹤中的課
  zju.py ppt --course <cid> | --days N [--dedup]  # 智雲 PPT 截圖合併 PDF
  zju.py transcript --course <cid> | --days N [--format txt|srt|md]
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import ssl

import requests
from requests.adapters import HTTPAdapter

KEYCHAIN_SERVICE = "zju-learning"
STATE_DIR = Path(os.environ.get("ZJU_STATE_DIR") or (Path.home() / ".config" / "zju-learning"))
CONFIG_FILE = STATE_DIR / "config.json"
COOKIE_FILE = STATE_DIR / "cookies.json"
# 這兩台只支援 1024-bit DHE / 靜態 RSA，OpenSSL 3 預設拒絕；降級只套用在它們身上
LEGACY_TLS_HOSTS = ("courses.zju.edu.cn", "identity.zju.edu.cn")
CST = dt.timezone(dt.timedelta(hours=8))  # 學校 API 沒帶時區時視為北京時間
LMS = "https://courses.zju.edu.cn"
DEFAULT_OUT = Path.home() / "ZJU-Courses"  # 可用 config.json 的 "out" 或環境變數 ZJU_OUT 覆寫
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:88.0) Gecko/20100101 Firefox/88.0"
MEDIA_EXT = {".mp4", ".mov", ".avi", ".mkv", ".flv", ".m4v", ".wmv", ".webm", ".mp3", ".m4a", ".wav"}
TIMEOUT = (6, 60)  # connect, read — 排程時別卡在單一 hop 上

COURSE_FIELDS = (
    "id,name,course_code,department(id,name),start_date,end_date,is_started,is_closed,"
    "academic_year_id,semester_id,credit,display_name,instructors(id,name)"
)


class ZjuError(RuntimeError):
    pass


def log(*a):
    print(*a, file=sys.stderr, flush=True)


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_name(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\x00-\x1f]', "_", str(s)).strip(" .")
    s = s[:150].rstrip(" .") or "_"
    if s.split(".")[0].upper() in WIN_RESERVED:  # Windows 保留裝置名
        s = "_" + s
    return s


# ---------------- config / credentials ----------------

def state_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    return STATE_DIR


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except ValueError as e:
        raise ZjuError(f"{CONFIG_FILE} 格式錯誤：{e}")


def save_config(cfg: dict):
    state_dir()
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2))


def keychain_get(user: str) -> str | None:
    if sys.platform != "darwin":
        try:
            import keyring
            return keyring.get_password(KEYCHAIN_SERVICE, user)
        except Exception:
            return None
    r = subprocess.run(
        ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def keychain_set_interactive(user: str):
    if sys.platform != "darwin":  # Windows 憑證管理員 / Linux Secret Service
        import getpass
        import keyring
        try:
            keyring.set_password(KEYCHAIN_SERVICE, user, getpass.getpass("密碼："))
        except Exception as e:
            raise ZjuError(f"系統憑證庫不可用（{e}）；改用環境變數 ZJU_USER / ZJU_PASS")
        return
    # macOS 走系統 security CLI（排程讀取不會跳授權視窗）；-w 放最後 = security 自己在 tty 上問密碼，密碼不進 argv / shell history
    r = subprocess.run(
        ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", user, "-w"]
    )
    if r.returncode != 0:
        raise ZjuError("寫入 Keychain 失敗")


def get_credentials() -> tuple[str, str]:
    user = os.environ.get("ZJU_USER") or load_config().get("username")
    if not user:
        raise ZjuError("尚未設定帳號，先跑：zju.py login")
    pwd = os.environ.get("ZJU_PASS") or keychain_get(user)
    if not pwd:
        raise ZjuError("Keychain 找不到密碼，先跑：zju.py login")
    return user, pwd


# ---------------- client ----------------

class LegacyTLS(HTTPAdapter):
    """只掛在 LEGACY_TLS_HOSTS：它們只給 1024-bit DHE，OpenSSL 3 報 DH_KEY_TOO_SMALL。"""

    def init_poolmanager(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().init_poolmanager(*a, **kw)

    def proxy_manager_for(self, *a, **kw):
        ctx = ssl.create_default_context()
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        kw["ssl_context"] = ctx
        return super().proxy_manager_for(*a, **kw)


class NoCookieHTTP(HTTPAdapter):
    """明文 http:// 一律不帶 cookie / Authorization。
    .zju.edu.cn 的 SSO cookie（iPlanetDirectoryPro 等）沒設 Secure，瀏覽器和 requests
    都會照送給 http 網址 —— 智雲 PPT 圖片就是 http，等於把登入憑證明文送出。"""

    def send(self, request, **kw):
        request.headers.pop("Cookie", None)
        request.headers.pop("Authorization", None)
        return super().send(request, **kw)


class DownloadError(ZjuError):
    def __init__(self, msg: str, codes: list[int]):
        super().__init__(msg)
        self.codes = codes


class TooBig(ZjuError):
    pass


def secure_url(u: str) -> str:
    """學校主機的 http 網址升級成 https（實測 video.cmc 等都支援）。"""
    p = urlparse(u)
    if p.scheme == "http" and (p.hostname or "").endswith(".zju.edu.cn"):
        return "https" + u[4:]
    return u


def refer_params(activity: dict | None) -> dict | None:
    """組 /uploads/{id}/blob 的 reference 參數，與官方網頁前端下載鈕送出的相同：
    classroom→classroom_activity、exam 不帶、其餘→learning_activity；
    伺服器只認 snake_case 參數名。"""
    if not activity or not activity.get("id"):
        return None
    t = activity.get("type")
    if t == "exam":
        return None
    return {"refer_id": activity["id"],
            "refer_type": "classroom_activity" if t == "classroom" else "learning_activity"}


class Zju:
    def __init__(self):
        self.jar = requests.cookies.RequestsCookieJar()  # 各執行緒 session 共用（CookieJar 自帶鎖）
        self._tl = threading.local()
        self.logged_in = False
        self._load_cookies()

    def _load_cookies(self):
        (STATE_DIR / "cookies.pkl").unlink(missing_ok=True)  # 舊版 pickle 快取：不再讀取
        if not COOKIE_FILE.exists():
            return
        try:
            for d in json.loads(COOKIE_FILE.read_text()):
                self.jar.set_cookie(requests.cookies.create_cookie(**d))
        except (ValueError, TypeError, KeyError):
            COOKIE_FILE.unlink(missing_ok=True)  # 壞了就重登

    @property
    def s(self) -> requests.Session:
        """每個執行緒一個 session：連線池不互搶，trust_env 切換也不會互相干擾。"""
        if not hasattr(self._tl, "s"):
            s = requests.Session()
            s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8))
            for h in LEGACY_TLS_HOSTS:
                s.mount(f"https://{h}", LegacyTLS(pool_connections=8, pool_maxsize=8))
            s.mount("http://", NoCookieHTTP())
            s.headers["User-Agent"] = UA
            s.cookies = self.jar
            self._tl.s = s
        return self._tl.s

    def req(self, method: str, url: str, retry: bool | None = None, **kw) -> requests.Response:
        """預設直連，連不上才退環境 proxy。
        重試只給冪等請求（GET 或明確 retry=True）；非冪等只在「確定沒送出」（連線逾時／proxy 錯）時重試，
        免得登入 POST 被重送、觸發 CAS 驗證碼。TLS 錯誤不重試：跟斷線要分得出來。"""
        kw.setdefault("timeout", TIMEOUT)
        idempotent = method in ("GET", "HEAD") if retry is None else retry
        last = None
        for attempt in range(4):
            self.s.trust_env = attempt % 2 == 1
            try:
                r = self.s.request(method, url, **kw)
            except requests.exceptions.SSLError as e:
                raise ZjuError(f"TLS 驗證失敗（網路可能被攔截，或學校憑證有問題）：{url}\n{e}")
            except (requests.exceptions.ConnectTimeout, requests.exceptions.ProxyError) as e:
                last = e
            except (requests.ConnectionError, requests.Timeout) as e:
                if not idempotent:
                    raise ZjuError(f"連線中斷（請求可能已送出，不自動重送）：{url}\n{e}")
                last = e
            else:
                if r.status_code in (429, 503) and attempt < 3:  # 被限流：照 Retry-After 退讓
                    wait = r.headers.get("Retry-After", "")
                    r.close()
                    time.sleep(min(int(wait), 60) if wait.isdigit() else 2 * 2 ** attempt)
                    continue
                return r
            time.sleep(0.3 * 2 ** attempt)
        raise ZjuError(f"連線失敗：{url}\n{last}")

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, **kw):
        return self.req("POST", url, **kw)

    def save_cookies(self):
        """JSON 不是 pickle：快取檔被別人改了也只是讀到壞 cookie，不會執行程式碼。"""
        state_dir()
        data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
                 "secure": c.secure, "expires": c.expires, "rest": c._rest} for c in self.jar]
        tmp = COOKIE_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, COOKIE_FILE)
        os.chmod(COOKIE_FILE, 0o600)

    # ---- auth ----

    def login(self, user: str, pwd: str):
        self.s.cookies.clear()
        text = self.get("https://zjuam.zju.edu.cn/cas/login").text
        m = re.search(r'name="execution" value="(.*?)"', text)
        if not m:
            raise ZjuError("CAS 頁面找不到 execution 欄位（登入頁改版？）")
        key = self.get("https://zjuam.zju.edu.cn/cas/v2/getPubKey").json()
        n, e = int(key["modulus"], 16), int(key["exponent"], 16)
        enc = format(pow(int.from_bytes(pwd.encode(), "big"), e, n), "x")
        if len(enc) % 2:
            enc = "0" + enc
        r = self.post("https://zjuam.zju.edu.cn/cas/login", data={
            "username": user, "password": enc, "execution": m.group(1),
            "_eventId": "submit", "authcode": "",
        })
        if "统一身份认证平台" in r.text:
            raise ZjuError("登入失敗：學號或密碼錯誤（或需要驗證碼，先在瀏覽器登入一次）")
        # 讓各子系統吃到 SSO
        self.get("https://courses.zju.edu.cn/user/courses")
        try:
            self.get("https://tgmedia.cmc.zju.edu.cn/index.php?r=auth/login&auType=cmc&tenant_code=112"
                     "&forward=https%3A%2F%2Fclassroom.zju.edu.cn%2F")
        except ZjuError as e:
            log(f"警告：智雲 SSO 連不上（只影響 classroom/ppt/transcript）：{str(e).splitlines()[0]}")
        self.logged_in = True
        self.save_cookies()

    def courses_alive(self) -> bool:
        try:
            r = self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true", allow_redirects=False)
            return r.status_code == 200 and "todo_list" in r.json()
        except (ValueError, ZjuError):
            return False

    def ensure(self, need_classroom: bool = False):
        if self.logged_in:
            return
        if self.courses_alive() and (not need_classroom or self._token(silent=True)):
            self.logged_in = True
            return
        self.login(*get_credentials())

    def _token(self, silent=False) -> str | None:
        for c in self.s.cookies:
            if (c.domain or "").lstrip(".") not in ("classroom.zju.edu.cn", "zju.edu.cn"):
                continue
            m = re.search(r'\{i:\d+;s:\d+:"_token";i:\d+;s:\d+:"(.+?)";\}', unquote(c.value or ""))
            if m:
                return m.group(1)
        if silent:
            return None
        raise ZjuError("智雲課堂 token 解析失敗：classroom cookie 格式可能改了，檢查 _token 正則")

    def bearer(self) -> dict:
        return {"Authorization": f"Bearer {self._token()}"}

    def json(self, r: requests.Response, what: str):
        try:
            return r.json()
        except ValueError:
            raise ZjuError(f"{what}：回應不是 JSON（HTTP {r.status_code}），session 可能失效，重跑即可")

    # ---- 學在浙大 ----

    def courses(self) -> list[dict]:
        self.ensure()
        out, page = [], 1
        while True:
            j = self.json(self.post("https://courses.zju.edu.cn/api/my-courses", retry=True, json={
                "fields": COURSE_FIELDS, "page": page, "page_size": 100,
                "conditions": {"status": ["ongoing", "notStarted", "closed"], "keyword": "",
                               "classify_type": "recently_started", "display_studio_list": False},
                "showScorePassedStatus": False,
            }), "my-courses")
            out += j.get("courses", [])
            if page >= j.get("pages", 1):
                return out
            page += 1

    def semesters(self) -> dict[int, str]:
        self.ensure()
        j = self.json(self.get("https://courses.zju.edu.cn/api/my-semesters?"), "semesters")
        return {x["id"]: x.get("name") or x.get("real_name") or str(x["id"]) for x in j.get("semesters", [])}

    def uploads(self, course_id: int) -> list[tuple[dict, dict]]:
        """回傳 (活動, upload) — 含一般活動與作業附件。"""
        res = []
        j = self.json(self.get(f"https://courses.zju.edu.cn/api/courses/{course_id}/activities"), "activities")
        for a in j.get("activities", []):
            for u in a.get("uploads") or []:
                res.append((a, u))
        page = 1
        while True:
            j = self.json(self.get(
                f"https://courses.zju.edu.cn/api/courses/{course_id}/homework-activities",
                params={"conditions": '{"itemsSortBy":{"predicate":"module","reverse":false}}',
                        "page": page, "page_size": 20, "reloadPage": "false"}), "homework")
            for h in j.get("homework_activities", []):
                for u in h.get("uploads") or []:
                    res.append((h, u))
            if page >= (j.get("pages") or 1):
                return res
            page += 1

    def upload_response(self, uid: int, rid: int, activity: dict | None = None) -> tuple[requests.Response, str]:
        """附件下載來源，依優先序嘗試、採用第一個能回檔的，回 (response, 來源)：
        1. reference blob — 常規下載
        2. upload blob — 原始檔
        3. upload blob + reference 參數 — 參數與官方網頁前端相同，部分活動的附件由此提供
        4. 預覽器的轉檔 PDF — document/{rid}/url?preview=true 回 {url}
        全部來源都不可用時丟 DownloadError（codes 為各來源的 HTTP 碼）。"""
        base = "https://courses.zju.edu.cn/api/uploads"
        sources = [
            (f"{base}/reference/{rid}/blob", None, "下載"),
            (f"{base}/{uid}/blob", None, "原檔"),
        ]
        refer = refer_params(activity)
        if refer:
            sources.append((f"{base}/{uid}/blob", refer, "排程原檔"))
        codes = []
        for url, params, src in sources:
            r = self.get(url, params=params, stream=True)
            if r.ok:
                return r, src
            codes.append(r.status_code)
            r.close()
        r = self.get(f"{base}/reference/document/{rid}/url", params={"preview": "true"})
        codes.append(r.status_code)
        if r.ok:
            try:
                url = r.json().get("url")
            except ValueError:
                url = None
            if url:
                r = self.get(secure_url(urljoin(base, url)), stream=True)
                if r.ok:
                    return r, "預覽PDF"
                codes.append(r.status_code)
                r.close()
        raise DownloadError(f"下載失敗 HTTP {'/'.join(map(str, codes))}", codes)

    def todos(self) -> list[dict]:
        self.ensure()
        return self.json(self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true"), "todos").get("todo_list", [])

    # ---- 活動 / 討論 / 作業提交（端點取自官方前端 JS）----

    def user_id(self) -> int:
        if not hasattr(self, "_uid"):
            self.ensure()
            m = re.search(r'ng-init="userId=(\d+);', self.get(f"{LMS}/user/index").text)
            if not m:
                raise ZjuError("抓不到自己的 user id（/user/index 改版？）")
            self._uid = int(m.group(1))
        return self._uid

    def activities(self, course_id: int) -> list[dict]:
        """課程所有活動（課件、影片、作業、討論、網頁、連結…）加上測驗。"""
        self.ensure()
        acts = self.json(self.get(f"{LMS}/api/courses/{course_id}/activities"), "activities").get("activities", [])
        r = self.get(f"{LMS}/api/courses/{course_id}/exams")
        if r.ok:
            acts += [dict(e, type="exam") for e in self.json(r, "exams").get("exams", [])]
        return acts

    def activity(self, aid: int) -> dict:
        self.ensure()
        r = self.get(f"{LMS}/api/activities/{aid}")
        if r.status_code == 404:
            raise ZjuError(f"找不到活動 {aid}（測驗請用課程的 activities 看）")
        return self.json(r, "activity")

    def forum_category(self, aid: int) -> int:
        """討論活動 id → 討論區分類 id（發帖、列帖都用分類 id）。"""
        cid = self.activity(aid)["course_id"]
        j = self.json(self.get(f"{LMS}/api/courses/{cid}/topic-categories"), "topic-categories")
        for cat in j.get("topic_categories", []):
            if cat.get("activity_id") == aid:
                return cat["id"]
        raise ZjuError(f"活動 {aid} 不是討論（或沒有討論區分類）")

    def topics(self, category_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            j = self.json(self.get(f"{LMS}/api/forum/categories/{category_id}",
                                   params={"page": page, "page_size": 50}), "forum")["result"]
            out += j.get("topics", [])
            if page >= (j.get("pages") or 1):
                return out
            page += 1

    def topic(self, tid: int) -> dict:
        self.ensure()
        return self.json(self.get(f"{LMS}/api/topics/{tid}"), "topic")

    def upload_file(self, path: Path) -> dict:
        """兩段式：先登記取得 upload_url，再依 storage_type 送檔（學校目前是本地儲存 multipart PUT）。"""
        self.ensure()
        pre = self.json(self.post(f"{LMS}/api/uploads", json={
            "name": path.name, "size": path.stat().st_size, "parent_type": None, "parent_id": 0,
            "is_scorm": False, "is_wmpkg": False, "source": "", "is_marked_attachment": False,
            "embed_material_type": "",
        }), "uploads")
        if "upload_url" not in pre:
            raise ZjuError(f"上傳登記失敗：{pre}")
        if pre.get("storage_type") in ("S3", "QINIU"):
            raise ZjuError(f"儲存後端 {pre['storage_type']} 尚未支援（學校改了上傳方式）")
        with path.open("rb") as f:
            r = self.req("PUT", pre["upload_url"], files={"file": (path.name, f)}, retry=False,
                         timeout=(6, 600))
        if not r.ok:
            raise ZjuError(f"上傳 {path.name} 失敗 HTTP {r.status_code}：{r.text[:200]}")
        return pre

    def create_topic(self, category_id: int, title: str, content: str, uploads: list[int]) -> dict:
        r = self.post(f"{LMS}/api/topics", json={"title": title, "content": content,
                                                  "category_id": category_id, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"發帖失敗 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "topic")

    def reply_topic(self, tid: int, content: str, uploads: list[int]) -> dict:
        self.ensure()
        r = self.post(f"{LMS}/api/topics/{tid}/replies", json={"content": content, "uploads": uploads})
        if not r.ok:
            raise ZjuError(f"回帖失敗 HTTP {r.status_code}：{r.text[:200]}")
        return self.json(r, "reply")

    def my_submission(self, aid: int) -> dict:
        return self.json(self.get(f"{LMS}/api/course/activities/{aid}/students/{self.user_id()}/submission"),
                         "submission")

    def submit(self, aid: int, comment: str, uploads: list[int], draft: bool, mode: str,
               draft_id: int | None) -> dict:
        """與網頁「提交」相同的 payload；已有草稿時用 PUT 蓋掉草稿。"""
        body = {"comment": comment, "uploads": uploads, "slides": [], "is_draft": draft, "mode": mode,
                "other_resources": [], "uploads_in_rich_text": []}
        method = "POST"
        if draft_id:
            method, body["submission_id"] = "PUT", draft_id
        r = self.req(method, f"{LMS}/api/course/activities/{aid}/submissions", json=body)
        if not r.ok:
            raise ZjuError(f"提交失敗 HTTP {r.status_code}：{r.text[:300]}")
        return self.json(r, "submission")

    # ---- 智雲課堂 ----

    def infosimple(self) -> dict:
        self.ensure(need_classroom=True)
        return self.json(self.get("https://classroom.zju.edu.cn/userapi/v1/infosimple",
                                  headers=self.bearer()), "infosimple")["params"]

    def classroom_search(self, title: str, teacher: str = "") -> list[dict]:
        info = self.infosimple()
        out, page = [], 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/searchlist", headers=self.bearer(), params={
                "tenant_id": 112, "user_id": info["id"], "user_name": info["account"], "page": page,
                "per_page": 16, "title": title, "realname": teacher, "trans": "", "tenant_code": 112,
                "randomKey": random.random()}), "searchlist")
            if j.get("code") != 0:
                raise ZjuError(j.get("msg", "searchlist 失敗"))
            lst = j["total"]["list"]
            out += lst
            if not lst or len(out) >= int(j["total"]["total"]):
                return out
            page += 1

    def course_subs(self, course_id: int) -> list[dict]:
        info = self.infosimple()
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/multi-search/get-course-detail",
                               headers=self.bearer(),
                               params={"course_id": course_id, "student": info["account"]}), "course-detail")
        data = j["data"]
        subs = []
        for year in (data.get("sub_list") or {}).values():
            for month in year.values():
                for week in month.values():
                    for s in week:
                        begin = int(s.get("class_begin") or 0)
                        subs.append({"course_id": course_id, "course_name": data["title"],
                                     "sub_id": int(s["id"]), "sub_name": s["sub_title"],
                                     "lecturer": s.get("lecturer_name", ""),
                                     "day": dt.date.fromtimestamp(begin) if begin else None})
        subs.sort(key=lambda s: s["sub_name"])
        return subs

    def day_subs(self, day: dt.date) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://classroom.zju.edu.cn/courseapi/v2/course-live/get-my-course-day",
                               headers=self.bearer(), params={"day": day.isoformat()}), "course-day")
        subs = []
        lst = j.get("list")
        for d in (lst.values() if isinstance(lst, dict) else lst or []):
            for c in d.get("course", []):
                subs.append({"course_id": int(c["id"]), "course_name": c["title"], "sub_id": int(c["sub_id"]),
                             "sub_name": c["sub_title"], "lecturer": c.get("realname", "")})
        return subs

    def ppt_urls(self, course_id: int, sub_id: int) -> list[str]:
        """智雲 PPT 截圖。API 不守 per_page：常一頁就回全部、下一頁再重複一次
        （ZLA 假設每頁 ≤100 會在 >100 張時卡死重試）→ 按序去重，湊滿 total 或遇到沒新東西就停。"""
        self.ensure(need_classroom=True)
        urls: list[str] = []
        seen: set[str] = set()
        page = 1
        while True:
            j = self.json(self.get("https://classroom.zju.edu.cn/pptnote/v1/schedule/search-ppt", params={
                "course_id": course_id, "sub_id": sub_id, "page": page, "per_page": 100},
                headers=self.bearer()), "search-ppt")
            total = int(j.get("total") or 0)
            added = 0
            for p in j.get("list") or []:
                u = json.loads(p["content"]).get("pptimgurl")
                if u and u not in seen:
                    seen.add(u)
                    urls.append(u)
                    added += 1
            if len(urls) >= total or added == 0 or page >= 50:
                if len(urls) < total:
                    log(f"[注意] PPT 只拿到 {len(urls)}/{total} 張 course={course_id} sub={sub_id}")
                return urls
            page += 1

    def subtitle(self, sub_id: int) -> list[dict]:
        self.ensure(need_classroom=True)
        j = self.json(self.get("https://yjapi.cmc.zju.edu.cn/courseapi/v3/web-socket/search-trans-result",
                               params={"sub_id": sub_id, "format": "json"}), "trans-result")
        if j.get("code") == 10002:  # 未查询到语音数据：當天課程通常還沒轉完
            return []
        if j.get("code") != 0:
            raise ZjuError(f"取轉錄失敗 code={j.get('code')} {j.get('msg', '')}")
        lst = j.get("list") or []
        return lst[0].get("all_content", []) if lst else []


# ---------------- helpers ----------------

class Manifest:
    """out/.zju_manifest.json：記錄 upload id → 本地路徑，換版（新 id）就重抓。"""

    def __init__(self, root: Path):
        self.path = root / ".zju_manifest.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key):
        return self.data.get(key)

    def put(self, key, val):
        self.data[key] = val
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
        os.replace(tmp, self.path)


def stream_to(r: requests.Response, dest: Path, limit: int | None = None) -> Path:
    """寫暫存檔再 rename；驗證長度、拒收空檔和錯誤頁，免得壞檔被記進 manifest 後永遠不再重抓。"""
    expected = r.headers.get("Content-Length")
    expected = int(expected) if expected and expected.isdigit() and not r.headers.get("Content-Encoding") else None
    if limit and expected and expected > limit:
        r.close()
        raise TooBig(f"{expected / 2**20:.0f}MB")
    if "text/html" in r.headers.get("Content-Type", "") and dest.suffix.lower() not in (".html", ".htm"):
        r.close()
        raise ZjuError("伺服器回傳 HTML（錯誤頁或登入頁），不存檔")
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        written = 0
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
                written += len(chunk)
                if limit and written > limit:
                    raise TooBig(f">{limit / 2**20:.0f}MB")
        if written == 0:
            raise ZjuError("伺服器回傳空檔")
        if expected is not None and written != expected:
            raise ZjuError(f"下載不完整：{written}/{expected} bytes")
        with open(tmp, "rb") as f:
            head = f.read(5)
        # preview 版常是 PDF，但檔名還是 .pptx/.docx — 補副檔名免得打不開
        if head == b"%PDF-" and dest.suffix.lower() != ".pdf":
            dest = dest.with_name(dest.name + ".pdf")
        os.replace(tmp, dest)
        return dest
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    finally:
        r.close()


def current_year(courses: list[dict]) -> list[dict]:
    """is_closed 學校常不關，靠 academic_year_id 取最新學年。"""
    latest = max((c.get("academic_year_id") or 0 for c in courses), default=0)
    return [c for c in courses if (c.get("academic_year_id") or 0) == latest and not c.get("is_closed")]


def local_time(iso: str | None, fmt: str = "%m-%d %H:%M") -> str:
    if not iso:
        return "時間未定"
    try:
        d = dt.datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    if d.tzinfo is None:
        d = d.replace(tzinfo=CST)
    return d.astimezone().strftime(fmt)


def match_courses(courses: list[dict], keys: list[str], include_all: bool) -> list[dict]:
    if not keys:
        return courses if include_all else current_year(courses)
    out = []
    for c in courses:
        for k in keys:
            if str(c["id"]) == k or k.lower() in c["name"].lower():
                out.append(c)
                break
    return out


ACT_TYPES = {
    "material": "課件", "online_video": "影片", "homework": "作業", "forum": "討論", "exam": "測驗",
    "page": "網頁", "web_link": "連結", "questionnaire": "問卷", "classroom": "課堂互動",
    "lesson": "直播", "vocabulary": "單字", "survey": "調查", "chatroom": "聊天室",
}


def html_to_text(s: str | None) -> str:
    def link(m):  # 連結文字 ≠ 網址時兩個都留；網址前後補空白，免得黏在中文上
        href, text = html.unescape(m.group(1)), re.sub(r"<[^>]+>", "", m.group(2)).strip()
        return f" {href} " if not text or html.unescape(text) == href else f"{text} ({href})"
    s = re.sub(r"(?is)<a\b[^>]*?href=\"([^\"]+)\"[^>]*>(.*?)</a>", link, s or "")
    s = re.sub(r"(?i)<img\b[^>]*?src=\"([^\"]+)\"[^>]*>", lambda m: f"[圖 {urljoin(LMS, html.unescape(m.group(1)))}]", s)
    s = re.sub(r"(?i)<li\b[^>]*>", "- ", s)
    s = re.sub(r"(?i)</t[dh]>", "\t", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h\d>", "\n", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    s = re.sub(r"[ \t]+\n", "\n", re.sub(r"(?m)^- +", "- ", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


def doc_to_text(data: bytes) -> str | None:
    """附件位元組 → 純文字；依檔頭判格式（預覽來源可能把 docx 換成 PDF）。不支援回 None。"""
    if data[:4] == b"PK\x03\x04":
        import zipfile, io
        z = zipfile.ZipFile(io.BytesIO(data))
        names = z.namelist()
        if "word/document.xml" in names:
            parts = ["word/document.xml"]
            para, tag = r"</w:p>", r"<(?:w|m):t[^>]*>([^<]*)</(?:w|m):t>"  # m:t = 公式裡的字
        else:
            parts = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                           key=lambda n: int(re.search(r"\d+", n.rsplit("/", 1)[1]).group()))
            para, tag = r"</a:p>", r"<a:t>([^<]*)</a:t>"
        if not parts:
            return None
        out = []
        for i, n in enumerate(parts):
            xml = z.read(n).decode("utf-8", "replace")
            lines = ["".join(re.findall(tag, p_)) + " [圖]" * len(re.findall(r"<w:drawing>|<w:pict>", p_))
                     for p_ in re.split(para, xml)]  # 公式常是貼圖，標出來免得以為漏字
            txt = "\n".join(l for l in lines if l.strip())
            out.append(f"--- 第 {i + 1} 頁 ---\n{txt}" if len(parts) > 1 else txt)
        return html.unescape("\n".join(out)).strip()
    tool = None
    if data[:5] == b"%PDF-" and shutil.which("pdftotext"):
        tool = ["pdftotext", "-layout", "{f}", "-"]
    elif data[:4] == b"\xd0\xcf\x11\xe0" and shutil.which("textutil"):  # 舊版 .doc，macOS 內建
        tool = ["textutil", "-convert", "txt", "-stdout", "{f}"]
    elif data[:5] == b"%PDF-" or data[:4] == b"\xd0\xcf\x11\xe0":
        return None
    else:
        try:
            return data.decode("utf-8").strip()
        except UnicodeDecodeError:
            return None
    with tempfile.NamedTemporaryFile(suffix=".bin") as f:
        f.write(data)
        f.flush()
        r = subprocess.run([f.name if x == "{f}" else x for x in tool], capture_output=True)
    return r.stdout.decode("utf-8", "replace").strip() if r.returncode == 0 else None


def text_to_html(s: str) -> str:
    """純文字 → 段落 HTML（空行分段、單換行 <br>），網頁編輯器存的也是這種格式。"""
    paras = [p for p in re.split(r"\n\s*\n", s.strip()) if p.strip()]
    return "".join("<p>" + html.escape(p.strip()).replace("\n", "<br>") + "</p>" for p in paras)


def act_status(a: dict) -> str:
    if a.get("is_closed"):
        return "已關閉"
    if a.get("is_started") is False:
        return "未開始"
    end = a.get("end_time")
    if end and dt.datetime.fromisoformat(end.replace("Z", "+00:00")) < dt.datetime.now(dt.timezone.utc):
        return "已截止"
    return "進行中"


def read_body(a) -> str:
    """--body 文字或 --body-file 檔案（- = stdin）。"""
    if a.body_file:
        return sys.stdin.read() if a.body_file == "-" else Path(a.body_file).read_text(encoding="utf-8")
    return a.body or ""


def upload_all(z: "Zju", files: list[str] | None) -> list[int]:
    ids = []
    for f in files or []:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到檔案：{p}")
        u = z.upload_file(p)
        log(f"[上傳] {p.name} → upload {u['id']}")
        ids.append(u["id"])
    return ids


def fmt_ts(sec: float, srt=True) -> str:
    sec = float(sec)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    ms = int((sec - int(sec)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}" if srt else f"{h:02d}:{m:02d}:{s:02d}"


def render_transcript(items: list[dict], fmt: str, title: str) -> str:
    lines = []
    if fmt == "srt":
        for i, c in enumerate(items, 1):
            lines += [str(i), f"{fmt_ts(c.get('BeginSec', 0))} --> {fmt_ts(c.get('EndSec', 0))}", c.get("Text", ""), ""]
    elif fmt == "md":
        lines.append(f"# {title}\n")
        for c in items:
            lines.append(f"**[{fmt_ts(c.get('BeginSec', 0), False)}]** {c.get('Text', '')}  ")
    else:
        for c in items:
            lines.append(f"[{fmt_ts(c.get('BeginSec', 0), False)}] {c.get('Text', '')}")
    return "\n".join(lines) + "\n"


def images_to_pdf(paths: list[Path], pdf: Path):
    import img2pdf
    from PIL import Image

    tmp = pdf.with_name(".part-" + pdf.name)

    def write(srcs):
        with open(tmp, "wb") as f:  # 直接串流進檔案，不在記憶體組整份 PDF
            img2pdf.convert([str(x) for x in srcs], outputstream=f)

    try:
        try:
            write(paths)  # 智雲截圖幾乎都是 JPEG：直接嵌入，不重新編碼
        except Exception:
            fixed = []  # 有 alpha / 特殊格式的才轉 JPEG
            for p in paths:
                with Image.open(p) as im:
                    if im.format == "JPEG" and im.mode in ("RGB", "L", "CMYK"):
                        fixed.append(p)
                        continue
                    q = p.with_suffix(".conv.jpg")
                    im.convert("RGB").save(q, quality=92)
                    fixed.append(q)
            write(fixed)
        os.replace(tmp, pdf)
    finally:
        tmp.unlink(missing_ok=True)


def dedup_slides(paths: list[Path], max_lost_cells: int = 2) -> list[Path]:
    """智雲截圖去重。智雲是對投影畫面定時截圖，同一頁會因動畫逐步出現、老師邊講邊寫、
    翻回前面而被截很多次。規則只有一條：一頁的筆畫若全都還在後面那頁（或之前留下的某頁）裡，
    它就是多餘的——所以連續的一串只留最後、最完整的一張，註記不會丟。

    「筆畫」= 跟 15×15 鄰域中位數差很多的像素，大片純色（白底、黑底、影片畫面）不算；
    八成以上的頁都有的（底圖紋理、黑邊、頁腳）也不算。「全都還在」= 消失的筆畫沒有聚成塊：
    有 4 個以上筆畫像素消失的 8×8 格不超過 max_lost_cells 個（JPEG 雜訊零星，真的少了東西會成塊；
    再高就抓不到影片裡又細又淡的線）。
    在三堂課（白底英文、底圖＋手寫、黑底教學影片混檔案總管）逐頁核對過，沒有誤刪。
    """
    import numpy as np
    from PIL import Image, ImageFilter

    T, W, H = 40, 512, 288  # 灰階門檻；解析度再低，細的手寫筆跡就糊掉看不見了
    if len(paths) < 2:
        return list(paths)

    def prep(p):
        with Image.open(p) as im:
            g = im.convert("L").resize((W, H), Image.BOX)
        f = np.asarray(g, dtype=np.int16)
        return f, strokes(f)

    def strokes(f):
        bg = Image.fromarray(f.astype(np.uint8)).filter(ImageFilter.MedianFilter(15))
        return np.abs(f - np.asarray(bg, dtype=np.int16)) > T

    frames, raw = zip(*map(prep, paths))
    frames = np.stack(frames)
    med = np.median(frames, axis=0).astype(np.int16)
    # 版面 = 八成以上的頁在那裡都一樣的筆畫；只看中位數的話，一張講了半堂課的投影片會被當成版面
    layout = strokes(med) & ((np.abs(frames - med) <= T).sum(axis=0) >= 0.8 * len(frames))
    masks = [m & ~(layout & (np.abs(f - med) <= T)) for f, m in zip(frames, raw)]

    def lost(i, js):  # i 的筆畫在 js 各頁消失成塊的格數
        gone = masks[i] & (np.abs(frames[i] - frames[js]) > T)
        return (gone.reshape(len(js), H // 8, 8, W // 8, 8).sum(axis=(2, 4)) >= 4).sum(axis=(1, 2))

    keep: list[int] = []
    for j in range(len(frames)):
        if frames[j].std() < 3:  # 全黑 / 全白過場
            continue
        if keep and lost(keep[-1], [j])[0] <= max_lost_cells:
            keep[-1] = j  # 前一張是這張的子集（動畫沒跑完、還沒寫完）→ 換成較完整的這張
            continue
        # 翻回講過的頁、擦掉註記的乾淨版；近乎空白的頁什麼都「包含得住」，不拿來比
        if keep and masks[j].sum() >= 400 and (lost(j, keep) <= max_lost_cells).any():
            continue
        keep.append(j)
    return [paths[k] for k in keep] or list(paths)


def tracked_courses() -> dict[str, str]:
    """config.json 的 tracked_courses：{智雲 course_id: 顯示名}。
    申請聽課核准的課不在 get-my-course-day（只回課表），智雲也沒有「我的申請」清單 API，只能自己記。"""
    return load_config().get("tracked_courses") or {}


def range_subs(z: Zju, end: dt.date, days: int) -> list[tuple[dt.date, dict]]:
    """end 往前 days 天：課表的課＋追蹤課程當天的堂次，依 sub_id 去重。"""
    out, seen = [], set()
    for i in range(days):
        d = end - dt.timedelta(days=i)
        for s in z.day_subs(d):
            if s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                out.append((d, s))
    start = end - dt.timedelta(days=days - 1)
    for cid, name in tracked_courses().items():
        try:
            subs = z.course_subs(int(cid))
        except Exception as e:  # 一門壞掉不拖垮課表的課
            log(f"[失敗] 追蹤課程 {cid} {name}: {e}")
            continue
        for s in subs:
            if s["day"] and start <= s["day"] <= end and s["sub_id"] not in seen:
                seen.add(s["sub_id"])
                # 旁聽課常和自己那班同名同節次（別的老師開的同一門），不改名會寫進同一個資料夾而被當成已存在略過
                out.append((s["day"], {**s, "course_name": name}))
    out.sort(key=lambda x: x[0], reverse=True)
    return out


def resolve_subs(z: Zju, a) -> list[dict]:
    if a.course:
        subs = z.course_subs(a.course)
        name = tracked_courses().get(str(a.course))
        if name:
            subs = [{**s, "course_name": name} for s in subs]
        if a.sub:
            subs = [s for s in subs if s["sub_id"] in a.sub]
        return subs
    return [s for _, s in range_subs(z, dt.date.today(), a.days or 1)]


# ---------------- commands ----------------

def cmd_login(a):
    cfg = load_config()
    user = a.username or input(f"學號 [{cfg.get('username', '')}]: ").strip() or cfg.get("username")
    if not user:
        raise ZjuError("沒有學號")
    if not keychain_get(user) or a.reset:
        print("輸入統一身份認證密碼（存進系統憑證庫）：")
        keychain_set_interactive(user)
    cfg["username"] = user
    save_config(cfg)
    z = Zju()
    z.login(*get_credentials())
    ok = z._token(silent=True) is not None
    print(f"登入成功：{user}；智雲課堂 token {'OK' if ok else '缺（classroom 指令可能失敗）'}")


def cmd_courses(a):
    z = Zju()
    cs = z.courses()
    if a.json:
        print(json.dumps(cs, ensure_ascii=False, indent=1))
        return
    sem = z.semesters()
    for c in (cs if a.all else current_year(cs)):
        teachers = ",".join(i["name"] for i in c.get("instructors") or [])
        print(f"{c['id']}\t{sem.get(c.get('semester_id'), '-')}\t{c['name']}\t{teachers}")


def cmd_sync(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("沒有符合的課程（用 courses --all 看 id）")
    root = Path(a.out).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    man = Manifest(root)
    new = skipped = failed = media = total = 0
    big: list[str] = []
    jobs: list[tuple] = []
    pending: list[str] = []
    dup = {n for n in (c["name"] for c in courses) if [x["name"] for x in courses].count(n) > 1}
    for c in courses:
        # 同名課程（不同班）分開放，免得同名檔互蓋
        cdir = root / safe_name(f"{c['name']} ({c['id']})" if c["name"] in dup else c["name"])
        items = z.uploads(c["id"])
        log(f"== {c['name']}（{len(items)} 個檔）")
        # 同名不同檔：全部加 id，命名不依 API 回傳順序（順序變了也不會重抓、不會互換）
        uids_by_name: dict[str, set] = {}
        for _, u in items:
            uids_by_name.setdefault(safe_name(u.get("name") or str(u["id"])), set()).add(u["id"])
        seen_keys: set[str] = set()
        for a_, u in items:
            act = a_.get("title", "")
            uid, rid = u["id"], u.get("reference_id") or u["id"]
            key = f"{c['id']}:{uid}"
            if key in seen_keys:  # 同一檔同時掛在活動和作業
                continue
            seen_keys.add(key)
            name = safe_name(u.get("name") or str(uid))
            if len(uids_by_name[name]) > 1:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem} ({uid}).{ext}" if dot else f"{name} ({uid})"
            rec = man.get(key)
            if rec and (root / rec["path"]).exists():
                skipped += 1
                continue
            size = u.get("size") or 0
            if not a.videos and Path(name).suffix.lower() in MEDIA_EXT:
                media += 1
                continue
            if a.max_size and size > a.max_size * 2**20:
                big.append(f"{c['name']}/{name}  {size / 2**20:.0f}MB")
                continue
            if a.dry_run:
                print(f"[會下載] {c['name']}/{name}  {size / 2**20:.1f}MB  ({act})")
                new += 1
                total += size
                continue
            jobs.append((key, uid, rid, cdir / name, a_))

    limit = a.max_size * 2**20 if a.max_size else None

    def fetch(job):
        key, uid, rid, dest, a_ = job
        r, src = z.upload_response(uid, rid, a_)
        return stream_to(r, dest, limit), src  # API 沒給 size 的檔，靠 Content-Length 把關

    # 多檔並行：單條連線常被伺服器限速，並行吃滿頻寬；manifest 只在主執行緒寫
    with ThreadPoolExecutor(max_workers=max(1, a.jobs)) as pool:
        futs = {pool.submit(fetch, j): j for j in jobs}
        for f in as_completed(futs):
            key, uid, rid, dest0, a_ = futs[f]
            act = a_.get("title", "")
            try:
                dest, src = f.result()
                man.put(key, {"path": str(dest.relative_to(root)), "rid": rid, "source": src,
                              "activity": act, "at": dt.datetime.now().isoformat(timespec="seconds")})
                new += 1
                print(f"[{src}] {dest.relative_to(root)}", flush=True)
            except TooBig as e:
                big.append(f"{dest0.parent.name}/{dest0.name}  {e}")
            except Exception as e:
                if a_.get("is_started") is False and 403 in getattr(e, "codes", ()):
                    # 排程未開放且所有來源皆回 403：開放後下次 sync 自動抓
                    pending.append(f"{dest0.parent.name}/{dest0.name}（{local_time(a_.get('start_time'))} 開放）")
                    continue
                failed += 1
                log(f"[失敗] {dest0.name}: {e}")
    for p_ in pending:
        log(f"[未開放] {p_}")
    for b in big:
        log(f"[太大跳過] {b}")
    if big:
        log(f"  → {len(big)} 個檔超過 {a.max_size}MB，要抓就指定課程加 --max-size 0")
    extra = f"、未開放 {len(pending)}" if pending else ""
    extra += f"、影音跳過 {media}（加 --videos 才抓）" if media else ""
    size_s = f"（約 {total / 2**20:.0f}MB）" if a.dry_run else ""
    log(f"{'預覽' if a.dry_run else '完成'}：{'待下載' if a.dry_run else '新增'} {new}{size_s}、已有 {skipped}、失敗 {failed}{extra} → {root}")
    if failed:
        sys.exit(2)


def cmd_todo(a):
    z = Zju()
    ts = z.todos()
    if a.json:
        print(json.dumps(ts, ensure_ascii=False, indent=1))
        return
    for t in sorted(ts, key=lambda t: t.get("end_time") or ""):
        end = local_time(t.get("end_time"), "%Y-%m-%d %H:%M")  # API 給 UTC
        print(f"{end}\t{t.get('course_name', '')}\t{t.get('title', '')}\t{t.get('type', '')}")


def cmd_activities(a):
    z = Zju()
    courses = match_courses(z.courses(), a.course, a.all)
    if not courses:
        raise ZjuError("沒有符合的課程（用 courses --all 看 id）")
    rows = []
    for c in courses:
        for x in z.activities(c["id"]):
            if a.type and x.get("type") not in a.type:
                continue
            rows.append((c, x))
    if a.json:
        print(json.dumps([dict(x, course_name=c["name"]) for c, x in rows], ensure_ascii=False, indent=1))
        return
    for c, x in rows:
        t = x.get("type", "")
        end = local_time(x.get("end_time"), "%Y-%m-%d %H:%M") if x.get("end_time") else "-"
        print(f"{x['id']}\t{c['name']}\t{ACT_TYPES.get(t, t)}\t{act_status(x)}\t{end}\t{x.get('title', '')}")


def cmd_show(a):
    z = Zju()
    x = z.activity(a.activity)
    t = x.get("type", "")
    d = x.get("data") or {}
    print(f"[{ACT_TYPES.get(t, t)}] {x.get('title')}  (id {x['id']}, 課程 {x.get('course_id')})")
    print(f"狀態：{act_status(x)}　開始 {local_time(x.get('start_time'), '%Y-%m-%d %H:%M')}"
          f"　截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M') if x.get('end_time') else '無'}")
    if x.get("completion_criterion"):
        print(f"完成條件：{x['completion_criterion']}")
    desc = html_to_text(d.get("description") or x.get("description"))
    if desc:
        print(f"\n{desc}\n")
    for u in x.get("uploads") or []:
        print(f"附件：{u.get('name')}  (upload {u.get('id')})")
        if a.read:
            try:
                r, _ = z.upload_response(u["id"], u.get("reference_id") or u["id"], x)
                txt = doc_to_text(r.content)
            except ZjuError as e:
                txt = f"（{e}）"
            print(f"\n{txt or '（這種格式讀不出文字，用 sync 下載後自己開）'}\n")
    if x.get("uploads") and not a.read and not desc:
        print("（說明在附件裡：加 --read 直接印出附件內容）")
    if t == "web_link" and d.get("link"):
        print(f"連結：{d['link']}")
    if t == "homework":
        s = z.my_submission(x["id"])
        if s.get("created_at"):
            kind = "草稿" if s.get("is_draft") else "已提交"
            print(f"我的提交：{kind} {local_time(s.get('created_at'), '%Y-%m-%d %H:%M')}"
                  f"　分數 {s.get('score') if s.get('score') is not None else '未評'}")
            for u in s.get("uploads") or []:
                print(f"  - {u.get('name')}")
            if s.get("comment"):
                print("  " + html_to_text(s["comment"]).replace("\n", "\n  "))
        else:
            print("我的提交：尚未提交")
    elif t == "forum":
        ts = z.topics(z.forum_category(x["id"]))
        print(f"討論帖 {len(ts)} 則（forum list {x['id']} 看全部）")


def cmd_forum(a):
    z = Zju()
    if a.action == "list":
        uid = z.user_id() if a.mine else None
        for t in z.topics(z.forum_category(a.id)):
            by = t.get("created_by") or {}
            if uid and by.get("id") != uid:
                continue
            print(f"{t['id']}\t{local_time(t.get('created_at'))}\t{by.get('name', '')}\t"
                  f"回覆 {t.get('reply_count', 0)}\t{t.get('title', '')}")
            if a.full:
                print("  " + html_to_text(t.get("content")).replace("\n", "\n  "))
    elif a.action == "read":
        t = z.topic(a.id)
        by = t.get("created_by") or {}
        print(f"# {t.get('title')}\n{by.get('name', '')}  {local_time(t.get('created_at'), '%Y-%m-%d %H:%M')}\n")
        print(html_to_text(t.get("content")))
        for u in t.get("uploads") or []:
            print(f"附件：{u.get('name')}")

        def show(rs, depth):
            for r in rs or []:
                rb = r.get("created_by") or {}
                pad = "  " * depth
                print(f"\n{pad}↳ {rb.get('name', '')}  {local_time(r.get('created_at'))}")
                print(pad + html_to_text(r.get("content")).replace("\n", "\n" + pad))
                show(r.get("replies"), depth + 1)
        show(t.get("replies"), 1)
    else:
        body = read_body(a)
        if not body.strip():
            raise ZjuError("內容是空的：用 --body 或 --body-file")
        content = body if a.html else text_to_html(body)
        if a.action == "post":
            if not a.title:
                raise ZjuError("發帖要 --title")
            cat = z.forum_category(a.id)
            ids = upload_all(z, a.attach)
            t = z.create_topic(cat, a.title, content, ids)
            print(f"[已發帖] topic {t['id']}：{t.get('title')}")
        else:
            ids = upload_all(z, a.attach)
            r = z.reply_topic(a.id, content, ids)
            print(f"[已回帖] reply {r.get('id')} → topic {a.id}")


def cmd_upload(a):
    z = Zju()
    for f in a.files:
        p = Path(f).expanduser()
        if not p.is_file():
            raise ZjuError(f"找不到檔案：{p}")
        u = z.upload_file(p)
        print(f"{u['id']}\t{p.name}")


def cmd_submit(a):
    z = Zju()
    x = z.activity(a.activity)
    if x.get("type") != "homework":
        raise ZjuError(f"活動 {a.activity} 是 {x.get('type')}，不是作業")
    status = act_status(x)
    if status != "進行中" and not x.get("is_resubmit_open"):
        raise ZjuError(f"作業「{x.get('title')}」{status}，網頁上也交不了")
    comment = read_body(a)
    if not comment.strip() and not a.file and not a.upload_id:
        raise ZjuError("沒有東西可交：給 --file、--upload-id 或 --body")
    prev = z.my_submission(x["id"])
    draft_id = prev.get("id") if prev.get("is_draft") else None
    kind = "存草稿" if a.draft else "正式提交"
    print(f"{kind}「{x.get('title')}」（截止 {local_time(x.get('end_time'), '%Y-%m-%d %H:%M')}）")
    for f in a.file or []:
        print(f"  檔案：{f}")
    if comment.strip():
        print(f"  文字：{comment.strip()[:80]}{'…' if len(comment.strip()) > 80 else ''}")
    if prev.get("created_at") and not prev.get("is_draft"):
        print("  注意：已經交過一次，這次會新增一份提交")
    if not a.yes:
        if not sys.stdin.isatty():
            raise ZjuError("非互動環境要加 --yes 才會真的送出")
        try:
            ok = input("確定送出？[y/N] ").strip().lower() == "y"
        except EOFError:  # Windows 的 NUL 也算 tty，讀不到就當取消
            ok = False
        if not ok:
            log("已取消（非互動環境加 --yes）")
            return
    ids = upload_all(z, a.file) + (a.upload_id or [])
    content = comment if a.html else text_to_html(comment) if comment.strip() else ""
    s = z.submit(x["id"], content, ids, a.draft, (x.get("data") or {}).get("mode") or "normal", draft_id)
    print(f"[{kind}] submission {s.get('id', '')} ✓")


def cmd_classroom(a):
    z = Zju()
    if a.action == "search":
        for c in z.classroom_search(a.arg or "", a.teacher or ""):
            print(f"{c.get('course_id')}\t{c.get('title')}\t{c.get('realname')}\t{c.get('term_name', '')}")
    elif a.action == "subs":
        if not a.arg:
            raise ZjuError("用法：classroom subs <course_id>")
        for s in z.course_subs(int(a.arg)):
            print(f"{s['sub_id']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action == "day":
        start = dt.date.fromisoformat(a.arg) if a.arg else dt.date.today()
        for d, s in range_subs(z, start, a.days or 1):
            print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action in ("add", "rm"):
        ids = [a.arg, *a.more] if a.arg else []
        if not ids or not all(v.isdigit() for v in ids):
            raise ZjuError(f"用法：classroom {a.action} <course_id>...（course_id 用 classroom search 查）")
        cfg = load_config()
        tracked = cfg.setdefault("tracked_courses", {})
        for cid in ids:
            if a.action == "rm":
                name = tracked.pop(cid, None)
                print(f"[移除] {cid} {name}" if name else f"[未追蹤] {cid}")
                continue
            subs = z.course_subs(int(cid))  # 順便驗證 id 存在
            name = f"{subs[0]['course_name']} {subs[0]['lecturer']}" if subs else cid
            tracked[cid] = name
            print(f"[追蹤] {cid} {name}（{len(subs)} 堂）")
        save_config(cfg)
    elif a.action == "tracked":
        for cid, name in tracked_courses().items():
            print(f"{cid}\t{name}")


def cmd_ppt(a):
    z = Zju()
    root = Path(a.out).expanduser()
    subs = resolve_subs(z, a)
    if not subs:
        log("沒有課堂")
        return
    failed = 0
    for s in subs:
        try:
            ppt_one(z, a, root, s)
        except Exception as e:  # 一堂壞掉不拖垮其他堂
            failed += 1
            log(f"[失敗] {s['course_name']} {s['sub_name']}: {e}")
    if failed:
        sys.exit(2)


def ppt_one(z: Zju, a, root: Path, s: dict):
    cdir = root / safe_name(s["course_name"]) / "智雲PPT"
    pdf = cdir / f"{safe_name(s['sub_name'])}.pdf"
    if pdf.exists() and not a.force:
        log(f"[略過] {pdf.relative_to(root)}")
        return
    urls = z.ppt_urls(s["course_id"], s["sub_id"])
    if not urls:
        log(f"[無PPT] {s['course_name']} {s['sub_name']}")
        return
    tmpdir = Path(tempfile.mkdtemp(prefix="zju-ppt-"))
    try:
        def grab(iu):
            i, u = iu
            p = tmpdir / f"{i:04d}{Path(urlparse(u).path).suffix[:5] or '.jpg'}"
            for attempt in range(5):
                r = z.get(secure_url(u))
                if r.ok and r.content:
                    p.write_bytes(r.content)
                    return p
                time.sleep(0.2 * 2 ** attempt)
            raise ZjuError(f"PPT 圖下載失敗：{u}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(grab, enumerate(urls)))  # map 保序 = 頁序
        pages = dedup_slides(paths) if a.dedup else paths
        cdir.mkdir(parents=True, exist_ok=True)
        images_to_pdf(pages, pdf)
        if a.keep_images:  # 留全部原圖，去重只影響 PDF
            shutil.copytree(tmpdir, cdir / safe_name(s["sub_name"]), dirs_exist_ok=True)
        note = f"，去重前 {len(paths)}" if len(pages) != len(paths) else ""
        print(f"[PDF] {pdf.relative_to(root)}（{len(pages)} 頁{note}）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def cmd_transcript(a):
    z = Zju()
    root = Path(a.out).expanduser()
    failed = 0
    for s in resolve_subs(z, a):
        out = root / safe_name(s["course_name"]) / "轉錄" / f"{safe_name(s['sub_name'])}.{a.format}"
        if out.exists() and not a.force:
            log(f"[略過] {out.relative_to(root)}")
            continue
        try:
            items = z.subtitle(s["sub_id"])
        except Exception as e:
            failed += 1
            log(f"[失敗] {s['course_name']} {s['sub_name']}: {e}")
            continue
        if not items:
            log(f"[無轉錄] {s['course_name']} {s['sub_name']}")
            continue
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render_transcript(items, a.format, f"{s['course_name']} {s['sub_name']}"))
        print(f"[轉錄] {out.relative_to(root)}（{len(items)} 段）")
    if failed:
        sys.exit(2)


def main():
    p = argparse.ArgumentParser(prog="zju.py", description="學在浙大 / 智雲課堂 CLI")
    try:
        default_out = os.environ.get("ZJU_OUT") or load_config().get("out") or str(DEFAULT_OUT)
    except ZjuError as e:
        log(f"錯誤：{e}")
        sys.exit(1)
    p.add_argument("--out", default=default_out, help=f"輸出根目錄（目前 {default_out}；config.json 的 out 或 ZJU_OUT 可改）")
    sp = p.add_subparsers(dest="cmd", required=True)

    x = sp.add_parser("login", help="設定學號並把密碼存進 Keychain")
    x.add_argument("username", nargs="?")
    x.add_argument("--reset", action="store_true", help="重設 Keychain 密碼")
    x.set_defaults(fn=cmd_login)

    x = sp.add_parser("courses", help="列出學在浙大課程")
    x.add_argument("--all", action="store_true", help="含往年課程（預設只列最新學年）")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_courses)

    x = sp.add_parser("sync", help="增量同步課件")
    x.add_argument("course", nargs="*", help="課程 id 或名稱片段；省略 = 最新學年所有課程")
    x.add_argument("--all", action="store_true", help="沒指定課程時抓全部學年")
    x.add_argument("--dry-run", action="store_true")
    x.add_argument("--videos", action="store_true", help="連影音檔也抓（預設跳過）")
    x.add_argument("-j", "--jobs", type=int, default=4, help="並行下載數（預設 4）")
    x.add_argument("--max-size", type=int, default=200, metavar="MB", help="單檔上限，超過只列出（預設 200，0 = 不限）")
    x.set_defaults(fn=cmd_sync)

    x = sp.add_parser("todo", help="待辦事項")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_todo)

    x = sp.add_parser("activities", help="列出課程活動（課件／影片／作業／討論／測驗…）")
    x.add_argument("course", nargs="*", help="課程 id 或名稱片段；省略 = 最新學年所有課程")
    x.add_argument("--all", action="store_true", help="沒指定課程時含往年課程")
    x.add_argument("--type", nargs="*", metavar="T", help=f"只列這些類型：{', '.join(ACT_TYPES)}")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_activities)

    x = sp.add_parser("show", help="單一活動詳情（說明、附件、作業提交狀態、討論帖數）")
    x.add_argument("activity", type=int)
    x.add_argument("--read", action="store_true", help="下載附件並印出文字（docx/pptx/pdf/doc/txt）")
    x.set_defaults(fn=cmd_show)

    def body_args(x):
        x.add_argument("--body", help="內容（純文字，空行分段）")
        x.add_argument("--body-file", help="從檔案讀內容；- = stdin")
        x.add_argument("--html", action="store_true", help="內容已經是 HTML，不轉換")
        x.add_argument("--attach", nargs="*", metavar="FILE", help="附件")

    x = sp.add_parser("forum", help="討論區：list / read / post / reply")
    fp = x.add_subparsers(dest="action", required=True)
    y = fp.add_parser("list", help="列出討論帖（給討論活動 id）")
    y.add_argument("id", type=int, help="討論活動 id（activities --type forum 查）")
    y.add_argument("--mine", action="store_true", help="只看自己發的")
    y.add_argument("--full", action="store_true", help="連內文一起印")
    y = fp.add_parser("read", help="讀一則帖子與回覆")
    y.add_argument("id", type=int, help="topic id")
    y = fp.add_parser("post", help="發新帖")
    y.add_argument("id", type=int, help="討論活動 id")
    y.add_argument("--title", required=True)
    body_args(y)
    y = fp.add_parser("reply", help="回帖")
    y.add_argument("id", type=int, help="topic id")
    body_args(y)
    x.set_defaults(fn=cmd_forum)

    x = sp.add_parser("upload", help="上傳檔案到學在浙大，印出 upload id")
    x.add_argument("files", nargs="+")
    x.set_defaults(fn=cmd_upload)

    x = sp.add_parser("submit", help="交作業（附檔＋文字），預設送出前確認")
    x.add_argument("activity", type=int, help="作業活動 id（activities --type homework 查）")
    x.add_argument("--file", nargs="*", metavar="FILE", help="要交的檔案")
    x.add_argument("--upload-id", type=int, nargs="*", help="已用 upload 指令傳好的檔案 id")
    x.add_argument("--body", help="作業文字內容")
    x.add_argument("--body-file", help="從檔案讀作業文字；- = stdin")
    x.add_argument("--html", action="store_true", help="文字已經是 HTML")
    x.add_argument("--draft", action="store_true", help="只存草稿不正式提交")
    x.add_argument("-y", "--yes", action="store_true", help="不確認直接送出")
    x.set_defaults(fn=cmd_submit)

    x = sp.add_parser("classroom", help="智雲課堂：search / subs / day / add / rm / tracked")
    x.add_argument("action", choices=["search", "subs", "day", "add", "rm", "tracked"])
    x.add_argument("arg", nargs="?")
    x.add_argument("more", nargs="*", help=argparse.SUPPRESS)
    x.add_argument("--teacher")
    x.add_argument("--days", type=int)
    x.set_defaults(fn=cmd_classroom)

    for name, fn in (("ppt", cmd_ppt), ("transcript", cmd_transcript)):
        x = sp.add_parser(name, help="智雲 PPT → PDF" if name == "ppt" else "智雲課堂語音轉錄")
        x.add_argument("--course", type=int, help="智雲課堂 course_id（classroom search 查）")
        x.add_argument("--sub", type=int, nargs="*", help="只抓這些 sub_id")
        x.add_argument("--days", type=int, help="不給 --course 時：最近 N 天的課（預設 1 = 今天；含 classroom add 追蹤的課）")
        x.add_argument("--force", action="store_true", help="已存在也重抓")
        if name == "ppt":
            x.add_argument("--keep-images", action="store_true")
            x.add_argument("--dedup", action="store_true",
                           help="刪掉重複截圖（動畫逐步出現、邊講邊寫、翻回前頁），每頁只留最完整的一張")
        else:
            x.add_argument("--format", choices=["txt", "srt", "md"], default="txt")
        x.set_defaults(fn=fn)

    a = p.parse_args()
    try:
        a.fn(a)
    except ZjuError as e:
        log(f"錯誤：{e}")
        sys.exit(1)
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()

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
  zju.py show <活動id> [--read]         # 活動詳情；作業顯示提交狀態、討論顯示自己的發帖回帖；--read 印出附件文字
  zju.py forum list|read|post|reply ... # 討論區
  zju.py upload 檔案...                 # 上傳，印 upload id
  zju.py submit <作業id> --file ... [--body ...] [--draft] [-y]  # 交作業
  zju.py classroom search 關鍵字        # 智雲課堂找課（id 與學在浙大不同）
  zju.py classroom subs <cid>          # 列出每堂課
  zju.py classroom day [日期] [--days N]   # 課表的課＋追蹤中的旁聽課
  zju.py classroom add|rm <cid>...      # 追蹤課表外的課（申請聽課核准的），--days 也會抓；rm 課表的課 = 忽略
  zju.py classroom tracked             # 列出追蹤中／忽略的課
  zju.py rollcall [--in-class] [--json]  # 簽到提醒用：只列出進行中的簽到，不代簽
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
from urllib.parse import quote, unquote, urljoin, urlparse

import ssl

import requests
from requests.adapters import HTTPAdapter

KEYCHAIN_SERVICE = "zju-learning"
STATE_DIR = Path(os.environ.get("ZJU_STATE_DIR") or (Path.home() / ".config" / "zju-learning"))
CONFIG_FILE = STATE_DIR / "config.json"
COOKIE_FILE = STATE_DIR / "cookies.json"
SCHEDULE_FILE = STATE_DIR / "schedule.json"  # rollcall --in-class 的當日課表快取
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

    def rollcalls(self) -> list[dict]:
        """進行中的簽到（欄位同 XMU-Rollcall-Bot）。只讀列表：不碰 answer / student_rollcalls。"""
        self.ensure()
        return self.json(self.get(f"{LMS}/api/radar/rollcalls", params={"api_version": "1.1.0"}),
                         "rollcalls").get("rollcalls", [])

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

    def my_forum_posts(self, aid: int) -> tuple[list[dict], list[tuple[dict, dict]]]:
        """自己在某討論活動的發帖與回帖（回帖附所屬主帖）。回帖要逐帖抓詳情，只抓有回覆的帖。"""
        uid = self.user_id()
        ts = self.topics(self.forum_category(aid))
        mine = [t for t in ts if (t.get("created_by") or {}).get("id") == uid]
        replies = []

        def walk(t, rs):
            for r in rs or []:
                if (r.get("created_by") or {}).get("id") == uid:
                    replies.append((t, r))
                walk(t, r.get("replies"))

        with ThreadPoolExecutor(8) as ex:
            for t in ex.map(lambda t: self.topic(t["id"]), [t for t in ts if t.get("reply_count")]):
                walk(t, t.get("replies"))
        replies.sort(key=lambda x: x[1].get("created_at") or "")
        return mine, replies

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
                                     "day": dt.date.fromtimestamp(begin) if begin else None,
                                     "begin": begin, "over": int(s.get("class_over") or 0)})
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
                             "sub_name": c["sub_title"], "lecturer": c.get("realname", ""),
                             "begin": int(c.get("course_begin") or 0), "over": int(c.get("course_over") or 0),
                             "room": c.get("room_name") or ""})
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


# ---------------- zdbk：本科教學管理信息服務平台（正方 jwglxt） ----------------
# 協議參考 Celechron（lib/http/zjuServices/zdbk.dart）與 Anchorite-no/zju-skill-public（lib/zdbk），自行實作。

ZDBK = "https://zdbk.zju.edu.cn/jwglxt"
ZDBK_CAS = "https://zjuam.zju.edu.cn/cas/login?service=" + quote(f"{ZDBK}/xtgl/login_ssologin.html", safe="")
ZDBK_JOURNAL = STATE_DIR / "zdbk_xk_journal.jsonl"  # 選退課寫入前後各記一筆，結果不明時拿來對帳
GNMKDM = {"grades": "N5083", "major": "N908550", "plan": "N153020", "xk": "N253530"}  # URL 的功能模組碼
ZDBK_TERMS = {"1": "秋冬", "2": "春夏"}
GRADE_PASS = re.compile(r"^(优秀?|良好?|中等?|及格|合格|[ABCDP])$")  # 五級制教務網常只寫一個字


def zdbk_text(v) -> str:
    return re.sub(r"\s+", " ", html_to_text(str(v or ""))).strip()


class Zdbk:
    """借 Zju 的 CAS cookie 換教務網 JSESSIONID（存進同一份 cookies.json），失效自動重登一次。
    教務網按滑動窗口限頻（HTTP 921），請求間至少隔 1 秒。"""
    GAP = 1.0

    def __init__(self, z: Zju | None = None):
        self.z = z or Zju()
        self.su = os.environ.get("ZJU_USER") or load_config().get("username")
        if not self.su:
            raise ZjuError("尚未設定帳號，先跑：zju.py login")
        self._last = 0.0
        self._ctx = None

    @staticmethod
    def expired(r: requests.Response) -> bool:
        if r.status_code in (901, 922):  # 901 會話失效；922 被別台機器登入頂掉
            return True
        loc = r.headers.get("Location", "")
        if r.is_redirect and re.search(r"login_slogin|login_ssologin|/cas/login", loc) and "ticket=" not in loc:
            return True
        return (r.status_code == 200 and "html" in r.headers.get("Content-Type", "")
                and re.search(r'id=["\']loginForm["\']', r.text) is not None)

    def login(self):
        """手動跟轉址：CAS 偶爾回 http:// 網址，http 走 NoCookieHTTP 不帶 cookie，要先升 https。"""
        for attempt in range(2):
            url = ZDBK_CAS
            for _ in range(8):
                r = self.z.get(url, allow_redirects=False)
                if not r.is_redirect:
                    break
                url = urljoin(url, r.headers["Location"])
                if url.startswith("http://"):
                    url = "https://" + url[7:]
            host = urlparse(url).hostname or ""
            if host.startswith("zdbk") and r.status_code == 200 and not self.expired(r):
                self.z.save_cookies()
                return
            if attempt == 0:
                if host.startswith("zjuam"):  # CAS 自己也過期：整套重登
                    self.z.login(*get_credentials())
                else:
                    time.sleep(1.5)  # 回跳偶爾落回登入頁，隔一下再走一次
        raise ZjuError("教務網登入失敗：CAS 回跳後仍是登入頁")

    def req(self, method: str, path: str, module: str, write: bool = False, **kw) -> requests.Response:
        """write=True：不重試、會話失效也不重送（選課寫入結果不明時只能回查）。"""
        params = {**(kw.pop("params", None) or {}), "gnmkdm": module}
        if module != "index":
            params["su"] = self.su
        if method == "POST":
            kw.setdefault("headers", {})["X-Requested-With"] = "XMLHttpRequest"
        for attempt in range(4):
            time.sleep(max(0.0, self._last + self.GAP - time.time()))
            r = self.z.req(method, f"{ZDBK}/{path}", params=params, allow_redirects=False,
                           retry=False if write else None, **kw)
            self._last = time.time()
            if r.status_code == 921 and not write and attempt < 3:  # 限頻：退讓後重試
                time.sleep(3 * (attempt + 1))
                continue
            if self.expired(r):
                if write:
                    raise ZjuError("教務網會話失效，寫入請求沒有重送；先跑 zju zdbk xk selected 回查")
                if attempt > 1:
                    break
                self.login()
                continue
            if r.status_code != 200:
                raise ZjuError(f"教務網 HTTP {r.status_code}：{path}")
            return r
        raise ZjuError(f"教務網請求失敗（會話或限頻）：{path}")

    def post_json(self, path: str, module: str, data: dict | None = None, **kw):
        r = self.req("POST", path, module, data=data or {}, **kw)
        try:
            return r.json()
        except ValueError:
            raise ZjuError(f"教務網沒有回 JSON：{path}\n{r.text[:200]}")

    def grid(self, path: str, module: str, form: dict, sort: str = "") -> list[dict]:
        """jqGrid 列表：POST ?doType=query，分頁取完。"""
        out = []
        for page in range(1, 51):
            j = self.post_json(path, module, {**form, "queryModel.showCount": "500", "queryModel.currentPage": str(page),
                                              "queryModel.sortName": sort, "queryModel.sortOrder": "asc", "time": "1"},
                               params={"doType": "query"})
            items = j.get("items")
            if not isinstance(items, list):
                raise ZjuError(f"教務網列表缺 items：{path}")
            out += items
            total = int(j.get("totalResult") or len(out))
            if len(out) >= total or not items:
                return out
        raise ZjuError(f"教務網列表分頁超過上限：{path}")

    # ---- 成績 ----

    def grades(self) -> dict:
        rows = self.grid("cxdy/xscjcx_cxXscjIndex.html", GNMKDM["grades"], {"xn": "", "xq": "", "zscjl": "", "zscjr": ""}, "xkkh")
        major = self.grid("zycjtj/xszgkc_cxXsZgkcIndex.html", GNMKDM["major"], {"xh": "", "kcmc": ""}, "xkkh, kcdm")
        try:  # result != "1"：本學期還沒評教，教務網不給看本學期成績
            ev = self.post_json("xtgl/index_cxMyCosJxpj.html", "index")
            pending = ev.get("status") == "success" and ev.get("result") != "1"
        except ZjuError:
            pending = False
        courses = mark_retaken([grade_row(r) for r in rows])
        return {"courses": courses,
                "major": mark_retaken([grade_row(r) for r in major if zdbk_text(r.get("xdbj")) == "1"]),
                "evaluation_pending": pending}

    # ---- 培養方案 ----

    def plans(self, year: str = "", keyword: str = "") -> list[dict]:
        return self.grid("pyfagl/pyfaxxcx_cxPyfaxscxIndex.html", GNMKDM["plan"],
                         {"synj": year, "xydm": "", "gjzydm": "", "zydm": "", "dlbs": "", "jxjhh": keyword}, "synj desc, xymc, jxjhh")

    def plan_pdf(self, plan_id: str) -> bytes:
        r = self.req("GET", "pyfagl/pyfayl_cxPyfaylPdf.html", GNMKDM["plan"], params={"id": plan_id, "doType": "query"})
        if not r.content.startswith(b"%PDF-"):
            raise ZjuError(f"培養方案 {plan_id} 沒有回 PDF（{r.headers.get('Content-Type')}）")
        return r.content

    # ---- 選課（自主選課 xsxk，常規選課與低定共用） ----

    def xk_url(self, name: str) -> str:
        return f"xsxk/zzxkghb_{name}.html"

    def xk_context(self, fresh: bool = False) -> dict:
        """選課首頁的隱藏欄位：選課學年學期、年級、專業、志願數；頁籤（課程類別）從 categroyClick(...) 抓。"""
        if self._ctx and not fresh:
            return self._ctx
        page = re.sub(r"<!--.*?-->", "", self.req("GET", self.xk_url("cxZzxkGhbIndex"), GNMKDM["xk"]).text, flags=re.S)
        inputs = {}
        for tag in re.findall(r"<input\b[^>]*>", page, re.I):
            a = dict((k, html.unescape(v)) for k, _, v in re.findall(r'([\w-]+)\s*=\s*(["\'])(.*?)\2', tag, re.S))
            if a.get("id") or a.get("name"):
                inputs[a.get("id") or a.get("name")] = a.get("value", "")
        if not all(inputs.get(k) for k in ("xn", "xq", "nj", "zydm", "xkzys")):
            raise ZjuError("選課首頁缺學期／年級／專業欄位，可能目前不開放選課")
        cats = []
        for m in re.finditer(r"categroyClick\(\s*this\s*,\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", page, re.S):
            c = {"dl": m.group(1), "lx": m.group(2), "name": zdbk_text(m.group(3))}
            if c not in cats:
                cats.append(c)
        c = {k: inputs.get(k, "") for k in ("xn", "xq", "nj", "zydm", "xkzys", "ylxs")}
        c["xnxq"] = f"({c['xn']}-{c['xq']})-"
        c["jxjhh"] = c["nj"] + c["zydm"]  # 本人的教學計劃號 = 年級 + 專業代碼
        c["categories"] = cats
        self._ctx = c
        return c

    def xk_selected(self) -> list[dict]:
        c = self.xk_context()
        j = self.post_json(self.xk_url("cxZzxkGhbChoosed"), GNMKDM["xk"], {"xn": c["xn"], "xq": c["xq"]})
        if not isinstance(j, list):
            raise ZjuError("已選志願沒有回列表")
        return j

    def xk_credits(self) -> dict:
        self.xk_context()
        j = self.post_json(self.xk_url("cxZzxkYxxf"), GNMKDM["xk"])
        v = j.get("result") or {}
        if j.get("status") != "success":
            raise ZjuError("選課學分匯總查詢失敗")
        return {"total": v.get("xf9"), "regular": v.get("xf91"), "short": v.get("xf92")}

    def xk_search_form(self, keyword: str, field: str = "kcmc", available: bool = False) -> dict:
        c = self.xk_context()
        return {"xnxq": c["xnxq"], "cx_zbgx": "and", "cx_ylcx": "1" if available else "0",
                "cxtjList[0].cxlb": field, "cxtjList[0].cxgx": "like", "cxtjList[0].cxnr": keyword}

    def xk_search(self, keyword: str, field: str = "kcmc", available: bool = False, size: int = 50) -> list[dict]:
        """全部課程搜尋（頁籤 xk_9）：回課程池列，xkkh 是課程池代號不是教學班。"""
        j = self.post_json(self.xk_url("cxZzxkGhbDdkcList"), GNMKDM["xk"],
                           {**self.xk_search_form(keyword, field, available), "kspage": "1", "jspage": str(size)})
        if not isinstance(j, list):
            raise ZjuError("課程搜尋沒有回列表")
        return j

    def xk_sections(self, course: dict, keyword: str, field: str = "kcmc") -> list[dict]:
        """某門課（搜尋結果的一列）底下的教學班；搜尋頁籤展開時要帶原搜尋條件。"""
        c = self.xk_context()
        j = self.post_json(self.xk_url("cxZzxkGhbJxbList"), GNMKDM["xk"], {
            "dl": "xk_9", "xn": c["xn"], "xq": c["xq"], "kcdm": course.get("kcdm", ""), "xkkh": course.get("xkkh", ""),
            "ylxs": c["ylxs"], **self.xk_search_form(keyword, field)})
        if not isinstance(j, list):
            raise ZjuError("教學班列表沒有回列表")
        return j

    def xk_write(self, action: str, name: str, form: dict, verify) -> dict:
        """寫入一律：先記 journal → 只送一次 → 回查已選志願判定結果。不自動重送。"""
        def journal(stage, **kw):
            state_dir()
            with ZDBK_JOURNAL.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"at": dt.datetime.now(CST).isoformat(), "action": action, "stage": stage, **kw},
                                   ensure_ascii=False) + "\n")
        journal("before", form=form)
        resp, err = None, None
        try:
            resp = self.req("POST", self.xk_url(name), GNMKDM["xk"], write=True, data=form,
                            headers={"Referer": f"{ZDBK}/{self.xk_url('cxZzxkGhbIndex')}"}).json()
        except (ZjuError, ValueError) as e:
            err = str(e).splitlines()[0]
        try:
            after = self.xk_selected()
        except ZjuError as e:
            after, err = None, err or f"回查失敗：{e}"
        ok = after is not None and verify(after)
        rejected = isinstance(resp, dict) and str(resp.get("flag")) == "0"
        status = "saved" if ok else "rejected" if rejected else "unknown"
        msg = (resp or {}).get("msg", "") if isinstance(resp, dict) else ""
        journal("after", status=status, response=resp, error=err)
        return {"status": status, "message": msg, "error": err, "choices": after}


def grade_row(r: dict) -> dict:
    """一筆成績 → 是否計入績點。按浙大發本〔2022〕24號：重修每次都計入；棄修、緩考、二級制合格不計；
    缺考、無效、不及格照教務網給的 0 績點計入。"""
    grade, raw = zdbk_text(r.get("cj")), zdbk_text(r.get("jd"))
    try:
        point = float(raw)
    except ValueError:
        point = None
    note = ("暫無成績" if not grade else "棄修不計" if "弃修" in grade else "緩考" if "缓考" in grade
            else "二級制不計" if re.fullmatch(r"合格|P", grade) else "無績點" if point is None else "")
    # 補考紀錄的選課課號只有 (學年-學期)-課程代碼，沒有教師與班號
    m = re.match(r"\((\d{4}-\d{4})-(\d+)\)-([^-]+)(-?)", zdbk_text(r.get("xkkh")))
    remark = "；".join(x for x in (zdbk_text(r.get("bkcj")), "補考" if m and not m.group(4) else "") if x)
    return {"name": zdbk_text(r.get("kcmc")), "grade": grade, "credits": float(zdbk_text(r.get("xf")) or 0),
            "point": point, "point43": to_43(point), "in_gpa": not note, "note": note, "remark": remark,
            "year": m.group(1) if m else "", "term": m.group(2) if m else "",
            "code": zdbk_text(r.get("kcdm")) or (m.group(3) if m else "")}


def to_43(point: float | None) -> float | None:
    """五分制 → 4.3 分制（《浙江大学本科课程成绩评定与管理办法》；2022 級起出國用）：
    95–100 分 5.0→4.3、92–94 分 4.8→4.2、89–91 分 4.5→4.1、86–88 分 4.2→4.0，3.9 以下兩制相同。"""
    return {5.0: 4.3, 4.8: 4.2, 4.5: 4.1, 4.2: 4.0}.get(point, point)


def gpa_of(courses: list[dict], key: str = "point") -> tuple[float | None, float]:
    counted = [c for c in courses if c["in_gpa"]]
    cr = sum(c["credits"] for c in counted)
    return (round(sum(c["credits"] * c[key] for c in counted) / cr, 3) if cr else None), round(cr, 2)


def passed(c: dict) -> bool:
    g = c["grade"]
    return float(g) >= 60 if re.fullmatch(r"\d+(\.\d+)?", g) else bool(GRADE_PASS.match(g))


def mark_retaken(cs: list[dict]) -> list[dict]:
    """出國口徑：沒過（不及格、缺考、棄修…）且之後同課號有再修的，標 retaken，算 GPA 時只看重修那次。"""
    for c in cs:
        key, when = c["code"] or c["name"], (c["year"], c["term"])
        c["retaken"] = not passed(c) and any((d["code"] or d["name"]) == key and (d["year"], d["term"]) > when
                                             for d in cs)
    return cs


# ---------------- 查老師（chalaoshi.de） ----------------
# 做法照 ADSR1042/Lazuli 選課插件：下載站上的 search.json 索引，教學班老師按 <br> 拆開後姓名完全比對。

CHALAOSHI = "https://chalaoshi.de"
CHALAOSHI_INDEX = STATE_DIR / "chalaoshi-index.json"


class Chalaoshi:
    """牆外站：Zju.req 直連失敗會退環境 proxy。索引約 1 MB、一萬多位老師，快取 7 天，過期用 ETag 條件 GET；
    課程均績表（api 子域 /gpa?course=）每門課一次請求。連不上只警告，不擋選課查詢。"""
    TTL = 7 * 86400

    def __init__(self, z: Zju):
        self.z = z
        self._idx = None
        self._names = None
        self._gpa = {}
        self.offline = False

    def index(self) -> dict:
        if self._idx is None:
            self._idx = self._load()
        return self._idx

    def _load(self) -> dict:
        cached = None
        try:
            cached = json.loads(CHALAOSHI_INDEX.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if cached and time.time() - cached.get("fetched", 0) < self.TTL:
            return cached
        try:
            r = self.z.get(f"{CHALAOSHI}/static/json/search.json",
                           headers={"If-None-Match": cached["etag"]} if cached and cached.get("etag") else {})
            if r.status_code == 304 and cached:
                cached["fetched"] = time.time()
            elif r.status_code == 200:
                d = r.json()
                cached = {"fetched": time.time(), "etag": r.headers.get("ETag", ""),
                          "colleges": {str(c["id"]): c["name"] for c in d["colleges"]}, "teachers": d["teachers"]}
            else:
                raise ZjuError(f"HTTP {r.status_code}")
        except (ZjuError, ValueError, KeyError, TypeError) as e:
            if cached:
                log("警告：查老師索引更新失敗，沿用舊快取")
                return cached
            log(f"警告：查老師連不上（牆外站，要有代理），不顯示評分：{str(e).splitlines()[0]}")
            self.offline = True
            return {"teachers": [], "colleges": {}}
        state_dir()
        CHALAOSHI_INDEX.write_text(json.dumps(cached, ensure_ascii=False), encoding="utf-8")
        return cached

    def find(self, name: str, college: str = "") -> list[dict]:
        """姓名完全比對；同名時用開課學院挑一位，挑不出來就全回（呼叫端標同名）。"""
        if self._names is None:  # 外籍教師教務網名字有空格、查老師沒有，比對時都去掉
            self._names = {}
            for t in self.index()["teachers"]:
                self._names.setdefault(re.sub(r"\s+", "", t["name"]), []).append(t)
        hits = self._names.get(re.sub(r"\s+", "", name), [])
        if len(hits) > 1 and college:
            same = [t for t in hits if self.index()["colleges"].get(str(t["xy"])) == college]
            if len(same) == 1:
                return same
        return hits

    def course_gpa(self, course: str) -> dict[str, dict]:
        """某門課各老師的平均績點表：{老師: {gpa, sd, n}}；n 可能是「50+」這種字串。"""
        if course in self._gpa or self.offline:
            return self._gpa.get(course, {})
        out = {}
        try:
            r = self.z.get("https://api.chalaoshi.de/gpa", params={"course": course})
            if r.status_code == 200:
                for name, gpa, sd, n in re.findall(r'<td class="course_name">([^<]*)</td>\s*<td>([\d.]+)'
                                                   r'(?:<span[^>]*>±([\d.]+)</span>)?</td>\s*<td>([^<]*)</td>', r.text):
                    out[html.unescape(name).strip()] = {"gpa": float(gpa), "sd": float(sd) if sd else None, "n": n.strip()}
        except ZjuError as e:
            log(f"警告：查老師均績表取不到（{course}）：{str(e).splitlines()[0]}")
        self._gpa[course] = out
        return out

    def teachers(self, row: dict, course: str = "", college: str = "") -> list[dict]:
        """教學班一列的每位老師 → 評分、評分頁、這門課的均績（有給 course 才查）。"""
        table = self.course_gpa(course) if course else {}
        out = []
        # 體育課是「教師/[項目名]」
        for name in (re.sub(r"/\[.*?\]$", "", zdbk_text(n)) for n in re.split(r"<br\s*/?>|[,，;；]", str(row.get("jsxm") or ""))):
            if not name:
                continue
            hits = self.find(name, college)
            t = {"name": name}
            if len(hits) == 1:
                rate = hits[0].get("rate", "")
                t.update(rate=rate if re.fullmatch(r"\d+(\.\d+)?", rate or "") and float(rate) > 0 else None,
                         url=f"{CHALAOSHI}/t/{hits[0]['id']}/")
            elif hits:
                t["same_name"] = len(hits)
            if name in table:
                t["course_gpa"] = table[name]
            out.append(t)
        return out


def teacher_label(t: dict) -> str:
    if "url" in t:
        s = f"{t['name']} {t['rate']}分" if t["rate"] else f"{t['name']} 無評分"
    elif "same_name" in t:
        s = f"{t['name']}（查老師同名 {t['same_name']} 位）"
    else:
        s = f"{t['name']}（查老師無資料）"
    if "course_gpa" in t:
        g = t["course_gpa"]
        sd = "" if g["sd"] is None else f"±{g['sd']:g}"
        s += f" 均績{g['gpa']:g}{sd}（{g['n']}人）"
    return s


def pending_label(s: dict) -> str:
    """rs = 餘量/容量；yxrs = 本專業待定~全部待定。全部待定 ÷ 餘量 = 幾進一（插件的「N 進 1」）。"""
    cap, pend = zdbk_text(s.get("rs")), zdbk_text(s.get("yxrs"))
    out = f"餘量/容量 {cap or '-'}"
    m, p = re.match(r"(-?\d+)\s*/", cap), re.fullmatch(r"(\d+)~(\d+)", pend)
    if p:
        out += f"｜待定 本專業 {p.group(1)}／全部 {p.group(2)}"
        if m:
            rest = int(m.group(1))
            out += "（已滿）" if rest <= 0 else f"（{int(p.group(2)) / rest:.2f} 進 1）"
    elif pend:
        out += f"｜待定 {pend}"
    return out


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


def today_cst() -> dt.date:
    """課表以北京時間分日；主機在別的時區時 dt.date.today() 會差一天。"""
    return dt.datetime.now(CST).date()


def class_span(s: dict) -> str:
    if not s.get("begin"):
        return "-"
    f = lambda t: dt.datetime.fromtimestamp(t, CST).strftime("%H:%M")
    return f"{f(s['begin'])}-{f(s['over'])}"


def in_class(subs: list[dict], now: float, margin: int = 10) -> list[dict]:
    """now（Unix 秒）落在 [上課 - margin 分, 下課 + margin 分] 的課堂。"""
    m = margin * 60
    return [s for s in subs if s.get("begin") and s["begin"] - m <= now <= s["over"] + m]


def rollcall_kind(r: dict) -> str:
    return "雷達" if r.get("is_radar") else "數字" if r.get("is_number") else "點名"


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


def ignored_courses() -> set[str]:
    """config.json 的 ignored_courses：課表裡有但不想抓的課（如選聽後不聽了），--days 會跳過。"""
    return set(load_config().get("ignored_courses") or [])


def range_subs(z: Zju, end: dt.date, days: int) -> list[tuple[dt.date, dict]]:
    """end 往前 days 天：課表的課＋追蹤課程當天的堂次，依 sub_id 去重。"""
    out, seen = [], set()
    ignored = ignored_courses()
    for i in range(days):
        d = end - dt.timedelta(days=i)
        for s in z.day_subs(d):
            if str(s["course_id"]) in ignored:
                continue
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
    return [s for _, s in range_subs(z, today_cst(), a.days or 1)]


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
        mine, replies = z.my_forum_posts(x["id"])
        if not mine and not replies:
            print("我的參與：尚未發帖或回帖")
        else:
            print(f"我的參與：發帖 {len(mine)}、回帖 {len(replies)}")
            for p in mine:
                print(f"  - 發帖 {local_time(p.get('created_at'), '%Y-%m-%d %H:%M')}"
                      f"　{p.get('title', '')}  (topic {p['id']})")
            for tp, r in replies:
                print(f"  - 回帖 {local_time(r.get('created_at'), '%Y-%m-%d %H:%M')}"
                      f"　→ {tp.get('title', '')}  (topic {tp['id']})")


def cmd_forum(a):
    z = Zju()
    if a.action == "list":
        if a.mine:
            ts, replies = z.my_forum_posts(a.id)
        else:
            ts, replies = z.topics(z.forum_category(a.id)), []
        for t in ts:
            by = t.get("created_by") or {}
            print(f"{t['id']}\t{local_time(t.get('created_at'))}\t{by.get('name', '')}\t"
                  f"回覆 {t.get('reply_count', 0)}\t{t.get('title', '')}")
            if a.full:
                print("  " + html_to_text(t.get("content")).replace("\n", "\n  "))
        for tp, r in replies:
            print(f"{tp['id']}\t{local_time(r.get('created_at'))}\t(回帖)\t\t→ {tp.get('title', '')}")
            if a.full:
                print("  " + html_to_text(r.get("content")).replace("\n", "\n  "))
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
    if not confirm(a.yes):
        return
    ids = upload_all(z, a.file) + (a.upload_id or [])
    content = comment if a.html else text_to_html(comment) if comment.strip() else ""
    s = z.submit(x["id"], content, ids, a.draft, (x.get("data") or {}).get("mode") or "normal", draft_id)
    print(f"[{kind}] submission {s.get('id', '')} ✓")


def confirm(yes: bool, question: str = "確定送出？") -> bool:
    if yes:
        return True
    if not sys.stdin.isatty():
        raise ZjuError("非互動環境要加 --yes 才會真的送出")
    try:
        ok = input(f"{question}[y/N] ").strip().lower() == "y"
    except EOFError:  # Windows 的 NUL 也算 tty，讀不到就當取消
        ok = False
    if not ok:
        log("已取消（非互動環境加 --yes）")
    return ok


def term_name(year: str, term: str) -> str:
    return f"{year} {ZDBK_TERMS.get(term, f'第{term}學期')}"


def cmd_zdbk(a):
    zb = Zdbk()
    {"grades": zdbk_grades, "plan": zdbk_plan, "xk": zdbk_xk}[a.zcmd](zb, a)


GPA_SCOPES = {"abroad": "出國（掛科後重修過的只算重修那次）", "domestic": "國內（浙大發本〔2022〕24號，重修每次都計入）"}


def gpa_view(cs: list[dict], scope: str, scale: str) -> tuple[float | None, float]:
    return gpa_of([c for c in cs if scope == "domestic" or not c["retaken"]], "point43" if scale == "4.3" else "point")


def zdbk_grades(zb: Zdbk, a):
    g = zb.grades()
    cs = g["courses"]
    combos = {f"{sc}_{sl}": gpa_view(cs, sc, sl) for sc in GPA_SCOPES for sl in ("5", "4.3")}
    if a.json:
        g["gpa"] = {k: {"gpa": v[0], "credits": v[1]} for k, v in combos.items()}
        print(json.dumps(g, ensure_ascii=False, indent=1))
        return
    key = "point43" if a.scale == "4.3" else "point"
    print(f"口徑：{GPA_SCOPES[a.scope]}｜{a.scale} 分制（--scope abroad|domestic、--scale 5|4.3 切換）\n")
    terms: dict[tuple[str, str], list[dict]] = {}
    for c in cs:
        terms.setdefault((c["year"], c["term"]), []).append(c)
    for (year, term), lst in sorted(terms.items()):
        if a.term and not f"{year}-{term}".startswith(a.term):
            continue
        gpa, cr = gpa_view(lst, a.scope, a.scale)
        print(f"{term_name(year, term)}\tGPA {gpa if gpa is not None else '-'}（計入 {cr} 學分）")
        for c in lst:
            point = "-" if c[key] is None else f"{c[key]:g}"
            dropped = "已重修，出國口徑不計" if c["retaken"] and a.scope == "abroad" else ""
            extra = "；".join(x for x in (c["note"], c["remark"], dropped) if x)
            print(f"  {c['grade']}\t{c['credits']:g}\t{point}\t{c['name']}{f'（{extra}）' if extra else ''}")
    gpa, cr = combos[f"{a.scope}_{a.scale}"]
    print(f"\n總 GPA {gpa}（計入 {cr} 學分）")
    if g["major"]:
        mg, mcr = gpa_view(g["major"], a.scope, a.scale)
        print(f"主修 GPA {mg}（計入 {mcr} 學分，教務網「成績主修數據」標記已修的課）")
    earned = {c["code"] or c["name"]: c["credits"] for c in cs if passed(c)}  # 同課重修只算一次學分
    print(f"已獲學分 {round(sum(earned.values()), 2):g}")
    print("四種口徑：" + "｜".join(f"{'出國' if sc == 'abroad' else '國內'} {sl} 分制 {combos[f'{sc}_{sl}'][0]}"
                                  for sc in GPA_SCOPES for sl in ("5", "4.3")))
    print("棄修、緩考、二級制不計；缺考、不及格以 0 計入（出國口徑下還沒重修的掛科也照算 0）。")
    if g["evaluation_pending"]:
        print("注意：本學期還沒完成評教，教務網暫不顯示本學期成績。")


PLAN_FIELDS = (("zymc", "專業"), ("xymc", "學院"), ("synj", "年級"), ("dlbsmc", "類別"), ("zdbyxf", "最低畢業學分"),
               ("syxw", "學位"), ("xz", "學制"), ("xqmc", "校區"), ("jxjhh", "教學計劃號"))


def zdbk_plan(zb: Zdbk, a):
    if a.list or a.major:
        year = a.year or zb.xk_context()["nj"]
        rows = [r for r in zb.plans(year) if (a.college or "") in zdbk_text(r.get("xymc"))
                and (a.major or "") in zdbk_text(r.get("zymc"))]
        if a.list or len(rows) != 1:
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1))
                return
            if not a.list:
                log(f"「{a.major}」在 {year} 級對到 {len(rows)} 個方案，名稱寫完整一點：" if rows else f"{year} 級沒有專業名含「{a.major}」的方案")
            for r in rows:
                print("\t".join(zdbk_text(r.get(k)) for k in ("jxjhh", "synj", "xymc", "zymc", "dlbsmc", "zdbyxf", "syxw")))
            return
        plan = rows[0]
    else:
        c = zb.xk_context()
        pid = (a.year or c["nj"]) + c["zydm"]
        plan = next((r for r in zb.plans(keyword=pid) if zdbk_text(r.get("jxjhh")) == pid), None)
        if not plan:
            raise ZjuError(f"找不到教學計劃號 {pid} 的培養方案；用 --list 看全部")
    if a.json:
        print(json.dumps(plan, ensure_ascii=False, indent=1))
        return
    for k, label in PLAN_FIELDS:
        print(f"{label}：{zdbk_text(plan.get(k)) or '-'}")
    for k, label in (("zyhxkc", "專業核心課"), ("qywkc", "前沿課")):
        items = [s.strip() for s in re.split(r"<br\s*/?>|\s{2,}", str(plan.get(k) or ""), flags=re.I) if s.strip()]
        if items:
            print(f"{label}：{'、'.join(html.unescape(s) for s in items)}")
    pid = zdbk_text(plan.get("jxjhh"))
    data = zb.plan_pdf(pid)
    dest = Path(a.out) / "zdbk" / safe_name(f"培养方案-{zdbk_text(plan.get('synj'))}-{zdbk_text(plan.get('zymc'))}.pdf")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    print(f"完整方案 PDF（各學期課程、學分要求）：{dest}")
    if a.text:
        print(doc_to_text(data) or "（沒有 pdftotext，讀不出 PDF 文字）")


def xk_row(r: dict, *keys: str) -> str:
    return "\t".join(zdbk_text(r.get(k)) or "-" for k in keys)


def xk_credit(r: dict) -> str:
    """課程列沒有 xf，學分藏在 kcxx（如 1~4.0~3.5-1.0 的第二段）。"""
    parts = zdbk_text(r.get("kcxx")).split("~")
    return zdbk_text(r.get("xf")) or (parts[1] if len(parts) > 1 else "-")


def xk_find_section(zb: Zdbk, xkkh: str) -> dict:
    """教學班號 (學年-學期)-課程代碼-教師工號-序號 → 用課程代碼搜，再從教學班列表找回含 tabname 的那列。"""
    m = re.match(r"\((\d{4}-\d{4})-(\d+)\)-([^-]+)-", xkkh)
    c = zb.xk_context(fresh=True)
    if not m or (m.group(1), m.group(2)) != (c["xn"], c["xq"]):
        raise ZjuError(f"{xkkh} 不是本次選課學期（{c['xn']}-{c['xq']}）的教學班號；用 xk sections 查")
    for course in zb.xk_search(m.group(3), field="kcdm"):
        for s in zb.xk_sections(course, m.group(3), field="kcdm"):
            if s.get("xkkh") == xkkh:
                return {**course, **s}
    raise ZjuError(f"目前的選課列表裡找不到教學班 {xkkh}")


def zdbk_xk(zb: Zdbk, a):
    act = a.action
    if act == "context":
        c = zb.xk_context()
        print(f"選課學期：{term_name(c['xn'], c['xq'])}（{c['xn']}-{c['xq']}）｜年級 {c['nj']}｜專業代碼 {c['zydm']}"
              f"｜每門課志願數 {c['xkzys']}")
        print("頁籤：" + "、".join(f"{x['name']}({x['dl']})" for x in c["categories"]))
    elif act in ("selected", "credits"):
        if act == "selected":
            rows = zb.xk_selected()
            ch = Chalaoshi(zb.z)
            for r in rows:
                r["teachers"] = ch.teachers(r, zdbk_text(r.get("kcmc")) if a.gpa else "", zdbk_text(r.get("kkxy")))
            if a.json:
                print(json.dumps(rows, ensure_ascii=False, indent=1))
                return
            rows.sort(key=lambda r: (zdbk_text(r.get("xkzyfz")) or zdbk_text(r.get("kcdm")), zdbk_text(r.get("xkzy"))))
            for r in rows:
                state = {"1": "已選上", "0": "待篩選"}.get(zdbk_text(r.get("sxbj")), "?")
                print(f"{state}\t志願{zdbk_text(r.get('xkzy')) or '-'}\t" + xk_row(r, "xkkh", "kcmc")
                      + "\t" + "；".join(map(teacher_label, r["teachers"])) + "\t" + xk_row(r, "sksj", "xf"))
        cr = zb.xk_credits()
        print(f"選課學分：合計 {cr['total']}（長學期 {cr['regular']}，短學期 {cr['short']}）")
    elif act == "search":
        rows = zb.xk_search(a.arg, a.field, a.available)
        if a.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
            return
        for r in rows:
            print(xk_row(r, "kcdm", "kcmc") + f"\t{xk_credit(r)}\t" + xk_row(r, "xxq", "kclb", "kkxy"))
        if not rows:
            log("沒有符合的課程")
    elif act == "sections":
        courses = zb.xk_search(a.arg, a.field, a.available)
        if len(courses) > 5:
            raise ZjuError(f"「{a.arg}」對到 {len(courses)} 門課，先用 xk search 看，再給完整課名或 --field kcdm 課程代碼")
        out, ch = [], Chalaoshi(zb.z)
        for course in courses:
            secs = [s for s in zb.xk_sections(course, a.arg, a.field) if not a.teacher or a.teacher in zdbk_text(s.get("jsxm"))]
            for s in secs:
                s["teachers"] = ch.teachers(s, zdbk_text(course.get("kcmc")), zdbk_text(course.get("kkxy")))
            out.append({**course, "sections": secs})
            if a.json:
                continue
            print(f"{zdbk_text(course.get('kcdm'))} {zdbk_text(course.get('kcmc'))}（{xk_credit(course)} 學分，"
                  f"{zdbk_text(course.get('xxq'))}，{zdbk_text(course.get('kkxy'))}）")
            for s in secs:
                print(f"  {zdbk_text(s.get('xkkh'))}　{pending_label(s)}")
                print(f"    老師：{'；'.join(map(teacher_label, s['teachers'])) or '-'}")
                print(f"    {xk_row(s, 'sksj')}｜{xk_row(s, 'skdd')}｜考試 {xk_row(s, 'kssj')}")
        if a.json:
            print(json.dumps(out, ensure_ascii=False, indent=1))
        elif not courses:
            log("沒有符合的課程")
    elif act in ("select", "drop"):
        before = zb.xk_selected()
        mine = next((r for r in before if r.get("xkkh") == a.arg), None)
        if act == "select":
            if mine:
                print(f"{a.arg} 已在志願裡，不用再選")
                return
            s = xk_find_section(zb, a.arg)
            if str(s.get("jxbly")) == "2" or s.get("tabname") == "jxrwbview":
                raise ZjuError("補考選課不走常規志願介面")
            c = zb.xk_context()
            print(f"選課：{xk_row(s, 'kcmc', 'xkkh', 'jsxm', 'sksj', 'skdd')}\t餘量/容量 {zdbk_text(s.get('rs')) or '-'}")
            print("  常規選課存的是志願，截止後統一篩選；資格、時間衝突由教務網判定。")
            form, name = {"xn": c["xn"], "xq": c["xq"], "nj": c["nj"], "xkkh": a.arg, "tabname": s.get("tabname", ""),
                          "xkzys": c["xkzys"]}, "xkBcZyZzxkGhb"
            verify = lambda rows: any(r.get("xkkh") == a.arg for r in rows)
        else:
            if not mine:
                print(f"{a.arg} 不在志願裡，不用退")
                return
            print(f"退選：{xk_row(mine, 'kcmc', 'xkkh', 'jsxm', 'sksj')}")
            if zdbk_text(mine.get("sxbj")) == "1":
                print("  注意：這門已選上（或預置），退掉之後不保證選得回來")
            form, name = {"xkkh": a.arg}, "tuikBcZzxkGhb"
            verify = lambda rows: all(r.get("xkkh") != a.arg for r in rows)
        if confirm(a.yes):
            xk_report(zb.xk_write(act, name, form, verify))
    elif act == "reorder":
        ids = [a.arg, *a.more]
        before = zb.xk_selected()
        rows = [next((r for r in before if r.get("xkkh") == x), None) for x in ids]
        if None in rows or len(set(ids)) != len(ids):
            raise ZjuError("排序裡有不在志願中或重複的教學班號；先跑 xk selected")
        group = lambda r: zdbk_text(r.get("xkzyfz")) or zdbk_text(r.get("kcdm"))
        if len({group(r) for r in rows}) != 1 or len([r for r in before if group(r) == group(rows[0])]) != len(ids):
            raise ZjuError("一次只能排同一志願組，而且要列出該組全部教學班")
        for i, r in enumerate(rows, 1):
            print(f"志願{i}：{xk_row(r, 'xkkh', 'kcmc', 'jsxm', 'sksj')}")
        print("  注意：已選上／預置課程改順序會重新參與篩選")
        verify = lambda cur: all(any(r.get("xkkh") == x and zdbk_text(r.get("xkzy")) == str(i) for r in cur)
                                 for i, x in enumerate(ids, 1))
        if confirm(a.yes):
            xk_report(zb.xk_write("reorder", "xkBcZypxZzxkGhb",
                                  {"xkkhs": ",".join(ids), "zypxs": ",".join(str(i) for i in range(1, len(ids) + 1))}, verify))


def xk_report(res: dict):
    label = {"saved": "已確認寫入（回查志願列表）", "rejected": "教務網拒絕", "unknown": "結果不明"}[res["status"]]
    print(f"[{label}] {res['message'] or ''}".rstrip())
    if res["error"]:
        print(f"  {res['error']}")
    if res["status"] != "saved":
        raise ZjuError(f"不要直接重送：先跑 zju zdbk xk selected 回查；紀錄在 {ZDBK_JOURNAL}")


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
        start = dt.date.fromisoformat(a.arg) if a.arg else today_cst()
        for d, s in range_subs(z, start, a.days or 1):
            print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}"
                  f"\t{class_span(s)}\t{s.get('room') or '-'}")
    elif a.action in ("add", "rm"):
        ids = [a.arg, *a.more] if a.arg else []
        if not ids or not all(v.isdigit() for v in ids):
            raise ZjuError(f"用法：classroom {a.action} <course_id>...（course_id 用 classroom search 查）")
        cfg = load_config()
        tracked = cfg.setdefault("tracked_courses", {})
        ignored = cfg.setdefault("ignored_courses", [])
        for cid in ids:
            if a.action == "rm":
                name = tracked.pop(cid, None)
                if name:
                    print(f"[移除] {cid} {name}")
                elif cid not in ignored:  # 不是 add 加的 → 課表本身的課，記進忽略名單
                    ignored.append(cid)
                    print(f"[忽略] {cid}（課表的課，之後 --days 跳過；classroom add 可恢復）")
                else:
                    print(f"[已忽略] {cid}")
                continue
            if cid in ignored:
                ignored.remove(cid)
                print(f"[取消忽略] {cid}")
            subs = z.course_subs(int(cid))  # 順便驗證 id 存在
            name = f"{subs[0]['course_name']} {subs[0]['lecturer']}" if subs else cid
            tracked[cid] = name
            print(f"[追蹤] {cid} {name}（{len(subs)} 堂）")
        save_config(cfg)
    elif a.action == "tracked":
        for cid, name in tracked_courses().items():
            print(f"{cid}\t{name}")
        for cid in sorted(ignored_courses()):
            print(f"{cid}\t(忽略)")


def today_subs(z: Zju) -> list[dict]:
    """當日課表一天只抓一次（排程每幾分鐘跑一次，別每次都打智雲）。"""
    today = today_cst().isoformat()
    try:
        cache = json.loads(SCHEDULE_FILE.read_text())
        if cache.get("day") == today:
            return cache["subs"]
    except (OSError, ValueError, KeyError):
        pass
    subs = z.day_subs(today_cst())  # 只看課表的課：旁聽課不點名
    state_dir()
    SCHEDULE_FILE.write_text(json.dumps({"day": today, "subs": subs}, ensure_ascii=False))
    return subs


def cmd_rollcall(a):
    z = Zju()
    if a.in_class:
        now_subs = in_class(today_subs(z), time.time(), a.margin)
        if not now_subs:
            log("現在不是上課時段，略過")
            if a.json:
                print("[]")
            return
        log("上課中：" + "、".join(f"{s['course_name']} {class_span(s)}" for s in now_subs))
    rs = z.rollcalls()
    if a.json:
        print(json.dumps(rs, ensure_ascii=False, indent=1))
        return
    if not rs:
        log("沒有進行中的簽到")
    for r in rs:
        signed = "已簽" if r.get("status") == "on_call_fine" else "未簽"
        print(f"{r.get('rollcall_id')}\t{r.get('course_title', '')}\t{rollcall_kind(r)}\t{signed}"
              f"\t{r.get('created_by_name', '')}")


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
    y.add_argument("--mine", action="store_true", help="只看自己的發帖與回帖")
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

    x = sp.add_parser("rollcall", help="進行中的簽到（只查看、不代簽）")
    x.add_argument("--in-class", action="store_true", help="只在今天課表的上課時段才查（給排程用）")
    x.add_argument("--margin", type=int, default=10, metavar="MIN", help="上課前後各放寬幾分鐘（預設 10）")
    x.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_rollcall)

    x = sp.add_parser("zdbk", help="本科教學管理信息服務平台：grades / plan / xk")
    zp = x.add_subparsers(dest="zcmd", required=True)
    y = zp.add_parser("grades", help="全部成績、各學期與總 GPA、主修 GPA、已獲學分；出國／國內、五分／4.3 可切換")
    y.add_argument("--scope", choices=["abroad", "domestic"], default="abroad",
                   help="abroad：掛科後重修過的只算重修那次（預設）；domestic：校內規則，重修每次都計入")
    y.add_argument("--scale", choices=["5", "4.3"], default="4.3", help="績點分制（預設 4.3，2022 級起出國用）")
    y.add_argument("--term", help="只看某學年或學期，如 2025-2026 或 2025-2026-1（1 秋冬、2 春夏）")
    y.add_argument("--json", action="store_true")
    y = zp.add_parser("plan", help="培養方案：預設本人方案並下載完整 PDF")
    y.add_argument("--major", help="看別的專業（名稱片段）")
    y.add_argument("--year", help="入學年級，如 2024（預設本人年級）")
    y.add_argument("--list", action="store_true", help="列出該年級全部方案（可配 --college/--major 篩）")
    y.add_argument("--college", help="--list 時按學院名篩")
    y.add_argument("--text", action="store_true", help="順便印出 PDF 文字（需要 pdftotext）")
    y.add_argument("--json", action="store_true")
    y = zp.add_parser("xk", help="自主選課：context / selected / credits / search / sections / select / drop / reorder")
    y.add_argument("action", choices=["context", "selected", "credits", "search", "sections", "select", "drop", "reorder"])
    y.add_argument("arg", nargs="?", help="search/sections：關鍵字；select/drop：教學班號；reorder：依序列出教學班號")
    y.add_argument("more", nargs="*", help=argparse.SUPPRESS)
    y.add_argument("--field", default="kcmc", choices=["kcmc", "kcdm", "jsxm", "kclb", "sksj", "skdd", "kkxy"],
                   help="search/sections 搜哪個欄位（課名 kcmc、課程代碼 kcdm、教師 jsxm…，預設課名）")
    y.add_argument("--available", action="store_true", help="只看有餘量的")
    y.add_argument("--teacher", help="sections 只列這位教師的班")
    y.add_argument("--gpa", action="store_true", help="selected 也查每門課各老師的查老師均績（每門課一次請求）")
    y.add_argument("--yes", action="store_true", help="select/drop/reorder 不再詢問直接送出")
    y.add_argument("--json", action="store_true")
    x.set_defaults(fn=cmd_zdbk)

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

#!/usr/bin/env -S uv run --quiet --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["requests", "img2pdf", "pillow", "keyring"]
# ///
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 8eoyw
# Portions ported from PeiPei233/zju-learning-assistant, Copyright (c) 2023 PeiPei233 (MIT).
"""學在浙大 / 智雲課堂 命令列工具。

API 邏輯移植自 PeiPei233/zju-learning-assistant (ZLA) 的 src-tauri/src/zju_assist.rs，
改成可腳本化、可排程、可被 AI agent 直接呼叫的單檔 CLI。

  zju.py login                         # 首次：存學號，密碼進 macOS Keychain
  zju.py courses [--all]               # 學在浙大課程列表
  zju.py sync [課程...] [--dry-run]     # 增量同步課件（含老師未開放下載的 preview）
  zju.py todo                          # 待辦
  zju.py classroom search 關鍵字        # 智雲課堂找課（id 與學在浙大不同）
  zju.py classroom subs <cid>          # 列出每堂課
  zju.py classroom day [日期] [--days N]
  zju.py ppt --course <cid> | --days N # 智雲 PPT 截圖合併 PDF
  zju.py transcript --course <cid> | --days N [--format txt|srt|md]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pickle
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
from urllib.parse import unquote, urlparse

import ssl

import requests
from requests.adapters import HTTPAdapter

KEYCHAIN_SERVICE = "zju-learning"
STATE_DIR = Path.home() / ".config" / "zju-learning"
CONFIG_FILE = STATE_DIR / "config.json"
COOKIE_FILE = STATE_DIR / "cookies.pkl"
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


def safe_name(s: str) -> str:
    s = re.sub(r'[/\\:*?"<>|\n\r\t]', "_", str(s)).strip().strip(".")
    return s[:150] or "_"


# ---------------- config / credentials ----------------

def load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def save_config(cfg: dict):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
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
    """學在浙大 SSO 跳轉鏈上有主機用 1024-bit DH，OpenSSL 3 預設拒絕（DH_KEY_TOO_SMALL）。"""

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


class Zju:
    def __init__(self):
        self.jar = requests.cookies.RequestsCookieJar()  # 各執行緒 session 共用（CookieJar 自帶鎖）
        self._tl = threading.local()
        self.logged_in = False
        if COOKIE_FILE.exists():
            try:
                self.jar.update(pickle.loads(COOKIE_FILE.read_bytes()))
            except Exception:
                pass

    @property
    def s(self) -> requests.Session:
        """每個執行緒一個 session：連線池不互搶，trust_env 切換也不會互相干擾。"""
        if not hasattr(self._tl, "s"):
            s = requests.Session()
            s.mount("https://", LegacyTLS(pool_connections=8, pool_maxsize=8))
            s.headers["User-Agent"] = UA
            s.cookies = self.jar
            self._tl.s = s
        return self._tl.s

    # 預設直連（浙大站走 Clash 只會多一跳），連不上才退環境 proxy
    def req(self, method: str, url: str, **kw) -> requests.Response:
        kw.setdefault("timeout", TIMEOUT)
        last = None
        for attempt in range(4):
            self.s.trust_env = attempt % 2 == 1
            try:
                return self.s.request(method, url, **kw)
            except (requests.ConnectionError, requests.Timeout) as e:
                last = e
                time.sleep(0.3 * 2 ** attempt)
        raise ZjuError(f"連線失敗：{url}\n{last}")

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, **kw):
        return self.req("POST", url, **kw)

    def save_cookies(self):
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = COOKIE_FILE.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            pickle.dump(self.jar, f)
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
        self.get("https://tgmedia.cmc.zju.edu.cn/index.php?r=auth/login&auType=cmc&tenant_code=112"
                 "&forward=https%3A%2F%2Fclassroom.zju.edu.cn%2F")
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
            dom = (c.domain or "").lstrip(".")
            if not ("classroom.zju.edu.cn" == dom or "classroom.zju.edu.cn".endswith("." + dom)):
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
            j = self.json(self.post("https://courses.zju.edu.cn/api/my-courses", json={
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

    def upload_response(self, uid: int, rid: int) -> tuple[requests.Response, str]:
        """三層退路，回 (response, 來源)：
        1. reference blob — 正常下載
        2. upload blob — 老師關了下載仍給原格式（ZLA / eWloYW8 / xzzd-pro 的做法）
        3. 預覽器的轉檔 PDF — document/{rid}/url?preview=true 回 {url}（Kcalb35 / fish-can 的做法）
        活動未開放時三層都 403，這是伺服器權限，不繞。"""
        base = "https://courses.zju.edu.cn/api/uploads"
        r = self.get(f"{base}/reference/{rid}/blob", stream=True)
        if r.ok:
            return r, "下載"
        r.close()
        r = self.get(f"{base}/{uid}/blob", stream=True)
        if r.ok:
            return r, "原檔"
        r.close()
        code = r.status_code
        r = self.get(f"{base}/reference/document/{rid}/url", params={"preview": "true"})
        if r.ok:
            try:
                url = r.json().get("url")
            except ValueError:
                url = None
            if url:
                r = self.get(url, stream=True)
                if r.ok:
                    return r, "預覽PDF"
                code = r.status_code
        raise ZjuError(f"下載失敗 HTTP {code}")

    def todos(self) -> list[dict]:
        self.ensure()
        return self.json(self.get("https://courses.zju.edu.cn/api/todos?no-intercept=true"), "todos").get("todo_list", [])

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
                        subs.append({"course_id": course_id, "course_name": data["title"],
                                     "sub_id": int(s["id"]), "sub_name": s["sub_title"],
                                     "lecturer": s.get("lecturer_name", "")})
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
                "course_id": course_id, "sub_id": sub_id, "page": page, "per_page": 100}), "search-ppt")
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


def stream_to(r: requests.Response, dest: Path) -> Path:
    """寫暫存檔再 rename，中斷不會留下半截檔被當成已下載。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".part-")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                f.write(chunk)
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
        return dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone().strftime(fmt)
    except ValueError:
        return iso


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
            lines += [str(i), f"{fmt_ts(c['BeginSec'])} --> {fmt_ts(c['EndSec'])}", c.get("Text", ""), ""]
    elif fmt == "md":
        lines.append(f"# {title}\n")
        for c in items:
            lines.append(f"**[{fmt_ts(c['BeginSec'], False)}]** {c.get('Text', '')}  ")
    else:
        for c in items:
            lines.append(f"[{fmt_ts(c['BeginSec'], False)}] {c.get('Text', '')}")
    return "\n".join(lines) + "\n"


def images_to_pdf(paths: list[Path], pdf: Path):
    import img2pdf
    from PIL import Image

    fixed = []
    for p in paths:
        try:
            img2pdf.convert(str(p))  # 能直接嵌入就不重新編碼（無損、省記憶體）
            fixed.append(str(p))
        except Exception:
            q = p.with_suffix(".conv.jpg")
            with Image.open(p) as im:
                im.convert("RGB").save(q, quality=92)
            fixed.append(str(q))
    tmp = pdf.with_name(".part-" + pdf.name)
    tmp.write_bytes(img2pdf.convert(fixed))
    os.replace(tmp, pdf)


def resolve_subs(z: Zju, a) -> list[dict]:
    if a.course:
        subs = z.course_subs(a.course)
        if a.sub:
            subs = [s for s in subs if s["sub_id"] in a.sub]
        return subs
    days = a.days or 1
    today = dt.date.today()
    subs = []
    for i in range(days):
        subs += z.day_subs(today - dt.timedelta(days=i))
    return subs


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
        seen: set[str] = set()
        for a_, u in items:
            act = a_.get("title", "")
            uid, rid = u["id"], u.get("reference_id") or u["id"]
            key = f"{c['id']}:{uid}"
            name = safe_name(u.get("name") or f"{uid}")
            if name in seen:  # 同課程同名檔：加 id 區分
                stem, dot, ext = name.rpartition(".")
                name = f"{stem} ({uid}).{ext}" if dot else f"{name} ({uid})"
            seen.add(name)
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

    def fetch(job):
        key, uid, rid, dest, _ = job
        r, preview = z.upload_response(uid, rid)
        return stream_to(r, dest), preview

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
            except Exception as e:
                if a_.get("is_started") is False and "HTTP 403" in str(e):
                    # 老師排程開放：伺服器對所有端點都 403（不繞），開放後下次 sync 自動抓
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


def cmd_classroom(a):
    z = Zju()
    if a.action == "search":
        for c in z.classroom_search(a.arg or "", a.teacher or ""):
            print(f"{c.get('course_id')}\t{c.get('title')}\t{c.get('realname')}")
    elif a.action == "subs":
        if not a.arg:
            raise ZjuError("用法：classroom subs <course_id>")
        for s in z.course_subs(int(a.arg)):
            print(f"{s['sub_id']}\t{s['sub_name']}\t{s['lecturer']}")
    elif a.action == "day":
        start = dt.date.fromisoformat(a.arg) if a.arg else dt.date.today()
        for i in range(a.days or 1):
            d = start - dt.timedelta(days=i)
            for s in z.day_subs(d):
                print(f"{d}\t{s['course_id']}\t{s['sub_id']}\t{s['course_name']}\t{s['sub_name']}\t{s['lecturer']}")


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
            p = tmpdir / f"{i:04d}{Path(urlparse(u).path).suffix or '.jpg'}"
            for attempt in range(5):
                r = z.get(u)
                if r.ok and r.content:
                    p.write_bytes(r.content)
                    return p
                time.sleep(0.2 * 2 ** attempt)
            raise ZjuError(f"PPT 圖下載失敗：{u}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            paths = list(pool.map(grab, enumerate(urls)))  # map 保序 = 頁序
        cdir.mkdir(parents=True, exist_ok=True)
        images_to_pdf(paths, pdf)
        if a.keep_images:
            shutil.copytree(tmpdir, cdir / safe_name(s["sub_name"]), dirs_exist_ok=True)
        print(f"[PDF] {pdf.relative_to(root)}（{len(paths)} 頁）")
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
    default_out = os.environ.get("ZJU_OUT") or load_config().get("out") or str(DEFAULT_OUT)
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

    x = sp.add_parser("classroom", help="智雲課堂：search / subs / day")
    x.add_argument("action", choices=["search", "subs", "day"])
    x.add_argument("arg", nargs="?")
    x.add_argument("--teacher")
    x.add_argument("--days", type=int)
    x.set_defaults(fn=cmd_classroom)

    for name, fn in (("ppt", cmd_ppt), ("transcript", cmd_transcript)):
        x = sp.add_parser(name, help="智雲 PPT → PDF" if name == "ppt" else "智雲課堂語音轉錄")
        x.add_argument("--course", type=int, help="智雲課堂 course_id（classroom search 查）")
        x.add_argument("--sub", type=int, nargs="*", help="只抓這些 sub_id")
        x.add_argument("--days", type=int, help="不給 --course 時：最近 N 天的課（預設 1 = 今天）")
        x.add_argument("--force", action="store_true", help="已存在也重抓")
        if name == "ppt":
            x.add_argument("--keep-images", action="store_true")
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

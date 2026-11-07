"""不連網的單元測試：python -m unittest discover tests（需 requests / img2pdf / pillow / numpy）。"""
import importlib.util
import tempfile
import unittest

import requests
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("zju", ROOT / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)


class Offline(unittest.TestCase):
    def test_rsa_no_padding_roundtrip(self):
        # 與 CAS 前端同一套 textbook RSA：m^e mod n，hex 輸出
        p, q, e = 1000000007, 998244353, 65537
        n = p * q
        d = pow(e, -1, (p - 1) * (q - 1))
        pwd = "abc"
        enc = format(pow(int.from_bytes(pwd.encode(), "big"), e, n), "x")
        self.assertEqual(pow(int(enc, 16), d, n).to_bytes(3, "big").decode(), pwd)

    def test_safe_name(self):
        self.assertEqual(zju.safe_name('a/b:c*?"<>|'), "a_b_c______")
        self.assertEqual(zju.safe_name("..."), "_")

    def test_dedup_slides(self):
        from PIL import Image, ImageDraw

        def slide(lines, ink=None, bg=255):
            im = Image.new("RGB", (1280, 720), (bg,) * 3)
            d = ImageDraw.Draw(im)
            for x, y, w in lines:  # 一行字 ≈ 一條細橫線
                d.rectangle([x, y, x + w, y + 8], fill=(0, 0, 0))
            if ink:  # 老師的紅筆註記
                d.line(ink, fill=(220, 0, 0), width=5)
            return im

        a_title = [(100, 60, 600)]
        a_full = a_title + [(120, 200, 800), (120, 280, 700), (120, 360, 900)]
        b = [(100, 60, 400), (150, 250, 500), (150, 450, 600), (300, 600, 300)]
        scribble = [(700, 300), (900, 420), (1100, 300), (900, 200)]
        pages = [
            slide(a_title),           # 0 動畫第一步 → 被 1 包含
            slide(a_full),            # 1 保留
            slide(b),                 # 2 → 被 3（寫了註記）包含
            slide([], bg=0),          # 3 全黑過場
            slide(b, ink=scribble),   # 4 保留
            slide(b),                 # 5 擦掉註記的乾淨版 → 被 4 包含
            slide(a_full),            # 6 翻回前面 → 被 1 包含
        ]
        with tempfile.TemporaryDirectory() as d:
            paths = []
            for i, im in enumerate(pages):
                p = Path(d) / f"{i:04d}.jpg"
                im.save(p, quality=85)
                paths.append(p)
            kept = zju.dedup_slides(paths)
        self.assertEqual([p.stem for p in kept], ["0001", "0004"])

    def test_srt(self):
        out = zju.render_transcript([{"BeginSec": 61.5, "EndSec": 63, "Text": "你好"}], "srt", "t")
        self.assertEqual(out, "1\n00:01:01,500 --> 00:01:03,000\n你好\n\n")

    def test_local_time(self):
        self.assertEqual(zju.local_time(None), "時間未定")
        self.assertRegex(zju.local_time("2026-09-27T15:59:00Z", "%Y-%m-%d"), r"2026-09-2[78]")

    def test_current_year(self):
        cs = [{"academic_year_id": 15, "is_closed": False, "id": 1},
              {"academic_year_id": 14, "is_closed": False, "id": 2},
              {"academic_year_id": 15, "is_closed": True, "id": 3}]
        self.assertEqual([c["id"] for c in zju.current_year(cs)], [1])

    def test_images_to_pdf(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            Image.new("RGBA", (40, 30), (255, 0, 0, 128)).save(d / "a.png")  # alpha：走 Pillow 轉檔分支
            Image.new("RGB", (40, 30)).save(d / "b.jpg")
            zju.images_to_pdf([d / "a.png", d / "b.jpg"], d / "out.pdf")
            self.assertEqual((d / "out.pdf").read_bytes()[:5], b"%PDF-")


    def test_safe_name_windows(self):
        self.assertEqual(zju.safe_name("CON.pdf"), "_CON.pdf")
        self.assertEqual(zju.safe_name("x . "), "x")

    def test_local_time_naive_is_beijing(self):
        # 沒帶時區 = 北京時間；換算成 UTC+8 顯示應不變
        import datetime as dt
        naive = zju.local_time("2026-09-27 23:59:00", "%Y-%m-%d %H:%M")
        want = dt.datetime(2026, 9, 27, 23, 59, tzinfo=zju.CST).astimezone().strftime("%Y-%m-%d %H:%M")
        self.assertEqual(naive, want)

    def test_secure_url(self):
        self.assertEqual(zju.secure_url("http://video.cmc.zju.edu.cn/a.jpg"), "https://video.cmc.zju.edu.cn/a.jpg")
        self.assertEqual(zju.secure_url("http://example.com/a.jpg"), "http://example.com/a.jpg")

    def test_http_never_carries_cookies(self):
        """.zju.edu.cn 的 SSO cookie 沒設 Secure：http:// 請求必須被剝掉 Cookie。"""
        from unittest import mock
        import requests
        seen = {}

        def fake_send(self_, request, **kw):
            seen["headers"] = dict(request.headers)
            r = requests.Response()
            r.status_code = 200
            r._content = b"ok"
            r.url = request.url
            r.request = request
            return r

        z = zju.Zju.__new__(zju.Zju)
        z.jar = requests.cookies.RequestsCookieJar()
        z.jar.set("iPlanetDirectoryPro", "SECRET", domain=".zju.edu.cn", path="/")
        import threading
        z._tl = threading.local()
        with mock.patch.object(requests.adapters.HTTPAdapter, "send", fake_send):
            z.s.get("http://video.cmc.zju.edu.cn/x.jpg")
            self.assertNotIn("Cookie", seen["headers"])
            z.s.get("https://courses.zju.edu.cn/api/x")
            self.assertIn("SECRET", seen["headers"].get("Cookie", ""))

    def test_refer_params(self):
        # 與前端 getDownloadRefer 同規則：classroom→classroom_activity、exam 不帶、其餘→learning_activity
        self.assertEqual(zju.refer_params({"id": 1164596, "type": "online_video"}),
                         {"refer_id": 1164596, "refer_type": "learning_activity"})
        self.assertEqual(zju.refer_params({"id": 1159399, "type": "material"}),
                         {"refer_id": 1159399, "refer_type": "learning_activity"})
        self.assertEqual(zju.refer_params({"id": 7, "type": "classroom"}),
                         {"refer_id": 7, "refer_type": "classroom_activity"})
        self.assertIsNone(zju.refer_params({"id": 7, "type": "exam"}))
        self.assertIsNone(zju.refer_params({"type": "material"}))  # 缺 id
        self.assertIsNone(zju.refer_params(None))

    def test_upload_response_refer_fallback(self):
        """排程未開放：前兩層 403、第 3 層帶 snake_case refer 回原檔，不再落到 preview PDF。"""
        import io
        import requests
        from unittest import mock

        calls = []

        def fake_get(self_, url, **kw):
            calls.append((url, kw.get("params")))
            ok = bool((kw.get("params") or {}).get("refer_id"))
            r = requests.Response()
            r.raw = io.BytesIO(b"x" * 10 if ok else b"")
            r.status_code = 200 if ok else 403
            r.headers["Content-Type"] = "application/octet-stream"
            if ok:
                r.headers["Content-Length"] = "10"
            return r

        z = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(zju.Zju, "get", fake_get):
            r, src = z.upload_response(2316787, 17939358, {"id": 1164596, "type": "online_video"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(src, "排程原檔")
        self.assertEqual(len(calls), 3)  # refer 成功就不需要 preview PDF
        self.assertTrue(calls[0][0].endswith("/reference/17939358/blob"))
        self.assertEqual(calls[2], ("https://courses.zju.edu.cn/api/uploads/2316787/blob",
                                    {"refer_id": 1164596, "refer_type": "learning_activity"}))

    def test_upload_response_no_refer_for_exam(self):
        """exam 活動不帶 refer：只走原三層，exam 未開放時照樣 403 拋錯。"""
        import io
        import requests
        from unittest import mock

        calls = []

        def fake_get(self_, url, **kw):
            calls.append((url, kw.get("params")))
            r = requests.Response()
            r.raw = io.BytesIO(b"")
            r.status_code = 403
            return r

        z = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(zju.Zju, "get", fake_get):
            with self.assertRaises(zju.DownloadError) as cm:
                z.upload_response(1, 2, {"id": 9, "type": "exam"})
        self.assertEqual(len(calls), 3)  # reference blob、uid blob、preview url（沒有 refer 層）
        self.assertTrue(all("refer_id" not in (p or {}) for _, p in calls))
        self.assertEqual(cm.exception.codes, [403, 403, 403])

    def test_stream_to_rejects_truncated(self):
        import io
        import requests

        def resp(body, length=None, ctype="application/octet-stream"):
            r = requests.Response()
            r.status_code = 200
            r.raw = io.BytesIO(body)
            r.headers["Content-Type"] = ctype
            if length is not None:
                r.headers["Content-Length"] = str(length)
            return r

        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b"abc", 10), d / "a.bin")
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b""), d / "b.bin")
            with self.assertRaises(zju.ZjuError):
                zju.stream_to(resp(b"<html>", ctype="text/html; charset=utf-8"), d / "c.pdf")
            with self.assertRaises(zju.TooBig):
                zju.stream_to(resp(b"x" * 10, 10), d / "d.bin", limit=5)
            self.assertEqual(sorted(p.name for p in d.iterdir()), [])  # 失敗不留任何檔
            out = zju.stream_to(resp(b"%PDF-1.4 ok", 11), d / "e.pptx")
            self.assertEqual(out.name, "e.pptx.pdf")


    def test_in_class(self):
        # 北京時間 2026-10-08 13:25–15:50 一堂（主機時區不影響）
        b = int(zju.dt.datetime(2026, 10, 8, 13, 25, tzinfo=zju.CST).timestamp())
        subs = [{"sub_id": 1, "begin": b, "over": b + 145 * 60}, {"sub_id": 2, "begin": 0, "over": 0}]
        self.assertEqual(zju.class_span(subs[0]), "13:25-15:50")
        self.assertEqual(zju.class_span(subs[1]), "-")
        self.assertEqual([s["sub_id"] for s in zju.in_class(subs, b - 5 * 60)], [1])  # 上課前 5 分
        self.assertEqual(zju.in_class(subs, b - 11 * 60), [])
        self.assertEqual([s["sub_id"] for s in zju.in_class(subs, b + 150 * 60)], [1])  # 下課後 5 分
        self.assertEqual(zju.in_class(subs, b + 150 * 60, margin=0), [])

    def test_day_subs_times(self):
        from unittest import mock
        import requests
        r = requests.Response()
        r.status_code = 200
        r._content = ('{"list":{"0":{"course":[{"id":"86830","title":"NLP","sub_id":"1976778",'
                      '"sub_title":"第9-10节","realname":"T","course_begin":"1791447300",'
                      '"course_over":"1791453000"}]}}}').encode()
        z = zju.Zju.__new__(zju.Zju)
        z.logged_in = True
        with mock.patch.object(zju.Zju, "get", lambda *a, **kw: r), \
                mock.patch.object(zju.Zju, "bearer", lambda self_: {}):
            s, = z.day_subs(zju.dt.date(2026, 10, 8))
        self.assertEqual((s["begin"], s["over"]), (1791447300, 1791453000))
        self.assertEqual(zju.class_span(s), "16:15-17:50")

    def test_rollcall_in_class_skips_api(self):
        """不在上課時段：不打簽到 API；簽到只用 GET 列表，不送任何簽到請求。"""
        import argparse
        import io
        from contextlib import redirect_stdout
        from unittest import mock
        z = zju.Zju.__new__(zju.Zju)
        z.logged_in = True
        a = argparse.Namespace(in_class=True, margin=10, json=True)
        with mock.patch.object(zju, "Zju", return_value=z), \
                mock.patch.object(zju, "today_subs", return_value=[]), \
                mock.patch.object(z, "req") as req, redirect_stdout(io.StringIO()) as out:
            zju.cmd_rollcall(a)
        req.assert_not_called()
        self.assertEqual(out.getvalue(), "[]\n")

        import requests
        r = requests.Response()
        r.status_code = 200
        r._content = ('{"rollcalls":[{"rollcall_id":9,"course_title":"NLP","is_number":true,'
                      '"is_radar":false,"status":"absent","created_by_name":"T"}]}').encode()
        a.in_class = a.json = False
        with mock.patch.object(zju, "Zju", return_value=z), \
                mock.patch.object(z, "req", return_value=r) as req, redirect_stdout(io.StringIO()) as out:
            zju.cmd_rollcall(a)
        self.assertEqual([c.args[0] for c in req.call_args_list], ["GET"])
        self.assertEqual(out.getvalue(), "9\tNLP\t數字\t未簽\tT\n")

    def test_today_cst(self):
        import datetime as dt
        self.assertEqual(zju.today_cst(), dt.datetime.now(zju.CST).date())

    def test_zdbk_grade_rules(self):
        row = lambda cj, xf, jd, kc="X": {"xkkh": f"(2025-2026-1)-{kc}-0001-1", "kcmc": kc, "cj": cj, "xf": xf, "jd": jd}
        cs = [zju.grade_row(r) for r in (row("90", "2", "4.5"), row("不及格", "4", "0", "Y"), row("弃修", "3", "0", "Z"),
                                         row("合格", "1.5", "3", "W"), row("中", "1", "2.4", "V"))]
        self.assertEqual([c["in_gpa"] for c in cs], [True, True, False, False, True])
        self.assertEqual((cs[0]["year"], cs[0]["term"], cs[0]["code"]), ("2025-2026", "1", "X"))
        self.assertEqual(zju.gpa_of(cs), (round((2 * 4.5 + 1 * 2.4) / 7, 3), 7.0))
        self.assertEqual([zju.passed(c) for c in cs], [True, False, False, True, True])

    def test_zdbk_retaken_and_makeup(self):
        rows = [{"xkkh": "(2024-2025-2)-M1-0001-2", "kcmc": "M", "cj": "55", "xf": "2", "jd": "0.0"},
                {"xkkh": "(2025-2026-1)-M1", "kcmc": "M", "cj": "不及格", "xf": "2", "jd": "0.0"},
                {"xkkh": "(2025-2026-2)-M1-0002-2", "kcmc": "M", "cj": "72", "xf": "2", "jd": "2.7"},
                {"xkkh": "(2025-2026-2)-E1-0003-1", "kcmc": "E", "cj": "不及格", "xf": "4", "jd": "0.0"}]
        cs = zju.mark_retaken([zju.grade_row(r) for r in rows])
        self.assertEqual(cs[1]["remark"], "補考")
        self.assertEqual((cs[1]["year"], cs[1]["code"]), ("2025-2026", "M1"))
        self.assertEqual([c["retaken"] for c in cs], [True, True, False, False])  # 還沒重修的掛科照算 0
        self.assertEqual(zju.gpa_view(cs, "abroad", "5"), (round(2 * 2.7 / 6, 3), 6.0))
        self.assertEqual(zju.gpa_view(cs, "domestic", "5"), (round(2 * 2.7 / 10, 3), 10.0))

    def test_zdbk_43_scale(self):
        # 《浙江大学本科课程成绩评定与管理办法》：86 分以上才不同，3.9 以下兩制相同
        self.assertEqual([zju.to_43(p) for p in (5.0, 4.8, 4.5, 4.2, 3.9, 1.5, 0.0, None)],
                         [4.3, 4.2, 4.1, 4.0, 3.9, 1.5, 0.0, None])
        cs = [zju.grade_row({"xkkh": "(2025-2026-1)-A-1-1", "kcmc": "A", "cj": "95", "xf": "2", "jd": "5.0"}),
              zju.grade_row({"xkkh": "(2025-2026-1)-B-1-1", "kcmc": "B", "cj": "80", "xf": "1", "jd": "3.6"})]
        self.assertEqual(zju.gpa_view(zju.mark_retaken(cs), "abroad", "4.3"), (round((2 * 4.3 + 3.6) / 3, 3), 3.0))

    def test_chalaoshi_match(self):
        ch = zju.Chalaoshi(None)
        ch._idx = {"colleges": {"1": "信息与电子工程学院", "2": "数学科学学院"},
                   "teachers": [{"id": 1, "name": "张三", "xy": 1, "rate": "9.1"}, {"id": 2, "name": "张三", "xy": 2, "rate": "3.0"},
                                {"id": 3, "name": "JOHNSMITH", "xy": 1, "rate": "N/A"}, {"id": 4, "name": "李四", "xy": 1, "rate": "8.0"}]}
        ch._gpa["电路"] = {"李四": {"gpa": 3.5, "sd": 1.0, "n": "50+"}}
        ts = ch.teachers({"jsxm": "张三<br>JOHN SMITH<br>李四/[极限飞盘]<br>王五"}, "电路", "信息与电子工程学院")
        self.assertEqual([t.get("url", "").split("/")[-2:-1] for t in ts[:3]], [["1"], ["3"], ["4"]])
        self.assertIsNone(ts[1]["rate"])
        self.assertEqual(zju.teacher_label(ts[2]), "李四 8.0分 均績3.5±1（50+人）")
        self.assertEqual(zju.teacher_label(ts[3]), "王五（查老師無資料）")
        self.assertEqual(ch.teachers({"jsxm": "张三"})[0]["same_name"], 2)  # 沒給學院分不出同名
        self.assertEqual(zju.pending_label({"rs": "10/60", "yxrs": "3~25"}), "餘量/容量 10/60｜待定 本專業 3／全部 25（2.50 進 1）")
        self.assertEqual(zju.pending_label({"rs": "-4/60", "yxrs": "0~0"}), "餘量/容量 -4/60｜待定 本專業 0／全部 0（已滿）")

    def test_zdbk_expired(self):
        def resp(code, loc="", ctype="text/html", body=""):
            r = requests.Response()
            r.status_code, r._content = code, body.encode()
            r.headers.update({"Location": loc, "Content-Type": ctype} if loc else {"Content-Type": ctype})
            return r
        self.assertTrue(zju.Zdbk.expired(resp(901)))
        self.assertTrue(zju.Zdbk.expired(resp(302, "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_slogin.html")))
        self.assertFalse(zju.Zdbk.expired(resp(302, "https://zdbk.zju.edu.cn/jwglxt/xtgl/login_ssologin.html?ticket=ST-1")))
        self.assertTrue(zju.Zdbk.expired(resp(200, body='<form id="loginForm">')))
        self.assertFalse(zju.Zdbk.expired(resp(200, ctype="application/pdf", body='id="loginForm"')))

    def test_zdbk_xk_context(self):
        page = ('<input id="xn" value="2026-2027"><input id="xq" value="2"><input id="nj" value="2023">'
                '<input id="zydm" value="0801"><input id="xkzys" value="3"><!-- <input id="xn" value="1999-2000"> -->'
                '<li><a onclick="categroyClick(this,\'xk_1\',\'bl\')">本类(专业)</a></li>')
        zb = zju.Zdbk.__new__(zju.Zdbk)
        zb._ctx = None
        zb.req = lambda *a, **kw: type("R", (), {"text": page})()
        c = zb.xk_context()
        self.assertEqual((c["xnxq"], c["jxjhh"], c["xkzys"]), ("(2026-2027-2)-", "20230801", "3"))
        self.assertEqual(c["categories"], [{"dl": "xk_1", "lx": "bl", "name": "本类(专业)"}])
        self.assertEqual(zju.xk_credit({"kcxx": "1~4.0~3.5-1.0"}), "4.0")

if __name__ == "__main__":
    unittest.main()

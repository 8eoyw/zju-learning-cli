"""不連網的單元測試：python -m unittest discover tests（需 requests / img2pdf / pillow）。"""
import importlib.util
import tempfile
import unittest
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


if __name__ == "__main__":
    unittest.main()

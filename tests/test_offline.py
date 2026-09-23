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


if __name__ == "__main__":
    unittest.main()

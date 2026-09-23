@echo off & chcp 65001 >nul & set "PYTHONUTF8=1" & title ZJU login & py -3.11 -x "%~f0" %* & echo. & pause & goto :eof
"""雙擊執行的 `zju.py login`：每次都是全新的登入。

第一行是 cmd 的批次指令，`py -x` 會跳過它，所以從這裡開始才是 Python。
開始前先清掉上一次留下的學號（config.json）、密碼（系統憑證庫）和 session cookie，
再交給 zju.py 的 login 重新問學號和密碼。zju.py 本身不改。
"""
import importlib.util
import shutil
import sys
from pathlib import Path

here = Path(sys.argv[0]).resolve().parent
spec = importlib.util.spec_from_file_location("zju", here / "zju.py")
zju = importlib.util.module_from_spec(spec)
sys.modules["zju"] = zju
spec.loader.exec_module(zju)

old_user = zju.load_config().get("username")
if old_user:
    try:
        import keyring
        keyring.delete_password(zju.KEYCHAIN_SERVICE, old_user)
    except Exception:
        pass  # 本來就沒存密碼
shutil.rmtree(zju.STATE_DIR, ignore_errors=True)

sys.argv = ["zju.py", "login", *sys.argv[1:]]
zju.main()

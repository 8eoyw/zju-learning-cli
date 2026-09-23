#!/bin/bash
# 在 Finder 雙擊執行的 `zju.py login`（macOS 版的 zju_login.bat）：每次都是全新的登入。
#
# 開始前先清掉上一次留下的學號（config.json 的 username）、密碼（Keychain）和 session cookie，
# 再交給 zju.py 的 login 重新問學號和密碼。config.json 其他設定（例如輸出目錄 out）保留。
# zju.py 本身不改。

cd "$(dirname "$0")" || exit 1
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

state="${ZJU_STATE_DIR:-$HOME/.config/zju-learning}"
cfg="$state/config.json"

if [ -f "$cfg" ]; then
    old_user=$(plutil -extract username raw -o - "$cfg" 2>/dev/null)
    if [ -n "$old_user" ]; then
        security delete-generic-password -s zju-learning -a "$old_user" >/dev/null 2>&1  # 本來就沒存密碼也沒關係
        plutil -remove username "$cfg"
    fi
fi
rm -f "$state/cookies.json" "$state/cookies.pkl"

if command -v uv >/dev/null 2>&1; then
    uv run --quiet --script zju.py login "$@"
else
    python3 zju.py login "$@"
fi

echo
read -r -n 1 -s -p "按任意鍵關閉視窗…"
echo

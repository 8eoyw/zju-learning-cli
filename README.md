# zju-learning-cli

简体中文 | [繁體中文](README.zh-TW.md)

学在浙大（courses.zju.edu.cn）与智云课堂（classroom.zju.edu.cn）的单文件命令行工具：同步课件、把智云课堂的 PPT 截图合并成 PDF（可去除重复截图）、导出课堂语音转录、查待办。

它是 [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（ZLA）的 CLI 移植版。ZLA 是很好用的桌面 GUI，但没办法写进脚本、定时任务或让 AI agent 调用；这个项目把它的 API 逻辑改写成一个 Python 文件，补上增量同步、并行下载、PPT 去重，也修了几个上游的边界状况。

## 功能

| 指令 | 作用 |
| --- | --- |
| `zju login` | 设置学号，密码存进系统凭证库（macOS Keychain、Windows 凭据管理器；其他系统走 [keyring](https://pypi.org/project/keyring/)） |
| `zju courses [--all]` | 课程列表（默认只列最新学年） |
| `zju sync [课程...]` | 增量同步课件到 `<输出目录>/<课程>/` |
| `zju todo` | 待办事项，依截止时间排序（本地时区） |
| `zju classroom search 关键字` | 在智云课堂找课，取得 `course_id` |
| `zju classroom subs <course_id>` | 列出该课每一堂的 `sub_id` |
| `zju classroom day [日期] [--days N]` | 某天（或最近 N 天）自己的课 |
| `zju ppt --course <id> \| --days N [--dedup]` | 智云 PPT 截图 → `<课程>/智云PPT/<堂>.pdf` |
| `zju transcript --course <id> \| --days N` | 语音转录 → `<课程>/转录/<堂>.txt\|srt\|md` |

## 安装

需要 [uv](https://docs.astral.sh/uv/)。依赖写在文件头（PEP 723），第一次运行时 uv 会自动安装。

```bash
git clone https://github.com/8eoyw/zju-learning-cli.git
ln -s "$PWD/zju-learning-cli/zju.py" ~/.local/bin/zju   # 或直接 ./zju.py
zju login
```

没有 uv 的话：`pip install requests img2pdf pillow keyring numpy`，再用 `python zju.py ...` 运行。

也可以不开终端，直接双击登录脚本（每次都会清掉上一次的学号、密码和 cookie，重新登录）：

- macOS：`zju_login.command`，在访达里双击会打开一个终端窗口。用 git clone 下载的可以直接双击；下载 zip 的会被 Gatekeeper 拦下，先运行一次 `chmod +x zju_login.command && xattr -c zju_login.command`。
- Windows：`zju_login.bat`，需要 Python 启动器 `py -3.11`，并先用 pip 装好上面的依赖。

## 使用

```bash
zju sync --dry-run                 # 先看会下载什么、总共多大
zju sync                           # 最新学年全部课程
zju sync 微积分 123456              # 指定课程（名称片段或 id，id 用 zju courses 查）
zju sync --videos --max-size 0     # 连音视频和大文件一起抓
zju sync -j 8                      # 并行数（默认 4）

zju classroom day --days 7
zju ppt --days 1 --dedup           # 今天所有课的 PPT，重复截图只留最完整的一张
zju transcript --days 1 --format md
```

输出目录的优先级：`--out` > 环境变量 `ZJU_OUT` > `~/.config/zju-learning/config.json` 的 `"out"` > `~/ZJU-Courses`。

定时任务示例（cron，每天 22:00）：

```cron
0 22 * * * ~/.local/bin/zju sync && ~/.local/bin/zju ppt --days 1 && ~/.local/bin/zju transcript --days 1
```

退出码：`0` 成功；`1` 设置、登录或 API 错误；`2` 部分文件或堂次失败（其余照常完成）。

## 跟 ZLA 的差异

- **增量同步**：以 `.zju_manifest.json` 记录 upload id，而不是比对文件名和大小；老师换了新版（新 id）才会重抓。
- **下载完整性**：先写 `.part-*`，核对 `Content-Length` 后才 rename；空文件、截断、服务器回的 HTML 错误页都不会被记成已下载。
- **并行下载**：课件默认 4 个文件同时，PPT 截图 8 张同时；每条线程有自己的 session，共用 cookie jar。遇到 429/503 会照 `Retry-After` 退让。
- **PPT 去重**（`--dedup`）：智云是对投影画面定时截图，同一页会因动画逐步出现、老师边讲边写、翻回前面而被截很多次。一页的笔画若全都还在后面那页（或之前留下的某页）里就删掉，所以动画只留跑完的那张、手写只留写完的那张，批注不会丢。用局部对比找笔画，白底、黑底、底图纹理、教学视频都适用；实测三堂课 73→68、81→48、176→99 页，逐页核对无误删。`--keep-images` 仍保留全部原图。
- **默认直连**，连不上才改走系统 proxy；连接 timeout 6 秒，定时运行时不会卡死。只有幂等请求会自动重试，登录 POST 不会被重送。
- **学年判断**：学校常常不把旧课程标成已结束（`is_closed`），因此改用 `academic_year_id` 找最新学年。
- **同名课程**（不同教学班）分开存放；同一课程里的同名文件一律加上 id，命名不受 API 返回顺序影响。
- 默认跳过音视频文件和 200MB 以上的文件（通常是软件安装包、项目压缩包），`--dry-run` 会列出总大小。

修掉的上游边界状况：

- 智云 `search-ppt` 不遵守 `per_page`：常常第 1 页就返回全部，下一页再重复一次。ZLA 假设每页最多 100 张，超过 100 页的课会一直重试然后失败；这里改成依序去重。
- 转录 API 对「还没有语音数据」返回 `code=10002`，现在当成「无转录」处理，不再中止整批。
- **明文发送登录凭证**：智云 PPT 截图网址是 `http://`，而 `.zju.edu.cn` 的 SSO cookie（包括 `iPlanetDirectoryPro`）没有设 `Secure`，照常下载就会把登录凭证用明文送出。这里把学校主机的网址升级成 HTTPS，而且所有 `http://` 请求都不带 Cookie 和 Authorization。
- `courses.zju.edu.cn` 与 `identity.zju.edu.cn` 只支持 1024-bit DHE／静态 RSA，OpenSSL 3 默认拒绝连接（`DH_KEY_TOO_SMALL`）。`SECLEVEL=1` 只套用在这两台，其他主机维持默认的 TLS 设置。
- 智云的 `_token` cookie 设在 `.zju.edu.cn` 父网域，而不是 `classroom.zju.edu.cn`。

## 关闭下载的课件

老师关闭下载时，依序尝试三条路：

1. `/api/uploads/reference/{rid}/blob`：正常下载
2. `/api/uploads/{id}/blob`：多数情况仍然拿得到原格式（ZLA、[eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)、[xzzd-pro](https://github.com/xzzd-pro/xzzd-pro) 用的方式）
3. `/api/uploads/reference/document/{rid}/url?preview=true`：预览器转出来的 PDF（[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader) 用的方式）

**老师排定之后才开放的活动**，服务器对以上所有端点都会回 403。这是权限控制，本工具不会尝试绕过：这类文件会标成 `[未开放]（开放时间）`，不算失败，开放后下次 `sync` 会自动抓。

## 安全性

- 密码只存在系统凭证库。macOS 由系统 `security`、Windows 由 Python `getpass` 在终端提示输入，不会出现在命令行参数或 shell 历史记录。也可以改用环境变量 `ZJU_USER` / `ZJU_PASS`。
- 登录时密码先用 CAS 提供的公钥加密再送出，跟网页登录的做法相同。
- Session cookie 以 JSON（不是 pickle）缓存在 `~/.config/zju-learning/cookies.json`；在 macOS／Linux 上文件权限是 `0600`，目录是 `0700`。缓存位置可用环境变量 `ZJU_STATE_DIR` 覆盖。
- Cookie 只会通过 HTTPS 送往 `*.zju.edu.cn`，明文 `http://` 请求一律不带。没有任何遥测。
- TLS 验证失败会直接报错，不会自动重试或改走 proxy，避免把中间人攻击误当成网络不稳。

## 免责声明

在 macOS 和 Windows 上实测过；Linux 理论上可以使用，但没有测试过。

仅供个人学习使用。课件的著作权属于授课教师与学校，请勿散布下载的内容；使用时请遵守学校的相关规定，不要高频或大量抓取。学校的 API 没有公开文档，随时可能改版。

## 致谢

- [PeiPei233/zju-learning-assistant](https://github.com/PeiPei233/zju-learning-assistant)（MIT）：登录流程、学在浙大与智云课堂的 API 调用都移植自它的 `src-tauri/src/zju_assist.rs`。本项目沿用 MIT 授权并保留其版权声明，见 [LICENSE](LICENSE)。
- [eWloYW8/ZJU-course-material-download](https://github.com/eWloYW8/ZJU-course-material-download)（MIT）、[Kcalb35/Tronclass-pdf-downloaderforChrome](https://github.com/Kcalb35/Tronclass-pdf-downloaderforChrome)、[fish-can/TronClass-PDF-Downloader](https://github.com/fish-can/TronClass-PDF-Downloader)、[xzzd-pro/xzzd-pro](https://github.com/xzzd-pro/xzzd-pro)：参考了关闭下载时的端点做法（没有拷贝代码）。

## 授权

[MIT](LICENSE)

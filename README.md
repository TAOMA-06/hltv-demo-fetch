# hltv-demo-fetch

Download CS2 GOTV demos from HLTV with a transport built for Cloudflare-protected
pages: **curl_cffi TLS/JA3 impersonation → real Chrome challenge fallback →
CDP attach → Jina page discovery**, plus resumable CDN downloads and RAR5
extraction.

> 从 `maglab-toolbox/cs2coach` 的 `learn/demo_fetch` 中抽出来的独立 HLTV demo
> 下载工具。

## 特性

- **四层反爬降级**，不是简单伪装 User-Agent：
  1. `curl_cffi` Chrome TLS/JA3 + HTTP/2 指纹（快路径）；
  2. 真实 Chrome（Playwright，装了 `patchright` 自动优先）过 Cloudflare
     managed challenge，拿 `cf_clearance` 后回灌 HTTP 会话；
  3. `--cdp-url` 接管已登录 / 已过验证的 Chrome（最稳）；
  4. `r.jina.ai` 读页面 + `r2-demos.hltv.org` 直链下载（IP 被硬封锁时的兜底）。
- **区分验证页与硬封锁**：`Just a moment...` 可以过；`Sorry, you have been
  blocked` 是 IP 级封锁，工具会直接提示换代理/换出口，不浪费 40 秒启动浏览器。
- **CDN 也会验证**：CDN 403 时自动改用浏览器下载；无头失败可自动切有头窗口重试。
- **断点续传**：下载写 `.part`，中断后下次从 Range 继续。
- **RAR5**：按 `unar → unrar → bsdtar → 7z` 依次尝试，解决 7z 的
  `Unsupported Method`。
- **幂等 + 清单**：SHA-256 去重，`manifest.jsonl` / `failed.jsonl` 可重试。
- 纯 Python，CLI + 可导入库；不依赖 Go / Node。

## 安装

要求 Python ≥ 3.10。

```bash
git clone https://github.com/TAOMA-06/hltv-demo-fetch.git
cd hltv-demo-fetch

python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install chrome   # 本机已有 Google Chrome 可跳过
```

也可以安装成命令：

```bash
pip install -e .
hltv-demo-fetch --help
```

> `curl_cffi` 是快路径；即使没装，工具会退化为标准库/curl，但更容易触发验证。
> `playwright` 只在需要过验证时使用；浏览器可以用系统 Chrome，不必下载
> Playwright 自带 Chromium。

### Docker

```bash
# 快速路径（curl_cffi，镜像小）
docker build -t hltv-demo-fetch .
docker run --rm -v "$PWD/demos:/demos" hltv-demo-fetch \
  --limit 5 --out /demos

# 带 Chromium 的验证兜底（镜像约 +500MB）
docker build --build-arg WITH_BROWSER=1 -t hltv-demo-fetch:browser .
docker run --rm -v "$PWD/demos:/demos" hltv-demo-fetch:browser \
  --limit 5 --out /demos --engine browser
```

容器里没有桌面，CDN 的“点按钮”验证可能过不去；生产环境建议 `--proxy` 或
`--engine curl`，需要浏览器兜底时用宿主机 `--cdp-url`。

## 快速开始

```bash
# 最近 10 场有 demo 的比赛（默认）
python -m hltv_demo_fetch

# 最近 5 星比赛，2026-10-01 之后
python -m hltv_demo_fetch --limit 10 --stars 5 --since 2026-10-01

# 指定比赛 / 事件 / demo id（可重复传）
python -m hltv_demo_fetch \
  --match-url https://www.hltv.org/matches/2399724/legacy-vs-nemiga-pgl-masters-bucharest-2026
python -m hltv_demo_fetch \
  --event-url https://www.hltv.org/events/8261/esports-world-cup-2026
python -m hltv_demo_fetch --demo-id 113158

# 只看计划，不下载
python -m hltv_demo_fetch --limit 5 --dry-run
```

默认输出到 `./demos/`：

```
demos/
  manifest.jsonl                  # 成功记录（含 sha256）
  failed.jsonl                    # 失败记录，方便换代理后重试
  hltv_113158_legacy-vs-nemiga__map1.dem
```

用 `--out /path/to/demos` 改目录。

## 反爬模式

```bash
# 默认 auto：curl_cffi 优先，遇到验证自动升级真实 Chrome
python -m hltv_demo_fetch --limit 10

# 只走 TLS 指纹，不启动浏览器
python -m hltv_demo_fetch --engine curl --limit 10

# 直接真实浏览器；顽固验证加 --headed 看窗口并手动点一下
python -m hltv_demo_fetch --engine browser --headed --limit 5

# 最稳：接管一个已经登录 / 已通过验证的 Chrome
./scripts/chrome-cdp.sh
python -m hltv_demo_fetch --engine cdp --cdp-url http://127.0.0.1:9222 --limit 5

# 旧实现（只有标准库/curl + Jina），仅调试用
python -m hltv_demo_fetch --engine stdlib --limit 3
```

### 代理 / Cookie

```bash
# HTTP(S) 代理（浏览器和 curl 都会使用）
python -m hltv_demo_fetch --proxy http://127.0.0.1:7890 --limit 10

# 浏览器登录后导出的 Netscape cookies.txt
python -m hltv_demo_fetch --cookie-file ~/hltv-cookies.txt --limit 5

# 直接传 Cookie 字符串
python -m hltv_demo_fetch \
  --cookie 'cf_clearance=...; __cf_bm=...; autologin=...' --limit 5
```

Cookie / `cf_clearance` 默认缓存在：

| 系统 | 路径 |
| --- | --- |
| macOS | `~/Library/Caches/hltv-demo-fetch/hltv-state.json` |
| Linux | `${XDG_CACHE_HOME:-~/.cache}/hltv-demo-fetch/hltv-state.json` |
| Windows | `%LOCALAPPDATA%\hltv-demo-fetch\hltv-state.json` |

文件权限 `0600`，只保存 `*.hltv.org` 域 cookie；含登录态时不要提交/分享。
用 `--state-file` 改位置。

### CDP 注意事项

Chrome 136+ 不允许对默认 profile 开 remote debugging，所以
`scripts/chrome-cdp.sh` 会使用一个独立 profile：

```bash
./scripts/chrome-cdp.sh
# 输出类似：Chrome CDP: http://127.0.0.1:9222
```

第一次在里面登录 hltv.org（需要旧 demo 或 premium 时），保持窗口打开，另开终端
跑 `--engine cdp --cdp-url http://127.0.0.1:9222`。CDP 模式使用这个 Chrome 自己的
网络/代理设置，`--proxy` 不会改接管浏览器的代理。

## 环境变量

| 变量 | 说明 |
| --- | --- |
| `HLTV_ENGINE` | `auto` / `curl` / `browser` / `cdp` / `stdlib` |
| `HLTV_IMPERSONATE` | curl_cffi 指纹，默认 `chrome`（也可 `chrome152_macos`、`firefox155` 等） |
| `HLTV_BROWSER_CHANNEL` | `auto` / `chrome` / `msedge` / `chromium` |
| `HLTV_HEADLESS` | `0` 显示浏览器窗口，默认 `1` |
| `HLTV_CDP_URL` | 同 `--cdp-url` |
| `HLTV_STATE_FILE` | 同 `--state-file` |
| `HLTV_CHALLENGE_TIMEOUT` | 等待验证秒数，默认 `40` |

## 工作原理

```text
results/event/match page
        │
        ├─ curl_cffi (Chrome TLS + HTTP/2 fingerprint)
        │       └─ 403 challenge ─→ real Chrome (Playwright/patchright)
        │                              └─ cf_clearance → back to HTTP session
        ├─ 403 hard block ─→ Jina Reader (page discovery only)
        │
        └─ /download/demo/<id>
                ├─ 302 → r2-demos.hltv.org/*.rar
                ├─ curl_cffi stream + Range resume
                └─ CDN challenge → browser download (headed retry if needed)
```

关键点：

- 不覆盖 `curl_cffi` 的 User-Agent，避免 TLS 指纹和 UA 版本对不上。
- 硬封锁不重试浏览器；managed challenge 才升级浏览器。
- 浏览器下载监听的是 Playwright 的 `download` 事件，不是把大文件读进内存。
- 只保存 HLTV 域 cookie，避免把用户其他站点 cookie 写进状态文件。

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `Just a moment...` / `cf-mitigated: challenge` | 去掉 `--engine curl`；默认 auto 会拉真实浏览器；顽固时 `--engine browser --headed`，或 `--cdp-url` |
| `Sorry, you have been blocked` | 出口 IP 被硬封锁：`--proxy` 换住宅代理，或在未封锁网络上的 Chrome 用 `--cdp-url` |
| 浏览器验证 40s 超时 | `--headed` 手动点；`--challenge-timeout 90`；或换代理 |
| `curl_cffi 未安装` | `pip install curl_cffi` |
| `浏览器模式需要 Playwright` | `pip install playwright` 且 `python -m playwright install chrome` |
| CDN 下载 403 / 返回 HTML | 工具会自动用浏览器；仍失败就 `--headed` / `--proxy`，不要多线程轰炸 |
| `r.jina.ai` SSL/超时 | 网络问题或 Jina 限流；换代理，或直接 `--match-url` / `--demo-id` |
| `.rar` 解压 `Unsupported Method` | 安装 `unar` / `unrar`；macOS：`brew install unar` |
| 重复下载 | 默认幂等；看 `manifest.jsonl`，需要重下加 `--force` |

## 测试

```bash
python -m unittest discover -s tests -v

# 可选：跑真实浏览器“验证页 → 下载”本地用例
RUN_BROWSER_TESTS=1 python -m unittest tests.test_hltv_demo_fetch.BrowserDownloadTests -v
```

离线测试覆盖：页面解析、demo id 提取、Jina CDN URL、cookie 状态、TLS 下载断点续传、
自定义下载器、RAR 工具降级、CLI 参数。

## 合规与礼貌

- 只下载你有权使用/分析的 demo，遵守 HLTV 的服务条款。
- 默认 demo 间隔 5 秒；不要并发轰炸，否则 IP 更容易被 Cloudflare 封。
- 工具不破解登录、不绕过付费墙；需要登录的 demo 请用你自己的浏览器会话
  （`--cookie-file` / `--cdp-url`）。
- 不要把 Cookie、代理密码、`hltv-state.json` 提交到仓库。

## 目录

```
Dockerfile         可选容器镜像（WITH_BROWSER=1 带 Chromium）
hltv_demo_fetch/
  cli.py          CLI
  hltv.py         HLTV 页面解析 + job 构建
  stealth.py      curl_cffi / Playwright / CDP / Jina 传输层
  pipeline.py     下载 → 解压 → 校验 → manifest
  archive.py      zip/rar/7z/tar/gz 解压，RAR5 工具降级
  manifest.py     幂等清单
  net.py          curl/urllib + 断点续传
  util.py         cookie/日期/文件名工具
tests/
scripts/
  fetch-hltv.sh   包装 `python -m hltv_demo_fetch`
  chrome-cdp.sh   启动带 CDP 的专用 Chrome
```

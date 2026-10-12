# timetrace

**本机屏幕时间账本：离线、不打分、把"挂着窗口去吃饭"从工作时间里剔掉。**

晚上回想"今天干了啥"，常常只记得很忙；或者反过来，计划写代码，一天过去发现浏览器占了六小时。
timetrace 只做一件事：在你自己的机器上记一份**诚实的**时间账本，然后出一张单文件报告。

```
timetrace · 2026-09-23（今天）  覆盖率 98.2%  最大采样间隔 12s  切换 87 次

  应用                    时长      占比   最长专注块
  code.exe               3h12m    41.2%   47m（09:12–09:59）
  msedge.exe             1h05m    14.0%   18m（14:20–14:38）
  （空闲）               1h40m    21.5%   —
  （锁屏）                 12m     2.6%   —
  （未记录）                8m     1.8%   —
```

## 它凭什么诚实

其他工具会把"开着窗口去吃饭两小时"算成工作两小时。timetrace 不会，因为两件事：

1. **空闲回填**：Windows 会告诉你"距离上一次键鼠输入过了多久"。所以 12:30 采样到"已空闲 40 分钟"时，
   我们知道那段空闲从 **11:50** 就开始了 —— 而不是从 12:30。这一条把挂机从工作时间里剔掉。
2. **账本守恒**：报告里每一项加起来**必须等于**区间总长，差额只能落进两个明说的桶：
   `（空闲）` 与 `（未记录）`。**没有"其它"这种黑盒**，守恒律由测试钉住。

它还自己报告自己有多可信：**覆盖率**、**最大采样间隔**、**丢弃的异常采样数**都在报告头部。
笔记本休眠过 40 分钟？报告会老实写"未记录 40m"，而不是把它摊给某个应用。

看一周/一月时，报告会给一张**每日趋势**：每天一根堆叠柱 + 一张逐日明细表，
把 `应用 / 空闲 / 未记录 / 锁屏 / 暂停` 五个桶、覆盖率、切换数、头号应用逐日列出 ——
每一行的五个桶加起来正好等于当天被记录的时长，跨午夜的段按实际秒数切到两天。

## 三条硬承诺（由测试强制，不是口号）

| 承诺 | 怎么保证 |
| --- | --- |
| **永不联网** | 运行时零依赖，且有一条测试扫描 import 图，出现 `socket`/`urllib`/`http` 就红 |
| **只记进程名与窗口标题** | 不记按键、不截屏、不读文件内容；`--no-titles` 连标题都不存；`purge` 真删 |
| **只报账，不打分** | 没有"效率分""专注力指数"这种东西 —— 那是伪科学，工具不知道你在干活还是在摸鱼 |

## 快速开始

```powershell
# 需要 Python 3.11+，不需要 pip install（运行时零依赖）
git clone <this repo>; cd timetrace

python scripts/timetrace_dev.py track            # 开始记录（Ctrl+C 停止）
python scripts/timetrace_dev.py report --range today
python scripts/timetrace_dev.py report --range week --html   # 生成单文件报告
```

装了包（或装到纯 ASCII 路径下）之后就是普通的 `timetrace ...`：

```powershell
timetrace track
timetrace status
timetrace report --range today
timetrace autostart --enable      # 开机自启：只写你自己账户的启动目录
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `track` | 常驻采样（Ctrl+C 优雅退出，退出码 130） |
| `pause [--note "开会"]` / `resume` | 明确不记的时间段（报告里显示为「已暂停」） |
| `status` | 现在在记吗、今天记了多少、DB 在哪 |
| `report --range today\|week\|month\|all\|A..B [--html] [--json] [--titles]` | console / HTML / JSON 三种报告，数字同源；多天区间给「每日明细」 |
| `purge --range ... \| --all` | 真删（DELETE + VACUUM，不留备份；区间内的暂停区间一并删） |
| `export --format json\|csv [--no-titles]` | 导出后可以放心 purge（JSON 带暂停区间与备注，CSV 的 `note` 列带备注） |
| `autostart --enable \| --disable \| --show` | 开机自启（写当前用户启动目录，不动注册表/服务） |
| `doctor [--range today]` | 自检：能不能拿到前台窗口、DB 可写、锁是否被占、采样一次多久；带 `--range` 顺带体检该区间的覆盖率与最大采样间隔 |

退出码：`0` 正常 · `1` 运行时错误 · `2` 用法错误 · `3` 已有实例在跑 · `130` 被中断。

## 数据放在哪

默认 `%LOCALAPPDATA%\timetrace`（可用 `--home` 或 `$TIMETRACE_HOME` 覆盖）：

```
timetrace.db    SQLite（WAL），一行一次采样
lock            单实例锁（带 PID，进程被杀后能自动回收）
config.json     手写配置，CLI 不会偷偷改它
report.html     --html 的默认输出
autostart.log   开机自启每次拉起都会追加一行（--show / doctor 会读它）
```

实测开销（`python scripts/bench_overhead.py`，详见 `DEVELOPMENT.md` §24.2）：

| 指标 | 实测 |
| --- | --- |
| 单次采样 | P50 0.022ms / P95 0.028ms（目标 < 5ms） |
| tracker 常驻内存 | 4.8–23MB |
| 一天写入 | 0.77s（16800 行） |
| 磁盘 | **113 字节/行**，满采样一天约 1.8MB（`--interval` 是存储旋钮：10 秒采样直接减半） |
| 30 天报告（51.8 万行） | 1.18s |

## 它不知道的事（写在明面上）

- **采样法会丢短切换**：短于采样间隔（默认 5 秒）的窗口切换不计入 —— 换来的是零依赖、抗崩溃、
  系统 Python 直接能跑；
- **`code.exe` 占 3 小时 ≠ 写了 3 小时代码**：窗口标题不等于你的意图；
- **全屏视频/游戏**照样算前台应用时间，工具不猜你在学习还是在摸鱼（可用 `--exclude` 手工排除）；
- **多显示器只有一个前台窗口**，副屏活动不单独计时；
- MVP **只支持 Windows**（前台窗口信息来自 Win32），其它平台会明确报错而不是假装能跑。

## 文档

- `DEVELOPMENT.md` —— 规格书：§9 判定规则（含可验算的例子）、§20 可执行完成定义、§24 实测记录
- `docs/privacy.md` —— 隐私说明（一页）
- `docs/adr/` —— 为什么零依赖、为什么采样而不是窗口钩子、为什么不做评分
- `docs/known-deviations.md` —— 与规格书的偏差

## English summary

**timetrace** is a local-only screen-time ledger for Windows. It samples the foreground window
every few seconds (process name + title) and Windows' "seconds since last input", stores
everything in a local SQLite file, and renders a single-file HTML report.

Two things make it honest: it **backfills idle time to where it actually started** (so
"left the window open while eating" is not counted as work), and its buckets are required by
test to **add up to the whole interval** — the only slack lives in explicitly named
`idle` and `not recorded` buckets. It reports its own trustworthiness (sample coverage,
largest gap, discarded samples) and never scores you.

Zero runtime dependencies (stdlib + ctypes), no network code at all (enforced by a test),
no screenshots, no keystrokes, and `purge` really deletes.

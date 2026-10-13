# timetrace 使用教程

从第一次安装到每天日常用法。命令的完整参考在 README 的命令表；
每个参数为什么是这样在 `DEVELOPMENT.md`。这篇只讲**怎么用**。

---

## 0. 三十秒版本

```powershell
# 第一次：先确认这台机器能采到窗口（只读检查，不开始记录）
timetrace doctor

# 开始记录（Ctrl+C 停止；想让它常驻就加 --tray 挂托盘）
timetrace track

# 晚上：看今天的账
timetrace report --range today
```

数据默认落在 `%LOCALAPPDATA%\timetrace`，一个 SQLite 文件，不联网、不上传。

---

## 1. 第一次使用：三步

**① 确认能采到前台窗口。** `timetrace doctor` 会真调一次 Win32：能拿到窗口名、
空闲秒数、采样开销，就亮 √；拿不到会直接说为什么。非 Windows 系统会明确报错
（这个工具只做 Windows 端，不假装能跑）。

**② 采一条试试。** `timetrace track --once` 采一条样本就退出 —— 不启动常驻，
适合验证"记录了但我不想现在开始"。

**③ 看一条真数据长什么样。** 还没有历史数据时，可以拿演示数据过眼瘾：

```powershell
python scripts/make_demo.py        # 生成 21 天合成数据并渲染 docs/demo.html
```

或者直接记录几分钟真的数据，然后 `timetrace report --range today`。

---

## 2. 三种入口，选一种

| 入口 | 适合谁 | 怎么起 |
| --- | --- | --- |
| 开发脚本 | 在源码目录里开发 | `python scripts/timetrace_dev.py track` |
| 装了包 | 日常使用 | `timetrace track`（`pip install` 这个仓库的 wheel） |
| 桌面应用 | 不想碰命令行 | 双击 `dist/timetrace.exe`，驻留托盘开始记录 |

桌面应用与命令行版**共用同一份数据**（都在 `%LOCALAPPDATA%\timetrace`），
从托盘退出不会丢已记录的部分。exe 没做代码签名，第一次运行 Windows
SmartScreen 会拦：「更多信息 → 仍要运行」。

**想开机自动开始记录**：`timetrace autostart --enable`（加 `--tray` 则开机
直接挂托盘）。它只写你自己账户的启动目录，不动注册表、不建服务。
`autostart --show` 看当前状态，`--disable` 关掉。

---

## 3. 一天的节奏

```powershell
# 早上到电脑前：开始记录（不想碰命令行就双击 dist/timetrace.exe 驻留托盘）
timetrace track

# 白天有段明确不记的时间（开会 / 演示 / 隐私时刻）
timetrace pause --note "开会"
timetrace resume          # 开完会恢复记录

# 晚上复盘：看今天的账；想存档或发给别人就加 --html 出单文件报告
timetrace report --range today
```

几个细节：

- **Ctrl+C 优雅停止**（退出码 130），退出前会把没落盘的采样 flush 完；
  直接杀进程不会损坏数据，但那次记录的结束时间会停在"被杀掉的那刻"
  （报告如实写"没有结束时间"，不编）。
- **同一时间只能有一个 `track` 在跑**（单实例锁）。起第二个会退出码 3
  并告诉你占用者的 PID；进程被杀后锁自动回收，不用手工清。
- **托盘图标**：右键（或左键）点它 → 打开报告 / 暂停 / 恢复 / 退出。
  托盘起不来不影响记录，只是少个图标。
- **`track` 期间 DB 可以随便读**：另开一个终端跑 `status` / `report` 不冲突。

---

## 4. 报告怎么读

报告头部三个数字是**工具在自报可信度**，先看它们再看下面的表：

| 指标 | 含义 | 低/高说明什么 |
| --- | --- | --- |
| 覆盖率 | 真的采到样的时间占区间比例 | 低 = 大部分时间没在记录，先确认 tracker 在跑再信数字 |
| 最大采样间隔 | 区间内最长的"没采样"空档 | 超过 `interval × 3` 记为「未记录」（休眠/关机/进程被杀） |
| 丢弃的异常采样 | 时间倒退等对不上的样本数 | 偶尔几条是 NTP 校时，正常 |

下面的表分五个桶，**加起来必须等于区间总长**（账本守恒，没有"其它"黑盒）：

- **应用**：按进程名聚合的时间。`code.exe` 3 小时 ≠ 写了 3 小时代码 ——
  窗口标题不等于你的意图，工具不猜。
- **（空闲）**：离开电脑的时间。按 `ts - idle` 回填到**真正离开的时刻**，
  所以"挂着窗口去吃饭一小时"不算应用时间 —— 这是这个工具最重要的诚实。
- **（未记录）**：没采到样的时间（休眠/关机）。不摊给任何应用。
- **（锁屏）**：锁屏时间。
- **（已暂停）**：`pause` 明确不记的时间。

多天区间（week/month/all）会多一张「每日明细」：每天一行五桶 + 覆盖率 + 切换数 +
头号应用；两周以上还有日历热力图。

---

## 5. 常用命令速查

| 想干什么 | 命令 |
| --- | --- |
| 开始记录 | `timetrace track`（常驻） |
| 挂托盘常驻 | `timetrace track --tray` |
| 写运行日志 | `timetrace track --log`（出错与生命周期进 `logs/track.log`） |
| 采一条就退 | `timetrace track --once` |
| 明确不记一段 | `timetrace pause --note "开会"` → `timetrace resume` |
| 现在什么状态 | `timetrace status` |
| 今天的账 | `timetrace report --range today` |
| 最近一周/月/全部 | `timetrace report --range week` / `month` / `all` |
| 任意区间 | `timetrace report --range 2026-09-28..2026-10-18`（含首含尾） |
| 单文件 HTML 报告 | `timetrace report --range week --html` |
| JSON（给程序用） | `timetrace report --range week --json` |
| 按窗口标题细分 | `timetrace report --range week --titles` |
| 只看前 5 名应用 | `timetrace report --range week --top 5` |
| 自检 | `timetrace doctor` / `timetrace doctor --range today` |
| 开机自启 | `timetrace autostart --enable [--tray]` |
| 导出了再删 | `timetrace export --format json --out backup.json` |
| 真删 | `timetrace purge --all --yes` |

退出码：`0` 正常 · `1` 运行时错误 · `2` 用法错误 · `3` 已有实例在跑 · `130` 被中断。

---

## 6. 配置：让它贴合你的节奏

配置优先级：**命令行 > 环境变量 > `config.json` > 内置默认**。
`config.json` 放在数据目录下手写即可，CLI 不会偷偷改它：

```json
{
  "interval": 5,
  "idle_threshold": 300,
  "goals": { "code.exe": 20 },
  "max_stretch_minutes": 90,
  "exclude": ["steam.exe"]
}
```

| 键 | 默认 | 作用 |
| --- | --- | --- |
| `interval` | 5 | 采样间隔（秒）。存储旋钮：改成 10 直接省一半磁盘 |
| `idle_threshold` | 300 | 空闲超过这么多秒才算"人走了" |
| `focus_min` | 10 | 同一应用连续不换超过这么多分钟算一个专注块 |
| `titles` | true | 是否记录窗口标题（`--no-titles` 连它都不存） |
| `exclude` | `[]` | 这些进程的时间照记、不计入排名、单独列出 |
| `goals` | `{}` | 每个进程的目标小时数。报告给实际/目标/进度 —— **只报进度，不打分** |
| `max_stretch_minutes` | 0 | 连续没离开超过这么久就列出来提醒。0 = 关 |
| `log` | false | 是否写 `logs/track.log` |

环境变量形如 `TIMETRACE_INTERVAL=11`、`TIMETRACE_TITLES=false`；
数据目录用 `--home 目录` 或 `TIMETRACE_HOME` 覆盖（中文路径没问题）。

`timetrace doctor` 会打印每个生效值**来自哪一层** —— 配置"不知道谁改了我的
设置"时，先跑它。

---

## 7. 数据管理

**数据在哪**：`%LOCALAPPDATA%\timetrace\timetrace.db`（SQLite WAL）。
换电脑 = 拷走这一个文件。备份 = 拷贝整个目录。

**导出再删**（唯一安全的 purge 前置）：

```powershell
timetrace export --format json --out backup.json            # 含暂停区间与备注
timetrace export --format csv --out samples.csv --pauses-out pauses.csv
timetrace purge --range 2026-09-01..2026-09-30 --yes        # 真删指定区间
```

`purge` 是 **DELETE + VACUUM，不留备份、没有回收站** —— 删了就是删了。
所以日常节奏是：定期 `export` 存档 → 需要时 `purge`。

---

## 8. 常见问题

**Q：`code.exe` 占了 3 小时，是我摸了 3 小时鱼吗？**
工具不知道，也不猜。窗口标题不等于意图；它只负责把时间记准。

**Q：短窗口切换（比如秒回一条消息）算得到吗？**
算不到。采样法会丢短于 `interval`（默认 5s）的切换 —— 换来的是零依赖、
抗崩溃、系统 Python 直接能跑。这是明码标价的取舍。

**Q：全屏看视频算在哪个应用上？**
算当时的前台应用。可以用 `--exclude` 手工排除（排除项的时间照记，单独列出）。

**Q：副屏上的活动呢？**
Win32 只有一个前台窗口，副屏不单独计时。做不到就不假装。

**Q：笔记本休眠了一小时，报告怎么说？**
老实说：「未记录 1h」，不摊给任何应用。

**Q：数据会被传走吗？**
不会。永不联网是测试强制的（有测试扫 import 图，出现网络库直接红）；
只记进程名与窗口标题，不记按键、不截屏。详见 `docs/privacy.md`。

---

## 9. 排障顺序

1. `timetrace doctor` —— 十二项自检，红的说人话；
2. `timetrace status` —— 现在在记吗、DB 在哪、上次记录什么时候停的；
3. `logs/track.log`（`track --log` 时才有）—— 生命周期与出错；
4. 还不行：`DEVELOPMENT.md` §9 判定规则，或带着 `doctor` 输出提问。

---

## 10. 一分钟演示

想先看看报告长什么样再决定用不用：

```powershell
python scripts/make_demo.py
# → docs/demo.html：21 天合成数据的完整报告，双击就能看
```

合成数据是编的，但报告是工具的真实产出 —— 你每天看到的格式就是它。

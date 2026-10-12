# 隐私说明（一页读完）

timetrace 记录的东西很少，但记录的东西很敏感。这一页把"记什么、不记什么、存在哪、怎么删"
一次说清，不用你翻代码。

## 记什么

每次采样（默认每 5 秒）：

| 记的 | 例子 | 为什么需要 |
| --- | --- | --- |
| 时间戳 | `2026-09-23T05:24:42.336Z` | 账本的基本单位 |
| 前台进程名 | `code.exe` | 报告里的排行 |
| 窗口标题 | `main.py — project` | 可选（`--no-titles` 关掉）；用于"按标题细分" |
| 距离上一次键鼠输入的秒数 | `1363.078` | **只读一个时间戳**，用来判断"人在不在" |
| 采样类型 | `active` / `paused` / `error` | 暂停与失败也要如实记账 |

## 不记什么

- **不记按键内容**：只读 `GetLastInputInfo` 返回的"上次输入距今多久"这一个数字；
- **不截屏**、不录屏、不读窗口内容、不读文件、不读浏览器历史；
- **不知道你在输入什么**，也不知道鼠标在哪；
- **不记录多显示器上非前台窗口**（Win32 只有一个前台窗口，做不到就不假装）。

## 存在哪

```
%LOCALAPPDATA%\timetrace\timetrace.db     SQLite 单文件（WAL 模式）
%LOCALAPPDATA%\timetrace\lock             单实例锁（JSON：PID + 启动时间 + 解释器路径）
%LOCALAPPDATA%\timetrace\config.json      手写配置（CLI 不会偷偷改它）
%LOCALAPPDATA%\timetrace\report.html      --html 的默认输出
```

要放到别处：`--home DIR` 或环境变量 `TIMETRACE_HOME`。

## 永远不会发生的事

- **不会联网**。运行时零依赖，且验收脚本会扫 import 图，出现 `socket`/`urllib`/`http`/`requests`/`httpx`
  就直接判 FAIL（`DEVELOPMENT.md` §20-7）。你可以自己跑一遍验证，不用相信这段话；
- **不会上传**：没有账号、没有遥测、没有"匿名使用统计"、没有更新检查；
- **不会开机自动扫描历史**：只有你自己运行 `track`（或显式 `autostart --enable`）之后才开始记录。

## 你要注意的

- **窗口标题可能泄露信息**：浏览器标签、邮件主题、文件名都在标题里。
  公用电脑上建议 `--no-titles`，或者临时 `pause`；
- **报告可以直接分享**，但请先看一眼：报告里不含用户名、主机名、完整路径、IP
  （有测试断言），但**窗口标题和应用名本身**就在里面；
- **导出前想清楚**：`export` 默认包含标题，用 `--no-titles` 去掉。

## 怎么停、怎么删

```powershell
timetrace pause                 # 临时不记（报告里显示为「已暂停」）
timetrace resume
timetrace purge --range today   # 删掉今天（会要求确认；DELETE + VACUUM，真删）
timetrace purge --all --yes     # 全删
timetrace autostart --disable   # 取消开机自启
```

也可以直接删掉 `%LOCALAPPDATA%\timetrace` 整个目录 —— 那之后 timetrace 就什么都不知道了。

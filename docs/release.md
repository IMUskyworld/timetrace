# 发布检查单

切一个版本时按这个走。核心原则：**工件必须从当前代码现构，闸门必须现跑** ——
`dist/` 里的旧工件不能直接拿去发布（2026-10-13 就抓到过一次：wheel 比代码旧了两笔提交，
版本号却还写着同一个 0.1.0）。

## 0. 前置

- [ ] 工作区干净（`git status`），所有改动已提交
- [ ] 版本号一致：`pyproject.toml` 的 `version` == `src/timetrace/__init__.py` 的 `__version__`（`test_packaging.py` 会查，分叉了就各说各话）

## 1. 质量闸门（一条红就停，不带病发布）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/check.ps1
```

覆盖：运行时零依赖断言 → ruff check / format → pyright → 仓库内 LF 检查 → pytest（386 项）→ `verify_mvp`（§20 十二条完成定义）。

## 2. 构建工件

```powershell
# wheel：任何有 pip 的 Python 都行，PEP 517 隔离自动拉 hatchling，不污染环境
python -m pip wheel . --no-deps -w dist/

# exe：需要一个带 pyinstaller 的环境（只用于构建，运行时仍零依赖）
python -m venv .build-venv
.build-venv\Scripts\python -m pip install ".[desktop]"
.build-venv\Scripts\python -m PyInstaller --noconfirm --clean packaging/timetrace.spec
```

## 3. 工件验收（不能只验"构建成功"）

- [ ] wheel 内容完整：`timetrace/` 下所有模块都在（**含 `app.py`** —— 桌面版入口，漏了它 frozen 构建就是残的）
- [ ] 装进**含中文路径的干净 venv**，十条命令全部 exit 0：`version`（两种写法）/ `status` / `report`（console·JSON·HTML 三态）/ `export`（JSON·CSV）/ `doctor` / `autostart --show` / `purge --all`
- [ ] exe 冒烟：进程驻留、按间隔写库（schema v2）、托盘挂上、`logs/track.log` 有启动行。用临时 `--home` 跑，别污染真实数据目录
- [ ] 用真实数据出一份 `report --range all`，核对五桶守恒（各项之和 = 区间总长）

## 4. 收尾文档

- [ ] `CHANGELOG.md` 补新版本条目（新增 / 修复 / 性能 / 隐私分节）
- [ ] README 命令表与实现一致（`test_docs.py` 会查漏与多）
- [ ] `DEVELOPMENT.md` §17 里程碑、§25 变更记录同步
- [ ] **报告层有变更时**：重跑 `python scripts/make_demo.py` 刷新 `docs/demo.html` 与三张 SVG —— `test_demo.py` 会检查它们与生成器同步，不同步就红（演示资产烂在仓库里比没有更糟）

## 5. 提交与打标签

```powershell
git tag -a v0.1.0 -m "timetrace v0.1.0"
```

仓库当前**没有配置远程**（local-first 项目，刻意如此）。要发布到 GitHub 时：
`git remote add origin <url>` 后推送，CI（`.github/workflows/ci.yml`）自动跑
windows / ubuntu × py3.11 / 3.12 全矩阵；Release 页面挂 `dist/` 下的 wheel 与 exe。

## 已知注意

- **exe 未做代码签名**：首次运行 Windows SmartScreen 会拦。要么接受签名成本，要么在发布页写明"更多信息 → 仍要运行"
- **`pip install -e`（可编辑安装）在含中文的源码路径下会出问题**（`.pth` 踩 CJK 坑）；wheel 安装不受影响。开发用 `scripts/timetrace_dev.py`
- `purge` 是真删（DELETE + VACUUM，不留备份）——发布说明里别把它写成"清空回收站"

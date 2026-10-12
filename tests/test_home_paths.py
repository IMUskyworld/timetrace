r"""数据目录带非 ASCII 字符时，每条命令都要照常工作。

项目本身就活在 `C:\code\github小项目\` 下，`timetrace_dev.py` 那段注释也是因为
cp936 解码含中文的 `.pth` 才存在 —— 中文路径不是假想场景，是这台机器的日常。
"""

from __future__ import annotations

from pathlib import Path

from timetrace.cli import build_parser, main
from timetrace.errors import EXIT_OK


def _all_commands() -> list[str]:
    parser = build_parser()
    sub = next(action for action in parser._subparsers._group_actions)
    return sorted(sub.choices)


def test_每个子命令都接受_home():
    """统一调用 `timetrace $cmd --home $DIR` 不该在某一个命令上炸。

    `version` 原来不收 `--home` —— 于是 for 循环式调用刚好在它身上 exit 2。
    """
    parser = build_parser()
    sub = next(action for action in parser._subparsers._group_actions)
    missing = []
    for name, command_parser in sub.choices.items():
        options = set()
        for action in command_parser._actions:
            options.update(action.option_strings)
        if "--home" not in options:
            missing.append(name)
    assert not missing, f"这些命令不接受 --home：{missing}"
    assert set(_all_commands()) == set(sub.choices)


def test_中文数据目录跑通全部命令(tmp_path: Path, monkeypatch, capsys):
    home = tmp_path / "时间账本-测试" / "子目录"
    home_str = str(home)

    # track --once 建库
    assert main(["track", "--once", "--quiet", "--home", home_str]) == EXIT_OK
    capsys.readouterr()
    assert (home / "timetrace.db").exists(), "库必须落在中文目录里"

    # 读类命令
    for command in (
        ["status"],
        ["doctor"],
        ["report", "--range", "all", "--json"],
        ["export", "--format", "csv"],
    ):
        assert main([*command, "--home", home_str]) == EXIT_OK, f"{command} 失败"
        capsys.readouterr()

    # HTML 落盘
    html_path = home / "报告.html"
    assert (
        main(["report", "--range", "all", "--html", "--out", str(html_path), "--home", home_str]) == EXIT_OK
    )
    capsys.readouterr()
    assert html_path.exists()
    assert html_path.read_text(encoding="utf-8").startswith("<!doctype html>")

    # 暂停 / 恢复（note 是中文）
    assert main(["pause", "--note", "开会", "--home", home_str]) == EXIT_OK
    assert "已暂停" in capsys.readouterr().out
    assert main(["resume", "--home", home_str]) == EXIT_OK
    capsys.readouterr()

    # 导出到中文文件名
    export_path = home / "导出.csv"
    assert main(["export", "--format", "csv", "--out", str(export_path), "--home", home_str]) == EXIT_OK
    capsys.readouterr()
    assert export_path.exists() and export_path.read_text(encoding="utf-8").startswith("ts,exe")

    # 纯信息类
    assert main(["version", "--home", home_str]) == EXIT_OK
    assert "timetrace" in capsys.readouterr().out


def test_中文目录里的报告不含路径与用户名(tmp_path: Path, monkeypatch, capsys, tmp_path_factory):
    """§12-7：报告不能泄露本机信息 —— 目录名带中文时更容易顺手带出去。"""

    home = tmp_path / "隐私测试目录"
    assert main(["track", "--once", "--quiet", "--home", str(home)]) == EXIT_OK
    capsys.readouterr()
    assert main(["report", "--range", "all", "--json", "--home", str(home)]) == EXIT_OK
    payload = capsys.readouterr().out
    assert str(home) not in payload, "报告里出现了数据目录路径"
    assert "隐私测试目录" not in payload, "报告里出现了目录名"

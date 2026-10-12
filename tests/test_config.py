"""config：默认值、文件覆盖、不认识的键只警告、坏文件给用法错误。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from timetrace.config import Config, from_dict, load_config
from timetrace.errors import UsageError


def test_默认值符合规格():
    config = Config()
    assert config.interval == 5.0
    assert config.idle_threshold == 300.0
    assert config.focus_min == 10.0
    assert config.titles is True
    assert config.exclude == ()
    assert config.log is False


def test_gap_limit_是间隔的倍数():
    assert Config(interval=5.0, gap_limit_factor=3.0).gap_limit == 15.0
    assert Config(interval=10.0, gap_limit_factor=2.5).gap_limit == 25.0


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"interval": 0.5}, "采样间隔"),
        ({"idle_threshold": 10.0}, "空闲阈值"),
        ({"focus_min": 0.0}, "专注块"),
    ],
)
def test_validate_拦住不合理的值(patch: dict[str, float], message: str):
    with pytest.raises(UsageError, match=message):
        Config(**patch).validate()  # type: ignore[arg-type]


def test_validate_默认值通过():
    Config().validate()


def test_from_dict_不认识的键只警告():
    config, warnings = from_dict({"interval": 10, "interval_seconds": 99}, source="test")
    assert config.interval == 10.0
    assert len(warnings) == 1
    assert "interval_seconds" in warnings[0]


def test_from_dict_类型不对报用法错误():
    with pytest.raises(UsageError, match="interval 不是数字"):
        from_dict({"interval": "五秒"}, source="test")
    with pytest.raises(UsageError, match="titles 应该是 true 或 false"):
        from_dict({"titles": "yes"}, source="test")
    with pytest.raises(UsageError, match="interval 应该是数字"):
        from_dict({"interval": None}, source="test")
    # bool 是 int 的子类，但不能当成数字用
    with pytest.raises(UsageError, match="不是 true/false"):
        from_dict({"interval": True}, source="test")


def test_from_dict_exclude_允许单个字符串():
    config, _ = from_dict({"exclude": "Slack.exe"}, source="test")
    assert config.exclude == ("slack.exe",)
    config, _ = from_dict({"exclude": [" A.exe ", ""]}, source="test")
    assert config.exclude == ("a.exe",)


def test_from_dict_记录来源():
    config, _ = from_dict({}, source="C:/tmp/config.json")
    assert config.sources == ("C:/tmp/config.json",)


def test_load_config_文件不存在就用默认值(home_dir: Path):
    config, warnings = load_config(home_dir / "config.json")
    assert config == Config(sources=config.sources)
    assert config.interval == 5.0
    assert warnings == []
    assert config.sources == ("内置默认",)


def test_load_config_None_表示不用文件(tmp_path: Path):
    (tmp_path / "config.json").write_text('{"interval": 60}', encoding="utf-8")
    config, _ = load_config(None)
    assert config.interval == 5.0


def test_load_config_读到真实文件(home_dir: Path):
    path = home_dir / "config.json"
    path.write_text(
        json.dumps({"interval": 7.5, "titles": False, "exclude": ["slack.exe"]}), encoding="utf-8"
    )
    config, warnings = load_config(path)
    assert config.interval == 7.5
    assert config.titles is False
    assert config.exclude == ("slack.exe",)
    assert warnings == []
    assert config.sources == ("内置默认", str(path))


def test_load_config_坏_json_给出可操作提示(home_dir: Path):
    path = home_dir / "config.json"
    path.write_text("{ 这不是 json", encoding="utf-8")
    with pytest.raises(UsageError) as caught:
        load_config(path)
    assert "读不了" in str(caught.value)
    assert "删掉它" in (caught.value.hint or "")


def test_load_config_顶层不是对象(home_dir: Path):
    path = home_dir / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(UsageError, match="顶层必须是一个对象"):
        load_config(path)


def test_merged_只覆盖显式给的值():
    base = Config(sources=("内置默认",))
    merged = base.merged(interval=2.0, titles=None)
    assert merged.interval == 2.0
    assert merged.titles is True
    assert merged.sources[-1] == "命令行"
    # 原对象不变（frozen dataclass）
    assert base.interval == 5.0


# ------------------------------------------------------------------ 目标


def test_goals_从配置解析并按名字排序():
    config, warnings = from_dict({"goals": {"msedge.exe": 5, "code.exe": "20.5"}}, source="test")
    assert config.goals == (("code.exe", 20.5), ("msedge.exe", 5.0))
    assert config.goal_map == {"code.exe": 20.5, "msedge.exe": 5.0}
    assert warnings == []
    config.validate()


def test_goals_默认为空():
    config = Config()
    assert config.goals == ()
    assert config.goal_map == {}
    assert config.to_dict()["goals"] == {}


def test_goals_键名归一成小写并去空格():
    config, _ = from_dict({"goals": {"  Code.EXE ": 3}}, source="test")
    assert config.goals == (("code.exe", 3.0),)


def test_goals_单个对象也接受():
    """只有目标是单个映射，所以不允许字符串 —— 但允许一个键。"""
    config, _ = from_dict({"goals": {"code.exe": 2}}, source="test")
    assert config.goals == (("code.exe", 2.0),)


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("nope", "应该是对象"),
        ({"code.exe": 0}, "必须是正数"),
        ({"code.exe": -1}, "必须是正数"),
        ({"code.exe": "abc"}, "不是数字"),
        ({"code.exe": True}, "应该是数字"),
        ({"": 2}, "不能为空"),
    ],
)
def test_goals_坏配置报用法错误(raw, message: str):
    with pytest.raises(UsageError, match=message):
        config, _ = from_dict({"goals": raw}, source="test")
        config.validate()


def test_goals_validate_拦住非正数():
    with pytest.raises(UsageError, match="必须是正数"):
        Config(goals=(("code.exe", 0.0),)).validate()


def test_to_dict_能来回():
    config = Config(interval=3.0, exclude=("a.exe",), titles=False)
    again, warnings = from_dict(config.to_dict(), source="roundtrip")
    assert warnings == []
    assert again.interval == 3.0
    assert again.exclude == ("a.exe",)
    assert again.titles is False

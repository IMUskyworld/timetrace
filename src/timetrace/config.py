"""配置：默认值 + `config.json` 覆盖（命令行 > 环境变量 > 配置文件 > 内置默认）。

配置文件是**手写**的，CLI 不写它 —— 少一个能悄悄改掉你设置的地方。
`doctor` 会打印生效值及其来源，方便排查"为什么没按我写的来"。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from timetrace.errors import UsageError

MIN_INTERVAL = 1.0
MIN_IDLE_THRESHOLD = 30.0


@dataclass(frozen=True)
class Config:
    interval: float = 5.0
    idle_threshold: float = 300.0
    focus_min: float = 10.0
    titles: bool = True
    exclude: tuple[str, ...] = ()
    gap_limit_factor: float = 3.0
    log: bool = False
    sources: tuple[str, ...] = field(default=(), compare=False)

    @property
    def gap_limit(self) -> float:
        return self.interval * self.gap_limit_factor

    def validate(self) -> None:
        if self.interval < MIN_INTERVAL:
            raise UsageError(f"采样间隔不能小于 {MIN_INTERVAL:g} 秒", hint="--interval 5 是常用值")
        if self.idle_threshold < MIN_IDLE_THRESHOLD:
            raise UsageError(
                f"空闲阈值不能小于 {MIN_IDLE_THRESHOLD:g} 秒",
                hint="阈值太小会把'停下来思考'也当成离开",
            )
        if self.focus_min <= 0:
            raise UsageError("专注块最小长度必须是正数")

    def merged(self, **overrides: object) -> Config:
        clean = {key: value for key, value in overrides.items() if value is not None}
        return replace(self, **clean, sources=(*self.sources, "命令行"))

    def to_dict(self) -> dict[str, object]:
        return {
            "interval": self.interval,
            "idle_threshold": self.idle_threshold,
            "focus_min": self.focus_min,
            "titles": self.titles,
            "exclude": list(self.exclude),
            "gap_limit_factor": self.gap_limit_factor,
            "log": self.log,
        }


def _number(data: Mapping[str, object], key: str, default: float) -> float:
    raw = data.get(key, default)
    if isinstance(raw, bool):  # bool 是 int 的子类，但它显然不是我们想要的
        raise UsageError(f"config.json 里的 {key} 应该是数字，不是 true/false")
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, str):
        try:
            return float(raw)
        except ValueError as cause:
            raise UsageError(f"config.json 里的 {key} 不是数字：{raw!r}") from cause
    raise UsageError(f"config.json 里的 {key} 应该是数字")


def _flag(data: Mapping[str, object], key: str, default: bool) -> bool:
    raw = data.get(key, default)
    if isinstance(raw, bool):
        return raw
    raise UsageError(f"config.json 里的 {key} 应该是 true 或 false")


def _names(data: Mapping[str, object], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = data.get(key, default)
    if isinstance(raw, str):
        return (raw.strip().lower(),) if raw.strip() else ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip().lower() for item in raw if str(item).strip())
    raise UsageError(f"config.json 里的 {key} 应该是字符串数组（或单个字符串）")


def from_dict(data: Mapping[str, object], *, source: str) -> tuple[Config, list[str]]:
    """从字典构造配置；不认识的键只警告，不炸 —— 手写文件很容易多写一行。"""
    base = Config()
    known = set(base.to_dict())
    warnings = [f"config.json 里不认识的键：{key}" for key in data if key not in known]
    config = Config(
        interval=_number(data, "interval", base.interval),
        idle_threshold=_number(data, "idle_threshold", base.idle_threshold),
        focus_min=_number(data, "focus_min", base.focus_min),
        titles=_flag(data, "titles", base.titles),
        exclude=_names(data, "exclude", base.exclude),
        gap_limit_factor=_number(data, "gap_limit_factor", base.gap_limit_factor),
        log=_flag(data, "log", base.log),
        sources=(source,),
    )
    return config, warnings


def load_config(path: Path | None) -> tuple[Config, list[str]]:
    """读配置文件；不存在就用默认值（这不是错误）。`path=None` 表示调用方已决定不用它。"""
    if path is None or not path.exists():
        return Config(sources=("内置默认",)), []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as cause:
        raise UsageError(f"读不了 {path}：{cause}", hint="修好它，或删掉它用默认值") from cause
    if not isinstance(raw, dict):
        raise UsageError(f"{path} 的顶层必须是一个对象")
    config, warnings = from_dict(raw, source=str(path))
    return replace(config, sources=("内置默认", str(path))), warnings

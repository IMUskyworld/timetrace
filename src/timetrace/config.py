"""配置：默认值 + `config.json` 覆盖（命令行 > 环境变量 > 配置文件 > 内置默认）。

配置文件是**手写**的，CLI 不写它 —— 少一个能悄悄改掉你设置的地方。
`doctor` 会打印生效值及其来源，方便排查"为什么没按我写的来"。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from timetrace.errors import UsageError

MIN_INTERVAL = 1.0
MIN_IDLE_THRESHOLD = 30.0

# 环境变量层：TIMETRACE_<键名大写>。§14 写的优先级是
# 命令行 > 环境变量 > config.json > 内置默认，这里补上中间那一层。
ENV_PREFIX = "TIMETRACE_"
_ENV_FLOAT_KEYS = (
    "interval",
    "idle_threshold",
    "focus_min",
    "gap_limit_factor",
    "max_stretch_minutes",
)
_ENV_BOOL_KEYS = ("titles", "log")
_ENV_TUPLE_KEYS = ("exclude",)


@dataclass(frozen=True)
class Config:
    interval: float = 5.0
    idle_threshold: float = 300.0
    focus_min: float = 10.0
    titles: bool = True
    exclude: tuple[str, ...] = ()
    gap_limit_factor: float = 3.0
    log: bool = False
    # (进程名, 目标小时数)。用元组对而不是 dict：frozen dataclass 要能 hash，而且
    # 配置一旦写定就该是不可变的。要查表用 goal_map。
    goals: tuple[tuple[str, float], ...] = ()
    # 连续"没离开"多久就提醒一次（分钟）。0 = 关掉 —— 规则跟目标一样，不配就不出现，
    # 免得工具自己替用户决定什么算"太久"。
    max_stretch_minutes: float = 0.0
    sources: tuple[str, ...] = field(default=(), compare=False)

    @property
    def gap_limit(self) -> float:
        return self.interval * self.gap_limit_factor

    @property
    def goal_map(self) -> dict[str, float]:
        return dict(self.goals)

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
        for exe, hours in self.goals:
            if not exe:
                raise UsageError("goals 里的进程名不能为空", hint='形如 {"code.exe": 20}')
            if hours <= 0:
                raise UsageError(
                    f"goals[{exe!r}] 必须是正数小时",
                    hint="写 20 表示这个区间想在该应用上花 20 小时",
                )
        if self.max_stretch_minutes < 0:
            raise UsageError(
                "max_stretch_minutes 不能是负数", hint="写 90 表示连续 90 分钟没离开就提醒；0 关闭"
            )

    def merged(self, **overrides: object) -> Config:
        clean = {key: value for key, value in overrides.items() if value is not None}
        if not clean:
            return self  # 一个都没覆盖就别把来源记成"命令行"—— doctor 会照着念
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
            "goals": dict(self.goals),
            "max_stretch_minutes": self.max_stretch_minutes,
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


def _goals(data: Mapping[str, object], key: str = "goals") -> tuple[tuple[str, float], ...]:
    """`{"code.exe": 20, "msedge.exe": 5}` → (("code.exe", 20.0), ...)。

    键按字母序落地：同一份配置每次解析出同一个元组，报告里的目标顺序才稳定。
    """
    raw = data.get(key, ())
    if raw in (None, ()):
        return ()
    if not isinstance(raw, Mapping):
        raise UsageError(f'config.json 里的 {key} 应该是对象，形如 {{"code.exe": 20}}')
    out: list[tuple[str, float]] = []
    for name, hours in raw.items():
        label = str(name).strip().lower()
        if not label:
            raise UsageError(f"{key} 里的进程名不能为空")
        if isinstance(hours, bool) or not isinstance(hours, (int, float, str)):
            raise UsageError(f"{key}[{name!r}] 应该是数字（小时）")
        try:
            value = float(hours)
        except (TypeError, ValueError) as cause:
            raise UsageError(f"{key}[{name!r}] 不是数字：{hours!r}") from cause
        out.append((label, value))
    return tuple(sorted(out))


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
        goals=_goals(data),
        max_stretch_minutes=_number(data, "max_stretch_minutes", base.max_stretch_minutes),
        sources=(source,),
    )
    return config, warnings


def _env_float(raw: str, key: str) -> float:
    try:
        return float(raw.strip())
    except ValueError as cause:
        raise UsageError(f"${ENV_PREFIX}{key.upper()} 不是数字：{raw!r}") from cause


def _env_bool(raw: str, key: str) -> bool:
    text = raw.strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise UsageError(f"${ENV_PREFIX}{key.upper()} 应该是 true/false（或 1/0），收到 {raw!r}")


def _env_tuple(raw: str, key: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in raw.split(",") if item.strip())


def from_env(base: Config, environ: Mapping[str, str] | None = None) -> tuple[Config, list[str]]:
    """读 `TIMETRACE_*` 环境变量，覆盖在文件配置之上（§14 的中间那层）。

    返回 (新配置, 警告)。没有一个变量就原样返回 —— 绝大多数用户不碰它。
    """
    env = os.environ if environ is None else environ
    overrides: dict[str, object] = {}
    touched: list[str] = []
    for key in _ENV_FLOAT_KEYS:
        raw = env.get(f"{ENV_PREFIX}{key.upper()}")
        if raw is not None:
            overrides[key] = _env_float(raw, key)
            touched.append(key)
    for key in _ENV_BOOL_KEYS:
        raw = env.get(f"{ENV_PREFIX}{key.upper()}")
        if raw is not None:
            overrides[key] = _env_bool(raw, key)
            touched.append(key)
    for key in _ENV_TUPLE_KEYS:
        raw = env.get(f"{ENV_PREFIX}{key.upper()}")
        if raw is not None:
            overrides[key] = _env_tuple(raw, key)
            touched.append(key)
    if not touched:
        return base, []
    return replace(base, **overrides, sources=(*base.sources, "环境变量")), []


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

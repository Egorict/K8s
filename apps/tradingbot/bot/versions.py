"""Версии торговых систем.

Зачем. У ТС нет "правильных" настроек - есть гипотезы, которые проверяются
статистикой. Чтобы сравнивать их честно, новая версия не должна ни заменять
старую, ни писать сделки в её журнал: обе торгуют одновременно, каждая ведёт
свой счёт, и через неделю видно, какая лучше.

Версия - это именованный набор отличий от базового конфига:

    VERSIONS["breakout"]["0.2"] = Version(
        notes="порог активности ниже, входим чаще",
        overrides={"breakout": {"min_entry_score": 0.35}},
    )

`overrides` - это {секция конфига: {поле: значение}}. Секция "" означает корень
`Config` (например, "risk" -> cfg.risk, "" -> сам cfg). Поля проверяются при
применении: опечатка в имени валит бота на старте, а не тихо игнорируется.

Если версия меняет не числа, а саму логику - положите её код рядом
(`bot/breakout_v2.py`) и укажите `engine="bot.breakout_v2:BreakoutEngine"`.
Тогда старая версия продолжит работать на прежнем коде.

В кластере каждая версия - отдельный под со своим томом; см. bots[] в
infrastructure/k8s/base/tradingbot-chart/values.yaml.
"""
from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional

log = logging.getLogger("versions")


@dataclass(frozen=True)
class Version:
    """Одна версия торговой системы."""
    notes: str = ""
    # {секция конфига: {поле: значение}}. Пустая секция "" - корень Config.
    overrides: Dict[str, Dict[str, object]] = field(default_factory=dict)
    # Необязательная замена движка: "модуль:Класс". Нужна, когда версия меняет
    # логику, а не параметры.
    engine: str = ""


# Реестр. Ключ первого уровня - ТС, второго - номер версии без "v".
#
# 0.1 у всех ТС - это поведение "как написано в коде": пустые overrides,
# базовые значения из bot/config.py. Дальше версии добавляются сюда руками.
VERSIONS: Dict[str, Dict[str, Version]] = {
    "impulse": {
        "0.1": Version(notes="Базовая: шорт истощения импульса, параметры из config.py"),
    },
    "density": {
        "0.1": Version(notes="Базовая: лимитка перед плотностью, выход при её съедании"),
    },
    "breakout": {
        "0.1": Version(notes="Базовая: импульсный пробой, вход и выход по активности у уровня"),
        # Своя реализация, а не только числа: 0.1 брала ближайший уровень с
        # любой стороны (годился и давно пройденный) и разрешала вход по обе
        # стороны от него. 0.2 требует уровень строго НА ПРОБОЙ.
        "0.2": Version(
            notes="Уровень только на пробой (цена идёт к нему снизу), тренд строже, "
                  "касаний от 3, активность и у уровня, и на самой монете",
            engine="bot.breakout_v2:BreakoutV2Engine",
        ),
    },
    "btc": {
        "0.1": Version(notes="Базовая: откуп просадки BTC от опоры, тейк +5$ / стоп -2$"),
    },
}


def _sort_key(version: str):
    """Сортировка по номеру: 0.10 идёт после 0.9, а не между 0.1 и 0.2."""
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return (0,)


def available(strategy: str) -> List[str]:
    """Версии ТС по возрастанию номера."""
    return sorted(VERSIONS.get(strategy, {}), key=_sort_key)


def latest(strategy: str) -> str:
    """Самая свежая версия ТС - её берём, если версия не указана явно."""
    versions = available(strategy)
    return versions[-1] if versions else ""


def get(strategy: str, version: str) -> Version:
    known = VERSIONS.get(strategy)
    if not known:
        raise SystemExit(f"Неизвестная ТС: {strategy}. Есть: {', '.join(sorted(VERSIONS))}")
    if version not in known:
        raise SystemExit(
            f"У ТС {strategy} нет версии {version}. Есть: {', '.join(available(strategy))}. "
            f"Новая версия добавляется в bot/versions.py"
        )
    return known[version]


def apply(cfg, strategy: str, version: str = "") -> str:
    """Накладывает overrides версии на конфиг. Возвращает применённую версию.

    Пустая версия означает "последняя": так локальный запуск без ключей всегда
    берёт самое свежее, а в кластере версия задана явно и не съезжает.
    """
    resolved = version or latest(strategy)
    if not resolved:
        return ""

    spec = get(strategy, resolved)
    for section, values in spec.overrides.items():
        target = cfg if not section else getattr(cfg, section, None)
        if target is None:
            raise SystemExit(
                f"{strategy} v{resolved}: в конфиге нет секции '{section}'")
        for key, value in values.items():
            if not hasattr(target, key):
                # Опечатку лучше поймать на старте, чем гадать потом, почему
                # версия торгует ровно как предыдущая.
                raise SystemExit(
                    f"{strategy} v{resolved}: в секции '{section or 'Config'}' "
                    f"нет параметра '{key}'")
            setattr(target, key, value)
        if values:
            log.info("v%s: %s -> %s", resolved, section or "Config", values)
    return resolved


def engine_path(strategy: str, version: str) -> str:
    """Переопределённый движок версии, если он задан."""
    return get(strategy, version).engine if version else ""

"""Словари интерфейса.

Эталон — `uz.py`: ключ, которого там нет, не существует. `ru.py` может
отставать, и тогда человек увидит узбекскую строку. `uz_cyrl.py` содержит
только исключения: остальная кириллица выводится из латиницы правилом.
"""
from app.core.i18n import BASE_LOCALE, DERIVED_LOCALE, catalogue
from app.i18n.ru import TABLE as RU
from app.i18n.uz import TABLE as UZ
from app.i18n.uz_cyrl import OVERRIDES as UZ_CYRL

catalogue.load(BASE_LOCALE, UZ)
catalogue.load("ru", RU)
catalogue.load(DERIVED_LOCALE, UZ_CYRL)

__all__ = ["RU", "UZ", "UZ_CYRL"]

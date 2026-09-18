"""Shared lightweight type aliases reused across modulo packages.

Centralised so connector and core payload-parsing code does not redeclare the
same ``dict[str, Any]`` / ``list[dict[str, Any]]`` aliases in every module.
"""

from __future__ import annotations

from typing import Any

__all__ = ["_DICT_STR_ANY", "_LIST_DICT_STR_ANY"]

type _DICT_STR_ANY = dict[str, Any]
type _LIST_DICT_STR_ANY = list[dict[str, Any]]

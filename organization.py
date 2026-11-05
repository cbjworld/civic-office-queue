"""Shared, editable deployment settings beside the executable."""
import json
import re
import sys
from pathlib import Path


def load(path):
    settings = json.loads(path.read_text(encoding="utf-8-sig"))
    org = settings["organization"]
    rooms = settings["offices"]
    if not isinstance(org, str) or not org.strip() or len(org) > 60:
        raise ValueError("organization: 기관명을 1~60자로 입력하세요.")
    if not isinstance(rooms, list) or not 1 <= len(rooms) <= 24:
        raise ValueError("offices: 부서를 1~24개 입력하세요.")
    for name in rooms:
        if not isinstance(name, str) or not re.fullmatch(r"[가-힣A-Za-z0-9][가-힣A-Za-z0-9 _()-]{0,39}", name) or name != name.strip():
            raise ValueError("부서명에는 한글, 영문, 숫자, 공백, _, -, 괄호를 사용할 수 있습니다.")
    if len({name.casefold() for name in rooms}) != len(rooms):
        raise ValueError("부서명이 중복됩니다.")
    return org.strip(), rooms


config = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().with_name("organization.json")
try:
    ORGANIZATION, ALL_DEPTS = load(config)
except (OSError, ValueError, KeyError, TypeError) as exc:
    if getattr(sys, "frozen", False):
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, f"{config}\n{exc}", "기관 설정 확인", 16)
    raise

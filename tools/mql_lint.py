"""Лёгкий линтер для MQL5: не компилятор, а ловушка опечаток, пока компилирует только Абу (F7).

Проверяет:
 1. баланс скобок (), {}, [] и незакрытые строки;
 2. каждый вызов функции известен: определена в файле, есть в списке встроенных (взят из рабочего
    эталона v3.2 плюс явный список) или вызвана как метод;
 3. число аргументов в вызовах собственных функций (с учётом значений по умолчанию);
 4. функция использована раньше определения без прототипа (в MQL5 лучше этого избегать);
 5. отсутствие `\\r` и непарных `"` в строках.

    python -m tools.mql_lint mt5/MrCasino.mq5 --reference path/to/MrCasino_v32.mq5
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

KEYWORDS = {"if", "for", "while", "switch", "return", "sizeof", "else", "do", "case", "default", "break", "continue",
            "struct", "enum", "input", "sinput", "class", "new", "delete", "input", "group", "define", "include",
            "property", "typename"}
TYPES = r"(?:void|bool|int|uint|long|ulong|short|ushort|char|uchar|double|float|string|datetime|color|" \
        r"ENUM_\w+|SDay|SZone|SSig|SCand|SSet|MqlRates)"

# встроенные функции MQL5, которые добавлены в v3.3 (проверены по документации MQL5)
EXTRA_BUILTINS = """
FolderCreate FileOpen FileClose FileSize FileSeek FileWriteArray FileReadArray FileMove FileDelete FileFindFirst
FileFindNext FileFindClose StringToCharArray CharArrayToString StringGetCharacter ShortToString StringToDouble
StringToInteger TimeToStruct MathIsValidNumber ObjectsTotal ObjectName ObjectGetInteger ObjectGetDouble ObjectCreate
ObjectSetInteger ChartID ChartOpen ChartClose ChartRedraw ChartGetInteger ChartScreenShot Sleep EventSetTimer
EventKillTimer GetTickCount64 TerminalInfoInteger AccountInfoInteger SymbolInfoSessionTrade StringReplace
StringSubstr StringFind StringLen StringFormat IntegerToString DoubleToString ArrayResize ArraySize NormalizeDouble
MathAbs MathMax MathMin MathRound MathFloor PositionGetInteger PositionGetDouble PositionGetString TimeGMT TimeCurrent
SendNotification WebRequest Print GetLastError SymbolInfoDouble SymbolInfoInteger AccountInfoDouble AccountInfoString
ObjectFind ObjectDelete ObjectSetDouble ObjectSetString ObjectsDeleteAll Comment MQLInfoInteger EnumToString
""".split()


def macros(code: str) -> set[str]:
    return set(re.findall(r"^\s*#define\s+(\w+)", code, re.M))


def strip(code: str) -> tuple[str, list[str]]:
    """Убирает комментарии, препроцессорные строки и содержимое строк. Возвращает (скелет, ошибки)."""
    code = re.sub(r"^[ \t]*#[^\n]*", "", code, flags=re.M)
    errs: list[str] = []
    out: list[str] = []
    i, n, line = 0, len(code), 1
    while i < n:
        c = code[i]
        if c == "\n":
            out.append(c)
            line += 1
            i += 1
        elif code.startswith("//", i):
            while i < n and code[i] != "\n":
                i += 1
        elif code.startswith("/*", i):
            j = code.find("*/", i + 2)
            if j < 0:
                errs.append(f"{line}: не закрыт /* */")
                break
            line += code.count("\n", i, j)
            out.append("\n" * code.count("\n", i, j))
            i = j + 2
        elif c == '"':
            j = i + 1
            while j < n and code[j] != '"':
                if code[j] == "\\":
                    j += 1
                if j < n and code[j] == "\n":
                    errs.append(f"{line}: строка не закрыта до конца строки")
                    break
                j += 1
            out.append('""')
            i = j + 1
        elif c == "'":
            j = code.find("'", i + 2 if code[i + 1] != "\\" else i + 3)
            out.append("0")
            i = (j if j > 0 else i + 2) + 1
        else:
            out.append(c)
            i += 1
    return "".join(out), errs


def balance(skel: str) -> list[str]:
    errs, stack = [], []
    pairs = {")": "(", "}": "{", "]": "["}
    line = 1
    for ch in skel:
        if ch == "\n":
            line += 1
        elif ch in "({[":
            stack.append((ch, line))
        elif ch in pairs:
            if not stack or stack[-1][0] != pairs[ch]:
                errs.append(f"{line}: лишняя или несогласованная '{ch}'")
                return errs
            stack.pop()
    for ch, ln in stack:
        errs.append(f"{ln}: не закрыта '{ch}'")
    return errs


def split_args(s: str) -> list[str]:
    out, depth, cur = [], 0, ""
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur)
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur)
    return out


def definitions(skel: str) -> dict[str, tuple[int, int, int]]:
    """имя -> (мин. число аргументов, макс., строка определения). Функции в колонке 0."""
    defs: dict[str, tuple[int, int, int]] = {}
    for m in re.finditer(rf"^(?:{TYPES})\s+(\w+)\s*\(([^)]*)\)\s*(?:;|\{{|\n\s*\{{)", skel, re.M):
        name, params = m.group(1), m.group(2).strip()
        line = skel.count("\n", 0, m.start()) + 1
        ps = split_args(params) if params and params != "void" else []
        mx = len(ps)
        mn = sum(1 for p in ps if "=" not in p)
        if name in defs and defs[name][2] != line:
            defs[name] = (min(defs[name][0], mn), max(defs[name][1], mx), defs[name][2])
        else:
            defs[name] = (mn, mx, line)
    for m in re.finditer(r"^void\s+(On\w+)\s*\(", skel, re.M):
        defs.setdefault(m.group(1), (0, 9, skel.count("\n", 0, m.start()) + 1))
    return defs


def calls(skel: str):
    """(имя, строка, число аргументов) для вызовов функций (не методов, не объявлений)."""
    for m in re.finditer(r"(?<![\w.:])([A-Za-z_]\w*)\s*\(", skel):
        name = m.group(1)
        if name in KEYWORDS:
            continue
        before = skel[max(0, m.start() - 40):m.start()]
        if re.search(rf"(?:^|\n)\s*(?:{TYPES})\s+$", before) or re.search(rf"\b(?:{TYPES})\s+$", before):
            continue                                  # это определение или объявление
        # собрать аргументы до парной скобки
        depth, j, n = 0, m.end() - 1, len(skel)
        while j < n:
            if skel[j] == "(":
                depth += 1
            elif skel[j] == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inner = skel[m.end():j]
        line = skel.count("\n", 0, m.start()) + 1
        yield name, line, (len(split_args(inner)) if inner.strip() else 0)


def lint(path: Path, reference: Path | None) -> int:
    code = path.read_text(encoding="utf-8")
    problems: list[str] = []
    if "\r" in code:
        problems.append("в файле есть \\r (ожидаются LF)")
    skel, errs = strip(code)
    problems += errs + balance(skel)
    defs = definitions(skel)
    known = set(defs) | set(EXTRA_BUILTINS) | macros(code)
    if reference:
        rskel, _ = strip(reference.read_text(encoding="utf-8"))
        rdefs = definitions(rskel)
        rcode = reference.read_text(encoding="utf-8")
        known |= {n for n, _, _ in calls(rskel)} | set(rdefs) | macros(rcode)
    first_use: dict[str, int] = {}
    for name, line, nargs in calls(skel):
        first_use.setdefault(name, line)
        if name not in known and not re.match(r"^(Mql|ENUM)", name):
            problems.append(f"{line}: неизвестная функция {name}()")
        if name in defs:
            mn, mx, _ = defs[name]
            if not (mn <= nargs <= mx):
                problems.append(f"{line}: {name}() вызвана с {nargs} арг., ожидается {mn}..{mx}")
    for name, (mn, mx, dline) in defs.items():
        use = first_use.get(name)
        if use is not None and use < dline and not re.search(rf"^(?:{TYPES})\s+{name}\s*\([^)]*\)\s*;", skel, re.M):
            problems.append(f"{use}: {name}() используется раньше определения (строка {dline}) без прототипа")
    for p in sorted(set(problems), key=lambda s: int(s.split(":")[0]) if s.split(":")[0].isdigit() else 0):
        print("✖", p)
    print(f"{'✔' if not problems else '✖'} {path.name}: функций {len(defs)}, проблем {len(set(problems))}")
    return 1 if problems else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--reference", help="рабочий эталон (например v3.2): его вызовы считаются известными")
    a = ap.parse_args()
    sys.exit(lint(Path(a.file), Path(a.reference) if a.reference else None))

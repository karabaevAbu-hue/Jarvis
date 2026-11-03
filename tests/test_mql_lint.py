"""Линтер MQL5: принимает встроенные функции без эталона и по-прежнему ловит опечатки, число аргументов, скобки."""
from pathlib import Path

import pytest

from tools import mql_lint

OK_CODE = """
int g_n = 0;
double Half(double x)
  {
   return x / 2.0;
  }
void OnTick()
  {
   MqlRates r[];
   int got = CopyRates(_Symbol, PERIOD_M5, 0, 10, r);
   datetime t = iTime(_Symbol, PERIOD_M5, 0);
   bool h = HistorySelect(0, TimeCurrent());
   double p = MathPow(2.0, 3);
   string parts[];
   StringSplit("a,b", ',', parts);
   ArraySort(parts);
   double m = 0;
   OrderCalcMargin(ORDER_TYPE_BUY, _Symbol, 0.01, 1.0, m);
   PositionSelectByTicket(1);
   int k = int(Half(4.0));
   FileMove("a", 0, "b", 0);
  }
"""


def run(tmp_path, code: str, capsys, name="t.mq5") -> tuple[int, str]:
    f = tmp_path / name
    f.write_text(code, encoding="utf-8")
    rc = mql_lint.lint(f, None)
    return rc, capsys.readouterr().out


def test_builtins_are_known_without_a_reference(tmp_path, capsys):
    rc, out = run(tmp_path, OK_CODE, capsys)
    assert rc == 0, out


def test_asked_for_builtins_and_every_group_are_listed():
    for name in ("CopyRates", "iTime", "HistorySelect", "MathPow", "FileMove", "OrderCalcMargin", "SymbolInfoSessionTrade",
                 "PositionSelectByTicket", "ChartScreenShot", "StringSplit", "WebRequest", "GlobalVariableSet", "iATR"):
        assert name in mql_lint.BUILTINS, name
    assert all(g.split() for g in mql_lint._BUILTIN_GROUPS.values())
    assert len(mql_lint.BUILTINS) > 400


def test_typo_in_a_builtin_name_is_still_caught(tmp_path, capsys):
    rc, out = run(tmp_path, OK_CODE.replace("CopyRates(", "CopyRatez("), capsys)
    assert rc == 1 and "неизвестная функция CopyRatez()" in out


def test_wrong_argument_count_of_own_function_is_caught(tmp_path, capsys):
    rc, out = run(tmp_path, OK_CODE.replace("Half(4.0)", "Half(4.0, 5.0)"), capsys)
    assert rc == 1 and "Half() вызвана с 2 арг." in out


def test_call_before_definition_needs_a_prototype(tmp_path, capsys):
    code = "void OnTick()\n  {\n   Later();\n  }\nvoid Later()\n  {\n  }\n"
    rc, out = run(tmp_path, code, capsys)
    assert rc == 1 and "раньше определения" in out
    rc, out = run(tmp_path, "void Later();\n" + code, capsys)
    assert rc == 0, out


def test_unbalanced_brackets_and_carriage_returns_are_caught(tmp_path, capsys):
    rc, out = run(tmp_path, "void OnTick()\n  {\n   Print(1;\n  }\n", capsys)
    assert rc == 1 and "✖" in out
    rc, out = run(tmp_path, "void OnTick()\r\n  {\r\n  }\r\n", capsys)
    assert rc == 1 and "\\r" in out


def test_no_arguments_checks_every_mq5_in_mt5(capsys):
    assert mql_lint.main([]) == 0
    out = capsys.readouterr().out
    assert "MrCasino.mq5" in out and "MrCasinoExport.mq5" in out


def test_missing_files_do_not_pass_silently(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "x.mq5"
    bad.write_text("void OnTick()\n  {\n   Nope();\n  }\n", encoding="utf-8")
    assert mql_lint.main([str(bad)]) == 1
    assert "Nope" in capsys.readouterr().out

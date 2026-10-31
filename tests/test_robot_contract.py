"""Статическая сверка робота v3.3 (mt5/MrCasino.mq5) с контрактом ТЗ и с кодом ассистента.

Компилятора MQL5 здесь нет, поэтому тест читает исходник и проверяет, что в нём есть всё, на что
рассчитан ассистент: события, команды, тревоги, диапазоны, колонки trades.csv, ключи status.json.
Если робот и ассистент разойдутся, тест упадёт до выхода на живой счёт.
"""
import re
from pathlib import Path

import pytest

from agent.bridge.protocol import ALL_CMDS, OPEN_SL_MAX_PTS, SET_RANGES
from agent.bridge.trades import COLUMNS
from tools.fake_ea import BOT_ALIVE_SEC, FakeEA
from tools import mql_lint
from agent.bridge.protocol import BridgePaths

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "mt5" / "MrCasino.mq5").read_text(encoding="utf-8")
OLD = ROOT / "mt5" / "archive" / "MrCasino_v3.2.mq5"


def has_key(body: str, key: str) -> bool:
    """Ключ JSON в исходнике: JS("key", ...) или собранный вручную \\"key\\":."""
    return f'"{key}"' in body or f'\\"{key}\\"' in body


def fn_body(name: str) -> str:
    """Тело функции верхнего уровня по имени (до закрывающей скобки в колонке 2)."""
    m = re.search(rf"^[A-Za-z_][\w ]*\b{name}\s*\([^)]*\)\s*\n\s*\{{\n(.*?)\n  \}}\n", SRC, re.S | re.M)
    assert m, f"функция {name} не найдена"
    return m.group(1)


def test_version_and_encoding():
    assert '#property version   "3.30"' in SRC
    raw = (ROOT / "mt5" / "MrCasino.mq5").read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf" and b"\r" not in raw


def test_v32_original_is_kept_untouched():
    import hashlib
    assert hashlib.sha256(OLD.read_bytes()).hexdigest() == "48d0347a4b5a54007b5ba702371018a912d5ad1dc76979cf6dc2baa65ac109c7"


def test_linter_is_clean():
    assert mql_lint.lint(ROOT / "mt5" / "MrCasino.mq5", OLD) == 0


@pytest.mark.parametrize("name,default", [
    ("InpBridge", "true"), ("InpBridgeKey", '""'), ("InpStatusSec", "10"), ("InpShots", "true"), ("InpShotW", "1280"),
    ("InpShotH", "720"), ("InpMorningHM", '"09:30"'), ("InpReportHM", '"23:05"'), ("InpTgFallback", "true"),
    ("InpAlertDDPct", "3.0"), ("InpAlertSpreadX", "3.0")])
def test_new_inputs_match_section_5_1(name, default):
    m = re.search(rf"^input\s+\w+\s+{name}\s*=\s*([^;]+);", SRC, re.M)
    assert m and m.group(1).strip() == default


@pytest.mark.parametrize("etype", [
    "trade_opened", "trade_be", "trade_modified", "trade_partial", "trade_closed", "limit_reached", "alert",
    "daily_report", "morning_brief", "cmd_result", "ea_started", "ea_stopped", "zone_confirmed", "zone_touch",
    "signal_skipped", "bias_changed", "zone_invalid", "analysis_skip", "order_rejected"])
def test_every_event_type_of_section_7_is_emitted(etype):
    assert re.search(rf'(?:Emit\(|[?:]\s*)"{etype}"', SRC), etype


@pytest.mark.parametrize("key", ["disconnect", "algo_off", "no_sl", "orders_rejected", "day_dd", "day_limit", "spread",
                                 "no_ticks", "margin", "friday_close"])
def test_every_alert_of_section_5_8(key):
    assert f'AlertSet("{key}"' in SRC


def test_critical_severity_matches_the_table():
    for key in ("disconnect", "algo_off", "no_sl", "orders_rejected", "day_limit"):
        assert re.search(rf'AlertSet\("{key}",[^;]*"critical"', SRC, re.S), key
    for key in ("day_dd", "spread", "no_ticks", "margin"):
        assert re.search(rf'AlertSet\("{key}",[^;]*"warning"', SRC, re.S), key
    assert re.search(r'AlertSet\("friday_close",[^;]*"info"', SRC, re.S)


def test_every_command_is_executed():
    body = fn_body("ExecCmd")
    for cmd in ALL_CMDS:
        assert f'cmd == "{cmd}"' in body, cmd


def test_command_ranges_match_protocol():
    ex = fn_body("ExecSet")
    r = SET_RANGES
    assert re.search(rf"risk < {r['risk_pct'][0]} \|\| risk > {r['risk_pct'][1]}", ex)
    assert re.search(rf"daily < {r['daily_risk_pct'][0]} \|\| daily > {r['daily_risk_pct'][1]}", ex)
    assert re.search(rf"mx < {r['max_trades'][0]} \|\| mx > {r['max_trades'][1]}", ex)
    assert re.search(rf"rr < {r['rr'][0]} \|\| rr > {r['rr'][1]}", ex)
    op = fn_body("ExecOpen")
    assert f"slPts > {OPEN_SL_MAX_PTS}" in op
    assert "slPts <= 0" in op and "riskArg < 0.1 || riskArg > 5.0" in op and "rrArg < 1.0 || rrArg > 5.0" in op


def test_unsafe_defaults_of_the_bridge():
    run = fn_body("RunCommand")
    assert 'InpBridgeKey == ""' in run                          # пустой ключ: команды не принимаются
    assert "неверный ключ моста" in run and "команда устарела" in run
    assert "<= 30" in fn_body("ReadHeartbeat") and BOT_ALIVE_SEC == 30
    assert "> 180" in fn_body("ReadHeartbeat")                    # бот молчит 3 минуты -> запасной канал


def test_trades_csv_header_equals_python_columns():
    m = re.search(r'#define TRADES_HEADER\s+"([^"]+)"', SRC)
    assert m and m.group(1).split(",") == COLUMNS


def _leaf_keys(d, out=None):
    out = set() if out is None else out
    for k, v in d.items():
        out.add(k)
        if isinstance(v, dict):
            _leaf_keys(v, out)
    return out


def test_status_json_has_every_key_of_the_contract(tmp_path):
    ea = FakeEA(BridgePaths(tmp_path, "1_1"), "k")
    ea.open_position("BUY", 5.5)
    body = fn_body("WriteStatus") + fn_body("PositionJson") + fn_body("ZonesJson")
    for key in sorted(_leaf_keys(ea.status_dict())):
        if key in ("zones",):
            continue
        assert has_key(body, key), f"в WriteStatus нет ключа {key}"
    for zk in ("dir", "lo", "hi", "status", "score", "srcs", "htf", "dist"):
        assert has_key(fn_body("ZonesJson"), zk)


def test_event_envelope_and_fields_used_by_templates():
    emit = fn_body("Emit")
    for k in ("seq", "ts_utc", "ts_local", "type", "level", "severity", "robot", "data", "text", "shot"):
        assert has_key(emit, k), k
    for k in ("symbol", "ticket", "side", "entry", "sl", "tp", "lot", "risk_usd", "risk_pct", "trigger", "mark", "fu_class",
              "type", "manual", "weak", "strength", "zone_lo", "zone_hi", "score", "srcs", "htf", "session", "rr", "sl_src",
              "sl_pts", "tp_pts"):
        assert f'"{k}"' in SRC, f"trade_opened: нет поля {k}"
    for k in ("exit_price", "pts", "usd", "exit", "duration_min"):
        assert f'"{k}"' in SRC, f"trade_closed: нет поля {k}"
    for k in ("key", "state", "severity", "reason", "retcode", "closed_lot", "left_lot"):
        assert f'"{k}"' in SRC


def test_no_blocking_or_network_calls_in_ontick():
    tick = re.search(r"void OnTick\(\)\n  \{\n(.*?)\n  \}\n", SRC, re.S).group(1)
    for bad in ("Sleep(", "WebRequest(", "SendTG(", "FileOpen(", "ChartOpen("):
        assert bad not in tick, bad
    # Sleep только при скрине на временном графике; WebRequest только в запасном канале
    assert SRC.count("Sleep(") == 2 and "Sleep(" in fn_body("TakeShot")
    assert SRC.count("WebRequest(") == 1 and "WebRequest(" in fn_body("SendTG")
    assert "DrainTG();" in fn_body("OnTimer") and "ProcessCommands();" in fn_body("OnTimer")


def test_all_bridge_files_use_the_common_folder():
    for name in ("WriteAtomic", "AppendUtf8", "ReadUtf8"):
        body = fn_body(name)
        assert "FILE_COMMON" in body and "FILE_BIN" in body, name
    assert "FILE_COMMON" in fn_body("ProcessCommands") and "FILE_COMMON" in fn_body("EnsureDirs")
    assert "FILE_REWRITE" in fn_body("WriteAtomic") and ".tmp" in fn_body("WriteAtomic")


def test_trading_logic_of_v32_is_unchanged():
    """Входы, стопы, тейки и фильтры v3.2 не менялись: тела этих функций совпадают с v3.2 дословно
    с точностью до замены чтения параметров на g_set и новых событий."""
    old = OLD.read_text(encoding="utf-8")

    def body_of(text, name):
        m = re.search(rf"^[A-Za-z_][\w ]*\b{name}\s*\([^)]*\)\s*\n\s*\{{\n(.*?)\n  \}}\n", text, re.S | re.M)
        return m.group(1) if m else None

    def norm(b):
        b = re.sub(r"\bInp(MaxTradesDay|DailyRiskPct|RR|WeakSignals|TradeBuy|TradeSell)\b", lambda m: "g_set." + {
            "MaxTradesDay": "maxTrades", "DailyRiskPct": "dailyRiskPct", "RR": "rr", "WeakSignals": "weak",
            "TradeBuy": "tradeBuy", "TradeSell": "tradeSell"}[m.group(1)], b)
        return b

    same = ["IsFU", "FUClass", "Negation", "IsEngulf", "IsBodyCover", "IsLowerWick", "IsBIW", "CalcBias", "TradeType",
            "FindHTFLevels", "HtfMatch", "ZoneInFVG", "InsideFVG", "GenOB", "GenFU", "GenLiq", "CandStrength",
            "NarrowGroup", "MaxOverlap", "SelectTradable", "PruneZones", "TriggerA", "TriggerB", "TriggerC", "TriggerD",
            "TriggerE", "TriggerF", "TriggerS", "TriggerK", "TriggerDirect", "EvalZone", "MinStopDist", "ChooseTP",
            "AdvanceNew", "CheckInvalidM15", "ReplayZone", "RebuildZones", "UpdateZoneStates", "Indecision", "ImpulseInto",
            "BEAt", "InRegion", "CheckEarlyExit", "CalcDay"]
    for name in same:
        a, b = body_of(old, name), body_of(SRC, name)
        assert a is not None and b is not None, name
        a, b = norm(a), norm(b)
        if name in ("RebuildZones",):
            b = b.replace("InpTradeBuy", "g_set.tradeBuy")
        assert a == b, f"изменилась торговая логика: {name}"

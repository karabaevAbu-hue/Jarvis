"""Статическая сверка робота v3.4 (mt5/MrCasino.mq5) с контрактом ТЗ и с кодом ассистента.

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
V33 = ROOT / "mt5" / "archive" / "MrCasino_v3.3.mq5"


def has_key(body: str, key: str) -> bool:
    """Ключ JSON в исходнике: JS("key", ...) или собранный вручную \\"key\\":."""
    return f'"{key}"' in body or f'\\"{key}\\"' in body


def fn_body(name: str) -> str:
    """Тело функции верхнего уровня по имени (до закрывающей скобки в колонке 2)."""
    m = re.search(rf"^[A-Za-z_][\w ]*\b{name}\s*\([^)]*\)\s*\n\s*\{{\n(.*?)\n  \}}\n", SRC, re.S | re.M)
    assert m, f"функция {name} не найдена"
    return m.group(1)


def test_version_and_encoding():
    assert '#property version   "3.40"' in SRC and SRC.count('JS("version", "3.40")') == 2
    raw = (ROOT / "mt5" / "MrCasino.mq5").read_bytes()
    assert raw[:3] != b"\xef\xbb\xbf" and b"\r" not in raw


def test_v32_original_is_kept_untouched():
    import hashlib
    assert hashlib.sha256(OLD.read_bytes()).hexdigest() == "48d0347a4b5a54007b5ba702371018a912d5ad1dc76979cf6dc2baa65ac109c7"


def test_v33_archive_is_kept_untouched():
    import hashlib
    assert hashlib.sha256(V33.read_bytes()).hexdigest() == "aef0c092e216230e9433284c485705dba0e5355f0b48da72b7a18d4124e78ead"


def test_linter_is_clean():
    assert mql_lint.lint(ROOT / "mt5" / "MrCasino.mq5", OLD) == 0


def test_linter_without_arguments_checks_every_mt5_file_without_a_reference():
    assert mql_lint.main([]) == 0
    assert mql_lint.main([str(ROOT / "mt5" / "MrCasino.mq5")]) == 0           # и без эталона v3.2


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
                                 "no_ticks", "margin", "friday_close", "equity_stop"])
def test_every_alert_of_section_5_8(key):
    assert f'AlertSet("{key}"' in SRC


def test_critical_severity_matches_the_table():
    for key in ("disconnect", "algo_off", "no_sl", "orders_rejected", "day_limit", "equity_stop"):
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
            "TriggerE", "TriggerF", "TriggerS", "TriggerK", "TriggerDirect", "MinStopDist", "ChooseTP",
            "AdvanceNew", "CheckInvalidM15", "ReplayZone", "RebuildZones", "UpdateZoneStates", "Indecision", "ImpulseInto",
            "BEAt", "InRegion", "CheckEarlyExit", "CalcDay"]
    for name in same:
        a, b = body_of(old, name), body_of(SRC, name)
        assert a is not None and b is not None, name
        a, b = norm(a), norm(b)
        if name in ("RebuildZones",):
            b = b.replace("InpTradeBuy", "g_set.tradeBuy")
        assert a == b, f"изменилась торговая логика: {name}"


def _funcs(text: str) -> dict[str, str]:
    pat = re.compile(r"^(?:[A-Za-z_][\w:<>&\* ]*?)\s+(\w+)\s*\(([^)]*)\)\s*\n\s*\{\n(.*?)\n  \}\n", re.S | re.M)
    return {m.group(1): m.group(3) for m in pat.finditer(text)}


def _changed_lines(old: str, new: str) -> set[str]:
    import difflib
    out = set()
    for ln in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0):
        if ln.startswith(("+++", "---", "@@")):
            continue
        out.add(ln[0] + ln[1:].strip())
    return out


def test_evalzone_is_v32_logic_with_only_the_signal_init_changed():
    a, b = _funcs(OLD.read_text(encoding="utf-8"))["EvalZone"], fn_body("EvalZone")
    assert a.split("t.ok = false;\n", 1)[1] == b.split('string w = "";\n', 1)[1]          # все триггеры и баллы дословно


def test_v34_changes_exactly_the_functions_that_were_asked_for():
    """Строгий охранник «логику входов не менять»: набор изменённых функций v3.4 относительно v3.3 задан ровно."""
    old, new = _funcs(V33.read_text(encoding="utf-8")), _funcs(SRC)
    assert set(old) - set(new) == set(), "функции v3.3 пропали"
    assert set(new) - set(old) == {"CheckEquityStop", "EquityBelowStop", "EquityStopText", "LotDigits", "NormLot", "SigInit"}
    changed = {n for n in old if old[n] != new[n]}
    assert changed == {"BridgeLine", "CalcLot", "ClosePosNow", "DrawPanel", "EvalZone", "ExecCmd", "ExecOpen", "GlobalBlock",
                       "ManagePosition", "OnInit", "OnTick", "OnTimer", "Skip", "TryEntry", "WriteStatus"}
    # решения о входе: правки только такие
    assert _changed_lines(old["TryEntry"], new["TryEntry"]) == {"-bs.ok = false;", "+SigInit(bs);", "+SigInit(s);"}
    assert _changed_lines(old["CalcLot"], new["CalcLot"]) == {
        "-lot = NormalizeDouble(lot, 2);", "+lot = NormLot(lot);",
        '+if(lot <= 0) { why = "лот после округления по шагу символа меньше минимального"; return 0; }'}
    # ManagePosition: правка только в блоке частичного закрытия (объём по шагу символа и в событии); сопровождение не тронуто
    mp = _changed_lines(old["ManagePosition"], new["ManagePosition"])
    assert mp == {
        "-double cv = MathFloor(vol / 2.0 / step + 1e-9) * step;",
        "+double cv = NormLot(vol / 2.0);                  // округлённый по шагу символа объём: его и закрываем, его и пишем в событие",
        "+int ld = LotDigits(step);",
        "-if(g_trade.PositionClosePartial(tk, NormalizeDouble(cv, 2)))", "+if(g_trade.PositionClosePartial(tk, cv))",
        '-Emit("trade_partial", 0, "info", JI("ticket", g_pId) + "," + JN("closed_lot", cv, 2) + "," + JN("left_lot", vol - cv, 2),',
        '-StringFormat("50%% позиции закрыто на первой LAL: %.2f лота, осталось %.2f", cv, vol - cv), -1);',
        "+double left = NormalizeDouble(vol - cv, ld);",
        '+Emit("trade_partial", 0, "info", JI("ticket", g_pId) + "," + JN("closed_lot", cv, ld) + "," + JN("left_lot", left, ld),',
        '+StringFormat("50%% позиции закрыто на первой LAL: %s лота, осталось %s", DoubleToString(cv, ld), DoubleToString(left, ld)), -1);'}
    assert _changed_lines(old["OnTick"], new["OnTick"]) == {"+CheckEquityStop();"}
    gb = _changed_lines(old["GlobalBlock"], new["GlobalBlock"])
    assert gb == {'+if(EquityBelowStop()) return "жёсткий стоп по эквити: " + EquityStopText();',
                  '-if(spread > InpMaxSpread) return StringFormat("спред %.2f > %.2f", spread, InpMaxSpread);',
                  '+if(spread > InpMaxSpread) return StringFormat("спред %.2f > максимума %.2f (InpMaxSpreadPt)", spread, InpMaxSpread);'}


def test_default_risk_and_entry_limits_are_unchanged_from_v33():
    for name in ("InpRiskMode", "InpRiskPercent", "InpDailyRiskPct", "InpMinLotIfFits", "InpMarginReserve", "InpMaxTradesDay",
                 "InpMinSLPt", "InpMaxSLPt", "InpRR", "InpMaxSpreadPt", "InpMinScoreWith", "InpMinScoreMixed", "InpMinScoreCounter",
                 "InpTrueStop", "InpBEFrac", "InpUseTrigA", "InpUseTrigB", "InpWeakSignals", "InpSessionStart", "InpSessionEnd"):
        pat = rf"^input\s+\w+\s+{name}\s*=\s*([^;]+);"
        a = re.search(pat, V33.read_text(encoding="utf-8"), re.M)
        b = re.search(pat, SRC, re.M)
        assert a and b and a.group(1) == b.group(1), name
    assert re.search(r"^input\s+double\s+InpRiskPercent\s*=\s*0\.0;", SRC, re.M)      # 0 = риск в день / сделок = 1% на сделку
    assert re.search(r"^input\s+int\s+InpMaxTradesDay\s*=\s*5;", SRC, re.M)
    assert re.search(r"^input\s+double\s+InpDailyRiskPct\s*=\s*5\.0;", SRC, re.M)       # 5% / 5 сделок = 1%


def _input_default(name: str) -> str:
    m = re.search(rf"^input\s+\w+\s+{name}\s*=\s*([^;]+);", SRC, re.M)
    assert m, name
    return m.group(1).strip()


def test_port_defaults_equal_robot_inputs():
    """Сверка не должна разъезжаться из-за разных настроек: RobotParams повторяет входные параметры робота."""
    from agent.analysis.robot_port import RobotParams
    p = RobotParams()
    pairs = {"zone_radius": "InpZoneRadiusPt", "max_zones_side": "InpMaxZonesSide", "use_m15_ob": "InpUseM15OB",
             "ob_wick_pct": "InpOBWickPct", "ob_move_bars": "InpOBMoveBars", "ob_need_fu": "InpOBNeedFU",
             "fu_zone_min_wick": "InpFUZoneMinWickPt", "sweep_zone": "InpSweepZonePt", "eq_tol": "InpEqTolPt",
             "zone_max": "InpZoneMaxPt", "zone_min": "InpZoneMinPt", "confirm_away": "InpConfirmAwayPt",
             "invalid_beyond": "InpInvalidBeyondPt", "zone_life_hours": "InpZoneLifeHours", "zone_life_m15": "InpZoneLifeM15",
             "doji_body_pct": "InpDojiBodyPct", "wick_pct": "InpWickPct", "lq_min": "InpLqMinPt", "min_sl": "InpMinSLPt",
             "max_sl": "InpMaxSLPt", "sl_buffer": "InpSLBufferPt", "true_stop": "InpTrueStop", "rr": "InpRR"}
    for field, inp in pairs.items():
        raw = _input_default(inp)
        want = {"true": True, "false": False}.get(raw, None)
        got = getattr(p, field)
        assert (got is want) if want is not None else float(raw) == float(got), f"{field} {got!r} != {inp} {raw}"


# ---------------------------------------------------------------- v3.4: что именно просили
def test_m15_order_blocks_are_off_by_default_and_m15_stays_for_fu_liquidity_and_entries():
    assert _input_default("InpUseM15OB") == "false"
    rb = fn_body("RebuildZones")
    assert "GenOB(PERIOD_H4, d, c);" in rb and "GenOB(PERIOD_H1, d, c);" in rb and "if(InpUseM15OB) GenOB(PERIOD_M15, d, c);" in rb
    assert "GenFU(PERIOD_M15, d, c);" in rb and "GenLiq(PERIOD_M15, d, c);" in rb      # M15 для входов и ликвидности остаётся
    from agent.analysis.robot_port import RobotParams
    assert RobotParams().use_m15_ob is False


def test_equity_stop_closes_pauses_and_alerts():
    assert _input_default("InpEquityStopUSD") == "0.0"                    # по умолчанию выключен, робот это показывает
    cs = fn_body("CheckEquityStop")
    assert "InpEquityStopUSD <= 0) return" in cs
    assert 'ClosePosNow(tk, "EQSTOP")' in cs and "g_set.paused = true;" in cs and "SaveSettings();" in cs
    assert re.search(r'AlertSet\("equity_stop", true, "critical"', cs)
    assert "GetTickCount64() - g_eqCloseMs >= 1000" in cs                 # повтор закрытия, пока позиция не закрыта
    tick, timer = fn_body("OnTick"), fn_body("OnTimer")
    assert tick.index("CheckEquityStop();") < tick.index("ManagePosition();")     # стоп раньше сопровождения
    assert "CheckEquityStop();" in timer                                           # и раз в секунду без тиков
    assert "EquityBelowStop()" in fn_body("GlobalBlock") and "EquityBelowStop()" in fn_body("ExecOpen")
    ex = fn_body("ExecCmd")
    assert re.search(r'cmd == "resume"\)\s*\n\s*\{\n\s*if\(EquityBelowStop\(\)\)', ex)       # resume не снимает паузу, пока стоп держится
    assert 'EQSTOP' in SRC and "equity_stop_usd" in fn_body("WriteStatus")
    assert "⚠️ Стоп по эквити выключен" in fn_body("BridgeLine") and "жёсткий стоп по эквити выключен" in fn_body("OnInit")


def test_equity_stop_hysteresis_clears_the_alert_only_2_percent_above_the_threshold():
    assert re.search(r"#define EQ_STOP_HYST\s+1\.02\b", SRC)
    below = fn_body("EquityBelowStop")
    assert "eq < InpEquityStopUSD || (g_eqTripped && eq < InpEquityStopUSD * EQ_STOP_HYST)" in below
    cs = fn_body("CheckEquityStop")
    assert "if(eq < InpEquityStopUSD) g_eqTripped = true;" in cs
    assert "else if(eq >= InpEquityStopUSD * EQ_STOP_HYST) g_eqTripped = false;" in cs
    # тревога закрывается только когда стоп снят (после возврата на +2%), а не на пороге
    off = cs[cs.index("if(!g_eqTripped)"):cs.index("string why = EquityStopText();")]
    assert 'AlertSet("equity_stop", false' in off and "return;" in off
    # после перезапуска робота в зоне гистерезиса состояние восстанавливается из паузы с причиной «стоп по эквити»
    init = fn_body("OnInit")
    assert "g_eqTripped = InpEquityStopUSD > 0 && g_set.paused && StringFind(g_pauseReason, EQ_STOP_REASON) == 0" in init
    assert "AccountInfoDouble(ACCOUNT_EQUITY) < InpEquityStopUSD * EQ_STOP_HYST" in init
    # текст в зоне гистерезиса честный: эквити уже выше порога, но стоп держится
    txt = fn_body("EquityStopText")
    assert "ниже жёсткого стопа" in txt and "ещё не вернулось выше жёсткого стопа" in txt and "EQ_STOP_HYST" in txt


def test_equity_stop_always_sets_its_own_pause_reason_even_over_an_older_pause():
    assert '#define EQ_STOP_REASON "жёсткий стоп по эквити"' in SRC
    cs = fn_body("CheckEquityStop")
    cond = re.search(r"if\((!g_set\.paused[^\n]*)\)\s*\n\s*\{", cs).group(1)
    # пауза не наша (ни паузы, ни причины «стоп по эквити») или это новое срабатывание: причина перезаписывается
    assert cond == '!g_set.paused || StringFind(g_pauseReason, EQ_STOP_REASON) != 0 || !AlertIsOn("equity_stop")'
    assert "g_pauseReason = EQ_STOP_REASON" in cs and "причина прежней паузы заменена" in cs
    assert cs.index("g_pauseReason = EQ_STOP_REASON") < cs.index('AlertSet("equity_stop", true')    # условие считается до открытия тревоги
    # причину пишем один раз на срабатывание, а не на каждом тике: SaveSettings() не дёргается постоянно
    assert cs.count("SaveSettings();") == 1


def test_close_failed_message_is_written_at_most_once_a_minute():
    cp = fn_body("ClosePosNow")
    assert "g_closeFailMs == 0 || GetTickCount64() - g_closeFailMs >= 60000" in cp
    assert cp.count("Print(") == 1 and "Закрытие не удалось" in cp
    assert cp.index("g_closeFailMs = GetTickCount64();") < cp.index('Print("Закрытие не удалось')
    assert "g_closeFailN = 0;" in cp and "g_closeFailN++;" in cp           # счётчик неудач подряд сбрасывается успехом
    assert 'Print("Закрытие не удалось' not in SRC.replace(cp, "")         # других мест с этим сообщением нет


def test_equity_stop_vocabulary_is_known_to_the_assistant():
    from agent.notifier import EMERGENCY_PAUSE_KEYS
    from agent.templates import ALERT_TITLES, CRITICAL_KEYS
    assert "equity_stop" in ALERT_TITLES and "equity_stop" in CRITICAL_KEYS
    assert "equity_stop" not in EMERGENCY_PAUSE_KEYS                    # робот паузу уже поставил сам


def test_manual_open_checks_min_and_max_sl_like_the_automatic_entry():
    op = fn_body("ExecOpen")
    assert "dist > InpMaxSL" in op and "InpMaxSLPt" in op
    assert "dist < InpMinSL" in op and "InpMinSLPt" in op                  # v3.4: и минимальный стоп
    assert op.index("dist > InpMaxSL") < op.index("CalcLot(") and op.index("dist < InpMinSL") < op.index("CalcLot(")   # до расчёта лота и ордера
    assert "InpMinSL" in fn_body("TryEntry")                               # у автоматического входа тот же нижний предел
    auto = fn_body("TryEntry")
    assert "dist > InpMaxSL" in auto                                       # то же условие у автоматического входа
    assert f"slPts > {OPEN_SL_MAX_PTS}" in op                              # общий предел 50 pt остаётся


def test_lot_follows_symbol_volume_step():
    nl = fn_body("NormLot")
    for token in ("SYMBOL_VOLUME_STEP", "SYMBOL_VOLUME_MIN", "SYMBOL_VOLUME_MAX", "LotDigits(step)"):
        assert token in nl, token
    code = mql_lint.strip(SRC)[0]                                          # без комментариев: в них эта строка объясняется
    assert not re.search(r"NormalizeDouble\((lot|cv),\s*2\)", code)            # жёсткие 2 знака запрещены, нужны знаки шага
    assert "NormLot(lot)" in fn_body("CalcLot")
    mp = fn_body("ManagePosition")
    assert "double cv = NormLot(vol / 2.0);" in mp                         # округлённый объём один раз
    assert "PositionClosePartial(tk, cv)" in mp                            # его и закрываем
    assert 'JN("closed_lot", cv, ld)' in mp and 'JN("left_lot", left, ld)' in mp and "double left = NormalizeDouble(vol - cv, ld);" in mp
    assert 'JN("closed_lot", cv, 2)' not in mp                             # ни исходного 50%, ни фиксированных 2 знаков
    assert "step * MathPow(10.0, n)" in fn_body("LotDigits")


def test_every_ssig_local_is_fully_initialised():
    si = fn_body("SigInit")
    for field in ("ok", "trig", "cls", "strong", "pts", "ext", "mark", "t"):
        assert f"s.{field} =" in si, field
    for m in re.finditer(r"^\s+SSig ([\w, ]+);", SRC, re.M):                      # локальные переменные SSig
        for var in [v.strip() for v in m.group(1).split(",")]:
            after = SRC[m.end():m.end() + 400]
            assert f"SigInit({var});" in after, f"SSig {var} не инициализирована"
    # struct SSig должна содержать ровно те поля, что сбрасывает SigInit
    body = re.search(r"struct SSig\s*\{(.*?)\};", SRC, re.S).group(1)
    fields = re.findall(r"^\s+\w+\s+(\w+);", body, re.M)
    assert sorted(fields) == sorted(["ok", "trig", "cls", "strong", "pts", "ext", "mark", "t"])


def test_skip_by_spread_reports_the_real_spread():
    assert _input_default("InpMaxSpreadPt") == "0.5"                       # input на месте
    assert "спред %.2f > максимума %.2f (InpMaxSpreadPt)" in fn_body("GlobalBlock")
    assert "спред %.2f > максимума %.2f (InpMaxSpreadPt)" in fn_body("ExecOpen")
    sk = fn_body("Skip")
    assert 'JN("spread", SymbolInfoDouble(g_sym, SYMBOL_ASK) - SymbolInfoDouble(g_sym, SYMBOL_BID), 2)' in sk   # живой спред
    assert 'JN("max_spread", InpMaxSpread, 2)' in sk and "g_curSpread" not in SRC
    assert 'StringFind(why, "спред ") == 0' in sk                           # дрожащий спред не плодит события
    assert "Спред сейчас" in fn_body("OnInit")                              # цифра для настройки на демо в журнале при старте

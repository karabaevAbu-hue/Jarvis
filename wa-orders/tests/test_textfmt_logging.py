import logging
import random

from app.logging_setup import MaskingFormatter, mask_secrets
from app.textfmt import build_summary, pick_header, split_orders, tg_html_to_wa


def test_tg_html_to_wa_formatting():
    raw = '<b>Ташкент - Ош</b>\nТент 120 &amp; <i>срочно</i> <s>700$</s> <a href="https://x.kg">ссылка</a>'
    assert tg_html_to_wa(raw) == "*Ташкент - Ош*\nТент 120 & _срочно_ ~700$~ ссылка"


def test_build_summary_layout():
    text = build_summary(["Ташкент - Ош\nТент 120", "Москва - Бишкек"], "Актуальные заказы", "📞 +996 555 000 000")
    assert text == (
        "*Актуальные заказы*\n\n1️⃣ Ташкент - Ош\nТент 120\n\n2️⃣ Москва - Бишкек\n\n📞 +996 555 000 000"
    )


def test_build_summary_without_header_and_footer():
    assert build_summary(["A"]) == "1️⃣ A"


def test_pick_header_changes_between_sends():
    rng = random.Random(1)
    headers = ["Шапка 1", "Шапка 2", ""]
    for last in (1, 2):
        index, _ = pick_header(headers, last, rng)
        assert index != last
    assert pick_header(["", "", ""], None, rng) == (None, "")
    assert pick_header(["Одна", "", ""], 1, rng) == (1, "Одна")


def test_split_orders_by_blank_line():
    assert split_orders("Заказ 1\nстрока 2\n\n  \nЗаказ 2\n\nЗаказ 3") == ["Заказ 1\nстрока 2", "Заказ 2", "Заказ 3"]


def test_mask_secrets_in_urls_and_tokens():
    token = "abcdef1234567890abcdef1234567890abcdef12345678"
    tg = "123456789:AAEhBP0av18bY8g5Zr-7jsdhGjkS0dfOOOq"
    text = f"GET https://7103.api.greenapi.com/waInstance7103123456/sendMessage/{token} bot{tg}"
    masked = mask_secrets(text, [token, tg])
    assert token not in masked and tg not in masked
    assert "waInstance7103123456/sendMessage/" in masked


def test_masking_formatter_hides_token_in_traceback():
    token = "secretTOKEN1234567890secretTOKEN"
    formatter = MaskingFormatter("%(message)s", [token])
    try:
        raise RuntimeError(f"boom {token}")
    except RuntimeError:
        record = logging.LogRecord("t", logging.ERROR, __file__, 1, "ошибка", None, exc_info=True)
        import sys

        record.exc_info = sys.exc_info()
    assert token not in formatter.format(record)

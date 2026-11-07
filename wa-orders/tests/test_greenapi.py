import json

import httpx
import pytest

from app.greenapi import GreenAPI, GreenAPIError

TOKEN = "tok123456789012345678901234567890"


def make_api(handler):
    return GreenAPI(
        "https://7103.api.greenapi.com",
        "https://7103.media.greenapi.com",
        "7103123456",
        TOKEN,
        transport=httpx.MockTransport(handler),
    )


async def test_send_message_url_and_body():
    seen = {}

    def handler(request: httpx.Request):
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"idMessage": "BAE5"})

    api = make_api(handler)
    assert await api.send_message("120363@g.us", "привет") == "BAE5"
    assert seen["method"] == "POST"
    assert seen["url"] == f"https://7103.api.greenapi.com/waInstance7103123456/sendMessage/{TOKEN}"
    assert seen["body"] == {"chatId": "120363@g.us", "message": "привет"}
    await api.close()


async def test_upload_goes_to_media_host(tmp_path):
    seen = {}

    def handler(request: httpx.Request):
        seen["url"] = str(request.url)
        seen["ctype"] = request.headers["content-type"]
        return httpx.Response(200, json={"idMessage": "X", "urlFile": "https://f/1.jpg"})

    photo = tmp_path / "order.jpg"
    photo.write_bytes(b"\xff\xd8fake")
    api = make_api(handler)
    assert await api.send_file_by_upload("1@g.us", photo, "подпись") == ("X", "https://f/1.jpg")
    assert seen["url"].startswith("https://7103.media.greenapi.com/waInstance7103123456/sendFileByUpload/")
    assert seen["ctype"].startswith("multipart/form-data")
    await api.close()


async def test_notifications_and_delete():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, str(request.url)))
        if "receiveNotification" in str(request.url):
            return httpx.Response(200, json={"receiptId": 42, "body": {"typeWebhook": "stateInstanceChanged"}})
        return httpx.Response(200, json={"result": True})

    api = make_api(handler)
    item = await api.receive_notification(5)
    assert item["receiptId"] == 42
    await api.delete_notification(42)
    assert calls[0][1].endswith("receiveNotification/" + TOKEN + "?receiveTimeout=5")
    assert calls[1] == ("DELETE", f"https://7103.api.greenapi.com/waInstance7103123456/deleteNotification/{TOKEN}/42")
    await api.close()


async def test_empty_notification_is_none():
    api = make_api(lambda r: httpx.Response(200, content=b"null"))
    assert await api.receive_notification(1) is None
    await api.close()


async def test_get_groups_filters_groups():
    chats = [
        {"id": "996555@c.us", "name": "Человек", "type": "user"},
        {"id": "120363@g.us", "name": "Ош логистика", "type": "group"},
    ]
    api = make_api(lambda r: httpx.Response(200, json=chats))
    assert [g["name"] for g in await api.get_groups()] == ["Ош логистика"]
    await api.close()


@pytest.mark.parametrize(
    "status,attr",
    [(466, "quota"), (429, "rate_limited"), (401, "bad_credentials"), (502, "maybe_sent")],
)
async def test_error_mapping(status, attr):
    api = make_api(lambda r: httpx.Response(status, text="err"))
    with pytest.raises(GreenAPIError) as info:
        await api.send_message("1@g.us", "x")
    assert getattr(info.value, attr)
    await api.close()


async def test_timeout_is_network_error_without_token_in_message():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    api = make_api(handler)
    with pytest.raises(GreenAPIError) as info:
        await api.send_message("1@g.us", "x")
    assert info.value.network and info.value.maybe_sent
    assert TOKEN not in str(info.value)
    assert info.value.__cause__ is None
    await api.close()

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from types import SimpleNamespace
import datetime as dt

from app.db import Base
from app.models import models  # noqa: F401
from app.services import telegram


def _db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'telegram.db'}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_config_and_secrets_roundtrip(tmp_path):
    with _db(tmp_path) as db:
        cfg = telegram.save_config(db, telegram.TelegramConfig(
            enabled=True, authorized_chat_ids=[22, 11, 22],
            pending_fiscal_urls={"11": "https://cabinet.tax.gov.ua/cashregs/check?id=1&fn=2"},
            pending_chats=[telegram.PendingChat(
                chat_id=33, username="pending", display_name="Pending User",
                requested_at="2026-09-01T10:00:00+00:00",
            )],
        ))
        telegram._set_secret(db, telegram.TOKEN_KEY, "123456:secret-token")
        assert cfg.authorized_chat_ids == [11, 22]
        assert [row.chat_id for row in cfg.authorized_chats] == [11, 22]
        assert [row.chat_id for row in cfg.pending_chats] == [33]
        assert telegram.load_config(db).enabled is True
        assert "11" in telegram.load_config(db).pending_fiscal_urls
        assert telegram.token_hint(db) == "••••-token"
        # Only ciphertext is persisted in app_settings.
        assert "secret-token" not in str(db.get(models.AppSetting, telegram.TOKEN_KEY).value)


def test_request_approval_is_idempotent(tmp_path):
    with _db(tmp_path) as db:
        message = {
            "chat": {"id": 44},
            "from": {"username": "max", "first_name": "Max", "last_name": "Test"},
        }
        assert telegram.request_approval(db, message) is True
        assert telegram.request_approval(db, message) is False
        cfg = telegram.load_config(db)
        assert len(cfg.pending_chats) == 1
        assert cfg.pending_chats[0].display_name == "Max Test"

        cfg.authorized_chat_ids.append(44)
        telegram.save_config(db, cfg)
        assert telegram.load_config(db).pending_chats == []


def test_authorized_identity_is_refreshed(tmp_path):
    with _db(tmp_path) as db:
        telegram.save_config(db, telegram.TelegramConfig(authorized_chat_ids=[55]))
        telegram.update_authorized_identity(db, {
            "chat": {"id": 55},
            "from": {"username": "new_name", "first_name": "Ірина", "last_name": "Коваль"},
        })
        chat = telegram.load_config(db).authorized_chats[0]
        assert chat.username == "new_name"
        assert chat.display_name == "Ірина Коваль"


def test_incoming_messages_keep_sender_and_reply_branch(tmp_path):
    with _db(tmp_path) as db:
        first = telegram.record_incoming_message(db, {
            "update_id": 100,
            "message": {
                "message_id": 10, "date": 1_787_000_000,
                "chat": {"id": 55, "type": "private"},
                "from": {"id": 7, "username": "max", "first_name": "Максим"},
                "text": "перший чек",
            },
        }, authorized=True)
        reply = telegram.record_incoming_message(db, {
            "update_id": 101,
            "message": {
                "message_id": 11, "date": 1_787_000_010,
                "chat": {"id": 55, "type": "private"},
                "from": {"id": 8, "username": "nastya", "first_name": "Настя"},
                # User replies to the bot's answer (12), which Telegram reports
                # as having replied to the original user message (10).
                "reply_to_message": {
                    "message_id": 12,
                    "from": {"id": 999, "is_bot": True},
                    "reply_to_message": {"message_id": 10},
                },
                "photo": [{"file_id": "secret", "file_size": 123, "width": 800, "height": 1200}],
                "caption": "уточнення",
            },
        }, authorized=True)

        assert first.conversation_key == reply.conversation_key == "55:reply:10"
        assert reply.root_message_id == 10 and reply.reply_to_message_id == 12
        assert reply.sender_id == 8 and reply.sender_username == "nastya"
        assert reply.content_type == "photo" and reply.media_meta["file_size"] == 123
        assert "file_id" not in reply.media_meta
        assert telegram.record_incoming_message(db, {
            "update_id": 101, "message": {"message_id": 11, "chat": {"id": 55}},
        }, authorized=True).id == reply.id
        assert db.query(models.TelegramMessageLog).count() == 2


def test_forum_topic_is_its_own_conversation(tmp_path):
    with _db(tmp_path) as db:
        row = telegram.record_incoming_message(db, {
            "update_id": 200,
            "message": {
                "message_id": 21, "message_thread_id": 777,
                "chat": {"id": -10055, "type": "supergroup", "title": "Сім'я"},
                "from": {"id": 7, "first_name": "Максим"}, "text": "чек",
            },
        }, authorized=True)

        assert row.conversation_key == "-10055:topic:777"
        assert row.root_message_id == 777


def test_mcp_commands_validate_arguments():
    assert telegram._mcp_command("/find") == "Формат: /find молоко"
    assert telegram._mcp_command("/add") == "Формат: /add молоко"
    assert telegram._mcp_command("hello") is None


def test_vchasno_html_prefers_tax_cabinet_link_over_preview():
    page = '''
      <meta property="og:image" content="https://kasa.vchasno.ua/check-preview/abc.png" />
      <a href="https://cabinet.tax.gov.ua/cashregs/check?id=DARnLkn-rj0&amp;sm=1499.00&amp;fn=4001348949&amp;date=20260730&amp;time=12:52:23">
        Переглянути чек у ДПС
      </a>
    '''
    result = telegram._fiscal_url_from_html(page)
    assert result is not None
    assert "id=DARnLkn-rj0" in result
    assert "fn=4001348949" in result
    assert "&amp;" not in result


def test_receipt_message_contains_text_and_deep_link():
    receipt = SimpleNamespace(
        id="receipt-42", shop_name="ГЛУСКО", shop_address="с. Бережани",
        receipt_url="https://cabinet.tax.gov.ua/cashregs/check?id=1&fn=2",
        purchased_at=dt.datetime(2026, 7, 11, 8, 35),
        total_discount=74.59, total_amount=747.84, currency="UAH", payment_type="card",
        items=[
            SimpleNamespace(name="Американо", quantity=1, unit=None,
                            unit_price=86, total_price=77.4, discount=8.6),
            SimpleNamespace(name="ГАЗ", quantity=12.98, unit="л",
                            unit_price=38.4, total_price=491.94, discount=6.49),
        ],
    )
    text = telegram.format_receipt_message(receipt, method="ai")
    assert "🏪 ГЛУСКО" in text
    assert "🕒 11.07.2026 11:35" in text
    assert "1. Американо" in text
    assert "12.98 л × 38.40 = 491.94 грн" in text
    assert "Знижка: 74.59 грн" in text
    assert "Разом: 747.84 UAH" in text
    assert "🧾 Оригінал: https://cabinet.tax.gov.ua/" in text
    assert text.endswith("/#/receipts/receipt-42")


def _codes(keyboard):
    return [b["callback_data"] for row in keyboard for b in row if "callback_data" in b]


def test_review_keyboard_offers_all_actions_and_inbox_link():
    kb = telegram._review_keyboard("job-7")
    codes = _codes(kb)
    # An unsaved draft can still grow: a receipt longer than the frame is shot
    # in parts, and the next part attaches to this job.
    assert codes == ["rev:save:job-7", "rev:rescan:job-7", "rev:ai:job-7",
                     "ser:add:job-7", "rev:del:job-7"]
    assert all(len(c.encode()) <= 64 for c in codes)
    assert kb[-1][0]["url"].endswith("/#/inbox")


def test_saved_receipt_keeps_rescan_and_delete_but_drops_save():
    """A receipt that filed cleanly can still be a misread, so rerunning and
    deleting stay available — flagged to act on the stored receipt too — while
    "save the draft" has nothing left to act on."""
    codes = _codes(telegram._review_keyboard("job-7", saved=True))
    assert codes == ["rev:rescanx:job-7", "rev:aix:job-7", "rev:delx:job-7"]


def test_rescan_job_reports_a_vanished_job(monkeypatch):
    import asyncio

    out = asyncio.run(telegram._rescan_job("missing", ai_first=True))
    assert out == "Сканування не знайдено"


def test_photo_is_acknowledged_before_the_slow_recognition_runs(tmp_path, monkeypatch):
    """Recognition can take tens of seconds; silence reads as the bot ignoring
    the message and invites a second copy of the same photo."""
    import asyncio

    sent: list[dict] = []

    async def fake_api(token, method, payload=None):
        sent.append({"method": method, **(payload or {})})
        return {"ok": True, "result": {}}

    async def slow_scan(*args, **kwargs):
        assert [m["text"] for m in sent] == ["📥 Чек отримано, розпізнаю…"], \
            "the acknowledgement must already be out before scanning starts"
        return "✅ Чек додано"

    async def fake_download(token, file_id):
        return b"jpeg", "image/jpeg"

    monkeypatch.setattr(telegram, "_api", fake_api)
    monkeypatch.setattr(telegram, "_scan_bytes", slow_scan)
    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(telegram, "_authorized", lambda db, chat_id: True)
    monkeypatch.setattr(telegram, "update_authorized_identity", lambda db, msg: None)

    asyncio.run(telegram.handle_update("token", {"message": {
        "message_id": 7, "chat": {"id": 5}, "photo": [{"file_id": "f1"}],
    }}))

    texts = [m["text"] for m in sent]
    assert texts[0] == "📥 Чек отримано, розпізнаю…"
    assert "Чек додано" in texts[1]
    # Both land in the thread of the photo they answer.
    assert all(m["reply_parameters"]["message_id"] == 7 for m in sent)


def test_draft_message_shows_what_was_read_and_why_it_is_a_draft():
    """"Розпізнано частково" alone gives nothing to judge — the user had to open
    the inbox to decide. The lines and the gap that made it a draft go in the
    chat, next to the buttons that act on them."""
    text = telegram.format_draft_message({
        "shop_name": "ФОП СЕМЕРОГ",
        "purchased_at": "2026-09-06T07:12:23+00:00",
        "total_amount": 698.25,
        "items": [
            {"name": "Картопля", "quantity": 5.81, "unit_price": 29.0, "total_price": 168.49},
            {"name": "Морква", "quantity": 1.32, "unit_price": 28.0, "total_price": 36.96},
        ],
    })
    assert "🏪 ФОП СЕМЕРОГ" in text
    assert "🕒 06.09.2026 10:12" in text          # stored UTC, shown in Kyiv time
    assert "1. Картопля" in text
    assert "5.81 × 29.00 = 168.49" in text
    assert "Сума позицій: 205.45" in text
    assert "Разом на чеку: 698.25" in text
    # The mismatch is the whole reason this needs a human.
    assert "Розбіжність -492.80" in text


def test_draft_message_confirms_when_the_lines_do_add_up():
    text = telegram.format_draft_message({
        "total_amount": 100.0,
        "items": [{"name": "Хліб", "quantity": 1, "unit_price": 100.0, "total_price": 100.0}],
    })
    assert "✅ Позиції сходяться із сумою." in text
    assert "магазин не розпізнано" in text


def test_draft_message_survives_an_empty_or_itemless_draft():
    assert "прийняти нічого" in telegram.format_draft_message({})
    assert "Позицій не розпізнано." in telegram.format_draft_message({"total_amount": 10.0})


def test_draft_message_stays_within_telegram_limits():
    """A long receipt must not push the message past Telegram's 4096 characters
    and lose the totals that the decision depends on."""
    draft = {
        "shop_name": "СІЛЬПО",
        "total_amount": 5000.0,
        "items": [{"name": f"Товар з доволі довгою назвою {i}", "quantity": 1,
                   "unit_price": 25.0, "total_price": 25.0} for i in range(200)],
    }
    text = telegram.format_draft_message(draft)
    assert len(text) < 3200
    assert "…ще" in text
    assert "Разом на чеку: 5 000.00" in text


def test_qr_status_line_distinguishes_all_four_outcomes():
    from app.scanner.fiscal_qr import QR_DECODED, QR_NONE, QR_UNREADABLE

    assert "фіскальний чек ДПС" in telegram._qr_status_line(
        QR_DECODED, "https://cabinet.tax.gov.ua/x")
    assert "не фіскальне посилання" in telegram._qr_status_line(QR_DECODED, None)
    # The photographed-too-far case must tell the user to move closer, not claim
    # the receipt has no QR at all.
    unreadable = telegram._qr_status_line(QR_UNREADABLE, None)
    assert "замалий/розмитий" in unreadable and "зблизька" in unreadable
    assert "не знайдено" in telegram._qr_status_line(QR_NONE, None)


def test_send_threads_reply_and_attaches_keyboard(monkeypatch):
    import asyncio

    captured = {}

    async def fake_api(token, method, payload=None):
        captured["method"] = method
        captured["payload"] = payload
        return {"ok": True, "result": {}}

    monkeypatch.setattr(telegram, "_api", fake_api)
    asyncio.run(telegram._send(
        "t", 5, "hi", reply_to=99, thread_id=77,
        keyboard=[[{"text": "a", "callback_data": "b"}]],
    ))
    assert captured["payload"]["reply_parameters"] == {"message_id": 99, "allow_sending_without_reply": True}
    assert captured["payload"]["message_thread_id"] == 77
    assert captured["payload"]["reply_markup"]["inline_keyboard"][0][0]["text"] == "a"


def test_fiscal_unavailable_message_keeps_qr_facts_and_instructions():
    from app.scanner.fiscal_qr import parse_fiscal_url

    fiscal = parse_fiscal_url(
        "https://cabinet.tax.gov.ua/cashregs/check?date=20260830&time=111656"
        "&id=233747&sm=7061.43&fn=3000893964"
    )
    text = telegram._fiscal_unavailable_message(fiscal)
    assert "30.08.2026 11:16:56" in text
    assert "7 061.43 UAH" in text
    assert fiscal.raw_url in text
    assert "повний скриншот" in text


# --------------------------------------------------------------------------- #
# Free-phrase capture (CAPTURE_PLAN.md)
# --------------------------------------------------------------------------- #
def _capture_db(tmp_path, monkeypatch):
    """A session factory the bot's capture path can use, with every table the
    shopping list and the scan journal touch."""
    from app.models import scanner, shopping  # noqa: F401 — register tables

    engine = create_engine(
        f"sqlite:///{tmp_path / 'capture.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(telegram, "SessionLocal", factory)
    return factory


def _fake_parse(draft):
    async def parse(db, text):
        return draft
    return parse


def test_captured_shopping_list_is_added_and_undoable(tmp_path, monkeypatch):
    import asyncio

    from app.models.shopping import ShoppingListItem
    from app.services import capture

    factory = _capture_db(tmp_path, monkeypatch)
    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.9,
        "shopping": [
            {"text": "яйця", "qty": 3, "unit": "десяток"},
            {"text": "молоко", "qty": 1, "note": "для Мілани"},
        ],
    }, source_text="додай в список покупок яйця 3 десятка, молоко для мілани")
    monkeypatch.setattr(capture, "parse_capture", _fake_parse(draft))

    reply = asyncio.run(telegram._capture_reply(draft.source_text))
    assert "Додано до списку (2)" in reply.text
    assert "яйця — 3 десяток" in reply.text
    assert "молоко (для Мілани)" in reply.text
    batch = _codes(reply.keyboard)[0].removeprefix("cap:undo:")

    with factory() as db:
        assert db.query(ShoppingListItem).count() == 2

    assert "Прибрано зі списку: 2" in telegram._undo_capture_batch(batch)
    with factory() as db:
        assert db.query(ShoppingListItem).count() == 0
    # A batch can only be undone once.
    assert telegram._undo_capture_batch(batch) == "Цю дію вже неможливо скасувати."


def test_captured_receipt_waits_for_confirmation(tmp_path, monkeypatch):
    import asyncio

    from app.models.models import Receipt
    from app.models.scanner import ScanJob, ScanStatus
    from app.services import capture

    factory = _capture_db(tmp_path, monkeypatch)
    draft = capture.parse_payload({
        "intent": "receipt", "confidence": 0.9,
        "receipt": {"shop_name": "Перукарня Вікторія", "shop_address": "Індустріальна",
                    "items": [{"name": "Стрижка Насти", "total_price": 600},
                              {"name": "Покраска волос", "total_price": 1200}]},
    }, source_text="додай чек перукарня Вікторія...")
    monkeypatch.setattr(capture, "parse_capture", _fake_parse(draft))

    reply = asyncio.run(telegram._capture_reply(draft.source_text))
    with factory() as db:
        job = db.query(ScanJob).one()
        assert job.status is ScanStatus.needs_review
        # Read, not booked: the ledger stays empty until the user confirms.
        assert db.query(Receipt).count() == 0

    assert "Перукарня Вікторія" in reply.text
    assert "1800" in reply.text.replace(" ", "").replace(" ", "")
    codes = _codes(reply.keyboard)
    # No image to run again — offering a rescan could only fail.
    assert codes == [f"rev:save:{job.id}", f"ser:add:{job.id}", f"rev:del:{job.id}"]


def test_capture_can_be_switched_off(tmp_path, monkeypatch):
    import asyncio

    factory = _capture_db(tmp_path, monkeypatch)
    with factory() as db:
        telegram.save_config(db, telegram.TelegramConfig(capture_enabled=False))

    from app.services import capture

    def explode(*args, **kwargs):  # pragma: no cover — must not run
        raise AssertionError("capture is off; no AI call may happen")

    monkeypatch.setattr(capture, "parse_capture", explode)
    assert asyncio.run(telegram._capture_reply("додай чек десь щось")) is None


def test_voice_message_is_transcribed_then_read(tmp_path, monkeypatch):
    """The transcript is quoted back: speech recognition mishears amounts, and
    that has to be visible before a receipt built from it is confirmed."""
    import asyncio

    from app.services import capture, stt, voice as voice_svc

    _capture_db(tmp_path, monkeypatch)
    monkeypatch.setattr(voice_svc, "VOICE_DIR", tmp_path / "voice")
    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.9,
        "shopping": [{"text": "молоко", "qty": 1}],
    }, source_text="додай в список покупок молоко")
    monkeypatch.setattr(capture, "parse_capture", _fake_parse(draft))

    async def fake_download(token, file_id):
        assert file_id == "voice-1"
        return b"opus", "audio/ogg"

    async def fake_transcribe(db, audio, **kwargs):
        assert audio == b"opus" and kwargs["duration_s"] == 4
        return "додай в список покупок молоко"

    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)

    reply = asyncio.run(telegram._voice_reply(
        "token", {"file_id": "voice-1", "duration": 4, "mime_type": "audio/ogg"}))
    assert reply.text.startswith("🗣 «додай в список покупок молоко»")
    assert "Додано до списку (1)" in reply.text


def test_voice_is_kept_and_retryable_when_recognition_is_off(tmp_path, monkeypatch):
    """Recognition may be off or misconfigured, and a voice note is said once —
    so the audio is stored anyway and the answer offers to try again."""
    import asyncio

    from app.models.scanner import VoiceMessage, VoiceStatus
    from app.services import stt, voice as voice_svc

    factory = _capture_db(tmp_path, monkeypatch)
    monkeypatch.setattr(voice_svc, "VOICE_DIR", tmp_path / "voice")

    async def fake_download(token, file_id):
        return b"opus", "audio/ogg"

    async def fake_transcribe(db, audio, **kwargs):
        raise stt.SttError("Розпізнавання голосу вимкнено — увімкніть його в Налаштуваннях → AI.")

    monkeypatch.setattr(telegram, "_download_telegram_file", fake_download)
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)

    reply = asyncio.run(telegram._voice_reply("token", {"file_id": "v", "duration": 2}))
    assert "Голосове збережено (2 с)" in reply.text
    assert "Розпізнавання голосу вимкнено" in reply.text

    with factory() as db:
        row = db.query(VoiceMessage).one()
        assert row.status is VoiceStatus.failed
        assert voice_svc.path(row).read_bytes() == b"opus"

    codes = _codes(reply.keyboard)
    # Nothing to re-read yet, so only re-recognition is offered.
    assert codes == [f"voi:stt:{row.id}"]


def test_retrying_a_stored_voice_reads_it_again(tmp_path, monkeypatch):
    import asyncio

    from app.services import capture, stt, voice as voice_svc

    factory = _capture_db(tmp_path, monkeypatch)
    monkeypatch.setattr(voice_svc, "VOICE_DIR", tmp_path / "voice")
    with factory() as db:
        row = voice_svc.store(db, b"opus", content_type="audio/ogg", duration_s=3)
        voice_id = row.id

    async def fake_transcribe(db, audio, **kwargs):
        return "додай в список покупок молоко"

    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.9,
        "shopping": [{"text": "молоко", "qty": 1}],
    }, source_text="додай в список покупок молоко")
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(capture, "parse_capture", _fake_parse(draft))

    reply = asyncio.run(telegram._retry_voice(voice_id, recognise=True))
    assert reply.text.startswith("🗣 «додай в список покупок молоко»")
    assert "Додано до списку (1)" in reply.text
    with factory() as db:
        stored = voice_svc.get(db, voice_id)
        assert stored.transcript == "додай в список покупок молоко"
        # The recording keeps a note of what it turned into.
        assert "Додано до списку" in stored.applied


def test_a_voice_can_be_recognised_again_in_the_other_language(tmp_path, monkeypatch):
    """Ukrainian first, Russian on request: the button carries the language code
    and the same stored recording goes through again."""
    import asyncio

    from app.services import ai, capture, stt, voice as voice_svc

    factory = _capture_db(tmp_path, monkeypatch)
    monkeypatch.setattr(voice_svc, "VOICE_DIR", tmp_path / "voice")
    with factory() as db:
        cfg = ai.load_config(db)
        cfg.speech.enabled = True
        cfg.speech.provider = "openai"
        cfg.speech.model = "gpt-4o-transcribe"
        cfg.speech.language = "uk-UA"
        cfg.speech.alternative_languages = ["ru-RU"]
        ai.save_config(db, cfg)
        ai.set_cloud_key(db, "openai", "sk-test")
        voice_id = voice_svc.store(db, b"opus", content_type="audio/ogg", duration_s=3).id

    asked: list[str | None] = []

    async def fake_transcribe(db, audio, **kwargs):
        asked.append(kwargs.get("language"))
        return "добавь в список молоко"

    draft = capture.parse_payload({
        "intent": "shopping", "confidence": 0.9,
        "shopping": [{"text": "молоко", "qty": 1}],
    }, source_text="добавь в список молоко")
    monkeypatch.setattr(stt, "transcribe", fake_transcribe)
    monkeypatch.setattr(capture, "parse_capture", _fake_parse(draft))

    reply = asyncio.run(telegram._retry_voice(voice_id, recognise=True, language="ru-RU"))
    assert asked == ["ru-RU"]
    assert "російська" in reply.text
    # Heard as Russian now, so only the languages left untried stay on offer.
    codes = _codes(reply.keyboard)
    assert f"voi:lang:uk-UA:{voice_id}" in codes
    assert not any(c.startswith("voi:lang:ru-RU:") for c in codes)


def test_retrying_a_deleted_voice_says_so(tmp_path, monkeypatch):
    import asyncio

    _capture_db(tmp_path, monkeypatch)
    reply = asyncio.run(telegram._retry_voice("gone", recognise=False))
    assert "вже видалено" in reply.text


# --------------------------------------------------------------------------- #
# A receipt photographed in several overlapping frames
# --------------------------------------------------------------------------- #
def test_an_album_the_qr_cannot_settle_asks_which_it_is(tmp_path, monkeypatch):
    """Guessing costs a model call per frame and receipts to clean up, so the
    bot asks — with the two buttons that answer it."""
    import asyncio

    from app.services import series_scan

    _capture_db(tmp_path, monkeypatch)

    async def fake_batch(db_, parts, *, job=None, choice=None):
        return series_scan.SeriesOutcome(
            kind=series_scan.ASK, results=[], scan_job_id="job-batch",
            note="на кадрах не знайдено QR — це один довгий чек чи різні чеки?")

    monkeypatch.setattr(series_scan, "scan_batch", fake_batch)

    reply = asyncio.run(telegram._scan_batch_parts(
        [(b"a", "image/jpeg", "1.jpg"), (b"b", "image/jpeg", "2.jpg")], chat_id=5))
    assert "Отримано 2 фото" in reply.text
    assert _codes(reply.keyboard) == ["ser:one:job-batch", "ser:many:job-batch"]


def test_choosing_separate_receipts_reports_each_one(tmp_path, monkeypatch):
    import asyncio

    from app.api.inbox import UploadResult
    from app.models.scanner import ScanJob, ScanStatus
    from app.services import series_scan

    factory = _capture_db(tmp_path, monkeypatch)
    with factory() as db:
        db.add(ScanJob(id="job-batch", status=ScanStatus.needs_review, method="series",
                       raw_ocr={"series": True, "pending_choice": True, "parts": 2}))
        db.commit()

    async def fake_resolve(db_, job, choice):
        assert choice == "separate"
        return series_scan.SeriesOutcome(
            kind=series_scan.SEPARATE, note="оброблено як 2 окремих чеків",
            results=[
                UploadResult(kind="receipt", scan_job_id="j1", receipt_id="r1", method="qr"),
                UploadResult(kind="receipt", scan_job_id="j2", method="ai", needs_review=True),
            ])

    monkeypatch.setattr(series_scan, "resolve", fake_resolve)

    reply = asyncio.run(telegram._resolve_series_choice("job-batch", "separate"))
    assert "2 окремих чеків" in reply.text
    assert "1. ✅ чек збережено" in reply.text
    assert "2. 🟡 розпізнано частково" in reply.text


def test_add_photo_button_arms_the_next_photo_for_that_job(tmp_path, monkeypatch):
    factory = _capture_db(tmp_path, monkeypatch)
    telegram._arm_pending_series(77, "job-9")
    with factory() as db:
        assert telegram.load_config(db).pending_series == {"77": "job-9"}

    # One photo joins one series: the armed job is consumed, not reused.
    assert telegram._take_pending_series(77) == "job-9"
    assert telegram._take_pending_series(77) is None


def test_a_series_whose_receipt_is_already_saved_says_so(tmp_path, monkeypatch):
    import asyncio

    _capture_db(tmp_path, monkeypatch)
    reply = asyncio.run(telegram._scan_batch_parts(
        [(b"a", "image/jpeg", "1.jpg")], chat_id=5, job_id="gone"))
    assert "уже збережено або видалено" in reply.text


def test_a_photo_nothing_came_out_of_still_offers_actions(tmp_path, monkeypatch):
    """A dead-end message with no buttons left the user with nowhere to go —
    rerunning through vision, adding a frame or dropping the attempt are exactly
    what is needed there."""
    import asyncio

    from app.api.inbox import UploadResult

    _capture_db(tmp_path, monkeypatch)

    async def fake_handle(db, data, filename, media_type, **kwargs):
        return UploadResult(kind="receipt", scan_job_id="job-x", method="none",
                            confidence="none", needs_review=False)

    monkeypatch.setattr("app.api.inbox._handle_receipt", fake_handle)

    reply = asyncio.run(telegram._scan_bytes(b"jpeg", "image/jpeg", "photo.jpg", chat_id=5))
    assert "Не вдалося надійно розпізнати чек" in reply.text
    codes = _codes(reply.keyboard)
    # Nothing to accept, everything else on offer.
    assert "rev:save:job-x" not in codes
    assert codes == ["rev:rescan:job-x", "rev:ai:job-x", "ser:add:job-x", "rev:del:job-x"]

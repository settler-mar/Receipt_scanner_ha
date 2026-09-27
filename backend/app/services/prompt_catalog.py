"""One catalogue for all editable AI instructions and their revisions."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.prompts import AiPrompt, AiPromptRevision


@dataclass(frozen=True)
class PromptDefault:
    key: str
    title: str
    description: str
    task: str
    content: str


DEFAULTS = (
    PromptDefault(
        "json_response_rules", "Формат ответа JSON",
        "Для всех AI-вызовов: задаёт формат, который приложение может надёжно разобрать.", "all",
        "Отвечай только одним валидным JSON-значением без markdown, пояснений и code fences. Соблюдай поля и типы из задания. Не выдумывай значения: используй null, пустой массив или пустую строку, если это разрешено схемой. Числа возвращай числами. Верни результат для каждой входной позиции.",
    ),
    PromptDefault(
        "language_variants", "Языковые варианты товаров", "Для распознавания одного товара при русском, украинском, английском, смешанном написании или другой раскладке клавиатуры.", "all",
        "Считай украинские, русские и английские варианты, транслитерацию, смешанную кириллицу/латиницу, неправильную раскладку, опечатки, сокращения и формы слова эквивалентными, если это один товар. Сравнивай смысл, а не точное написание: «алюминиевая» и «алюмінієва» могут совпадать, а похожее общее слово — нет. Бренд, модель, артикул, штрихкод, количество, объём, вес и размеры важнее языка и порядка слов. Учитывай варианты модели с дефисом и пробелом, например WD-40. Не объединяй товары с разным материалом, размером, моделью, категорией или важной характеристикой. Исходное название сохраняй.",
    ),
    PromptDefault(
        "classify_rules", "Классификация товаров", "Для назначения позиции существующей категории и корректной статистики расходов.", "classify",
        "Назначай каждому товару лучший путь только из разрешённого списка. Учитывай смысл названия независимо от языка, магазин — только как контекст. Различай материал, назначение, размер и модель. Не создавай категорию и не переписывай исходное название. Если подходящей категории нет, верни пустую строку.",
    ),
    PromptDefault(
        "normalize_rules", "Нормализация и поиск", "Для подготовки короткого поискового запроса к каталогу выбранного магазина.", "normalize",
        "Верни короткий смысловой запрос на украинском языке (1–3 слова), сохранив бренд, модель, размер, количество и важную характеристику. Учитывай русские, украинские и английские варианты, но не переводи механически бренд или модель. Не удаляй отличительные цифры, дефисы и единицы измерения. Не выдумывай характеристики.",
    ),
    PromptDefault(
        "ocr_rules", "OCR чеков", "Для чтения чеков с фотографий и передачи позиций в базу и сопоставление со списком покупок.", "ocr",
        "Точно переписывай видимые названия, включая латиницу, кириллицу, бренд, модель, артикул, дефисы, цифры и единицы измерения. Не переводи и не исправляй название догадкой. Несколько кадров считай частями одного чека, объединяй их и не дублируй строки. Не смешивай продавца с названием позиции. Не выдумывай дату, сумму, количество или скрытые строки: используй null, если значение не видно.",
    ),
    PromptDefault(
        "receipt_amount_rules", "Суммы, скидки и доставка", "Не даёт модели искажать финансовую структуру чека при распознавании товаров и онлайн-заказов.", "ocr",
        "Сохраняй финансовую структуру как на источнике. Финальный total_amount — это напечатанный или явно показанный итог к оплате, а не автоматически пересчитанная сумма строк. Не подгоняй цены товаров под итог и не распределяй скидку, купон, налог, комиссию, доставку или другую доплату по товарам без явного указания источника. Скидку записывай отдельным положительным уменьшением, а доплату — отдельной положительной строкой или корректировкой. Доставку выделяй отдельной позицией с названием «Доставка» или исходным названием, не смешивай её с товаром. Для онлайн-заказов различай скидку на конкретную позицию и скидку на весь заказ; глобальную скидку не приписывай случайному товару. Если сумма строк не равна итогу из-за видимой скидки, доставки, налога, комиссии или округления, сохрани оба значения и причину; если причина не видна, не выдумывай её. В сырых ответах магазина приоритет имеют исходные поля сервера, а нормализованные поля не должны скрывать исходные суммы.",
    ),
    PromptDefault(
        "intent_rules", "Разбор сообщений", "Для преобразования свободного текста или голосовой команды в чек, заправку или список покупок.", "intent",
        "Сохраняй название товара и язык пользователя, учитывая русские, украинские, английские и смешанные варианты как один смысл. Различай receipt, fuel, shopping и unknown. Для списка выделяй позиции, количество, единицу и заметку; для чека — магазин и строки; для топлива — станцию, вид топлива, сумму, литры и цену. Не выдумывай данные и не превращай назначение вещи в другой товар.",
    ),
    PromptDefault(
        "shopping_match_rules", "Сопоставление списка с чеком", "Для определения, какие позиции списка куплены, чтобы приложение сняло их со списка и связало со строками чека.", "normalize",
        "Сопоставляй позиции по смыслу, а не по точному написанию. Учитывай русские, украинские, английские и смешанные варианты, бренд, модель, артикул, штрихкод, количество, единицы, материал и размеры; дефис или пробел в модели не должны мешать. Важные признаки должны совпадать. Не объединяй товары только из-за общего слова или похожего перевода. Каждую строку списка и чека используй один раз. При сомнении или конфликте признаков не создавай совпадение. Возвращай только существующие входные id.",
    ),
)

_BY_KEY = {item.key: item for item in DEFAULTS}

# Safe, one-time upgrades for the versions shipped in the first catalogue. A
# user-edited prompt never equals one of these exact snapshots and is left
# untouched.
_LEGACY_CONTENT = {
    "json_response_rules": "Отвечай только одним валидным JSON-значением без markdown, пояснений и code fences.",
    "language_variants": "Считай украинские, русские и английские варианты, транслитерацию, смешанную кириллицу/латиницу, раскладку клавиатуры, опечатки, формы слова и падежи эквивалентными, если они обозначают один товар. Перед сравнением нормализуй смысл, а не требуй полного совпадения написания. Бренд, модель, штрихкод, артикул, объём, вес и размеры важнее языка и порядка слов. Не создавай совпадение только из-за похожего общего слова: проверяй измерения, модель, материал и категорию. Исходное название товара сохраняй в результатах, если задача не просит нормализованное название.",
    "classify_rules": "Назначай каждому товару наиболее подходящий путь категории из разрешённого списка. Используй магазин и контекст только для снятия неоднозначности. Если подходящей категории нет, верни пустую строку.",
    "normalize_rules": "Нормализуй название по смыслу и верни короткий поисковый запрос на украинском языке (1–3 слова), не теряя бренд, модель, размер или важную характеристику. Не переводи название механически и не удаляй отличительные цифры.",
    "ocr_rules": "Точно переписывай видимые названия позиций, включая латиницу, кириллицу, бренд, модель, артикул и цифры. Не переводи и не исправляй название догадкой. Если символ неразборчив, сохрани наиболее вероятный вариант только при наличии визуального основания; иначе используй null или пометь неопределённость.",
    "intent_rules": "Сохраняй пользовательское название товара и его язык, но учитывай русские, украинские, английские и смешанные варианты как один смысл. Не превращай назначение вещи в другой товар. Разделяй отдельные позиции и не выдумывай отсутствующие характеристики.",
    "shopping_match_rules": "Сопоставляй позиции по смыслу, а не по точному написанию. Русское, украинское, английское, смешанное или транслитерированное название может быть одним товаром. Учитывай бренд, модель, артикул, штрихкод, количество, единицы, материал и размеры; размеры и модель должны согласовываться. Не объединяй разные категории и похожие товары, если важный признак отличается. При сомнении не создавай совпадение.",
}


def ensure_defaults(db: Session) -> None:
    """Insert new prompts and upgrade untouched built-ins without overwriting edits."""
    changed = False
    for default in DEFAULTS:
        row = db.get(AiPrompt, default.key)
        if row is None:
            db.add(AiPrompt(key=default.key, title=default.title, description=default.description,
                            task=default.task, content=default.content, version=1))
            db.add(AiPromptRevision(prompt_key=default.key, version=1, content=default.content,
                                    note="Начальная версия", changed_by="system"))
            changed = True
            continue
        # Titles/descriptions are catalogue metadata, not the editable prompt
        # body, so keep them current for existing installations.
        if (row.title, row.description, row.task) != (default.title, default.description, default.task):
            row.title, row.description, row.task = default.title, default.description, default.task
            changed = True
        if row.content == _LEGACY_CONTENT.get(default.key):
            row.version += 1
            row.content = default.content
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            db.add(AiPromptRevision(prompt_key=default.key, version=row.version,
                                    content=default.content,
                                    note="Обновление системных правил", changed_by="system"))
            changed = True
            continue
        # The previous catalogue release also made a system-only revision. If
        # it is still untouched, replace that verbose body with the concise
        # current rules; a later user revision is never overwritten.
        latest = db.execute(
            select(AiPromptRevision).where(AiPromptRevision.prompt_key == default.key)
            .order_by(AiPromptRevision.version.desc()).limit(1)
        ).scalar_one_or_none()
        if (latest and latest.version == row.version
                and latest.changed_by == "system"
                and latest.note == "Обновление системных правил"
                and row.content == latest.content
                and row.content != default.content):
            row.version += 1
            row.content = default.content
            row.updated_at = dt.datetime.now(dt.timezone.utc)
            db.add(AiPromptRevision(prompt_key=default.key, version=row.version,
                                    content=default.content,
                                    note="Уточнение правил и описания", changed_by="system"))
            changed = True
    if changed:
        db.commit()


def get(db: Session | None, key: str) -> str:
    default = _BY_KEY.get(key)
    if default is None:
        return ""
    if db is None:
        return default.content
    ensure_defaults(db)
    row = db.get(AiPrompt, key)
    return row.content if row else default.content


def list_prompts(db: Session) -> list[AiPrompt]:
    ensure_defaults(db)
    return list(db.execute(select(AiPrompt).order_by(AiPrompt.task, AiPrompt.key)).scalars())


def history(db: Session, key: str) -> list[AiPromptRevision]:
    ensure_defaults(db)
    if key not in _BY_KEY:
        return []
    return list(db.execute(select(AiPromptRevision).where(AiPromptRevision.prompt_key == key)
                .order_by(AiPromptRevision.version.desc())).scalars())


def update(db: Session, key: str, content: str, note: str | None = None,
           changed_by: str | None = None) -> AiPrompt:
    ensure_defaults(db)
    row = db.get(AiPrompt, key)
    if row is None:
        raise KeyError(key)
    content = content.strip()
    if not content:
        raise ValueError("Текст промта не может быть пустым")
    if content != row.content:
        row.version += 1
        row.content = content
        row.updated_at = dt.datetime.now(dt.timezone.utc)
        db.add(AiPromptRevision(prompt_key=key, version=row.version, content=content,
                                note=(note or "").strip() or None,
                                changed_by=(changed_by or "").strip() or None))
        db.commit()
    return row

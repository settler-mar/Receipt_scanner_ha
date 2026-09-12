from sqlalchemy import select

from app.db import SessionLocal
from app.models.models import Account, AccountStatus, AuthType, Category, Store
from app.services.text import slugify

# code, display name, auth type, network_type (see Store.network_type)
STORES = [
    ("atb", "АТБ-Маркет", AuthType.phone_otp, "supermarket"),
    ("fozzy", "Fozzy", AuthType.phone_otp, "supermarket"),
    ("fora", "Фора", AuthType.phone_otp, "supermarket"),
    ("metro", "METRO", AuthType.device_uuid, "supermarket"),
    ("silpo", "Сільпо", AuthType.oauth_browser, "supermarket"),
    ("epicentrk", "Епіцентр К", AuthType.phone_otp, "home"),
    ("scanner", "Сканер (OCR)", AuthType.manual, None),
    ("wog", "WOG", AuthType.phone_otp, "azs"),
    ("ukrnafta", "Укрнафта", AuthType.phone_otp, "azs"),
    ("bars", "АЗК Барс", AuthType.card_number, "azs"),
    ("brsm", "БРСМ-Нафта", AuthType.phone_call, "azs"),
    ("dnipro-m", "Дніпро-М", AuthType.phone_otp, "tech"),
    ("upg", "UPG", AuthType.phone_otp, "azs"),
    ("foxtrot", "Фокстрот", AuthType.phone_otp, "tech"),
    ("klo", "KLO", AuthType.phone_otp, "azs"),
    ("vencon", "ВЕНКОН", AuthType.phone_password, "tech"),
    ("prom", "Prom.ua", AuthType.phone_password, "other"),
    ("aliexpress", "AliExpress", AuthType.phone_password, "other"),
    ("brain", "Brain", AuthType.phone_password, "tech"),
    ("eva", "ЄВА", AuthType.phone_otp, "beauty"),
]

# Starter taxonomy — "Класифікація покупок та витрат". One-off seed; the user
# owns the tree after (editable in UI). A node is either "Name" (leaf) or
# ("Name", [children]); children nest to any depth. Reviewed & rebuilt 2026-08-30
# (see migration e7a2c4b91f30, which wipes the previous tree + all classifications
# on existing databases so this one re-seeds clean).
CATEGORY_TREE: list = [
    ("Продукти", [
        "Овочі та зелень",
        "Фрукти та ягоди",
        "М'ясо та птиця",
        "Риба та морепродукти",
        "Ковбаси та м'ясні вироби",
        ("Молочні продукти", [
            "Молоко та вершки",
            "Кисломолочні продукти",
            "Сир",
            "Масло та спреди",
        ]),
        "Яйця",
        "Хліб та випічка",
        ("Бакалія", [
            "Крупи та бобові",
            "Макарони",
            "Борошно та суміші",
            "Цукор та підсолоджувачі",
            "Спеції та приправи",
            "Олія та оцет",
            "Соуси",
            "Консерви",
        ]),
        "Заморожені продукти",
        "Напівфабрикати",
        "Готова їжа",
    ]),
    ("Напої", [
        "Вода",
        "Соки та морси",
        "Газовані напої",
        "Кава та чай",
        "Енергетики та функціональні напої",
        ("Алкоголь", [
            "Пиво",
            "Вино",
            "Міцний алкоголь",
        ]),
    ]),
    ("Солодощі та снеки", [
        "Шоколад та цукерки",
        "Печиво та вафлі",
        "Торти та тістечка",
        "Морозиво",
        "Чіпси та солоні снеки",
        "Горіхи та сухофрукти",
        "Жуйки та льодяники",
    ]),
    ("Побутова хімія та господарські товари", [
        ("Прання", [
            "Засоби для прання",
            "Догляд за білизною та пральною машиною",
        ]),
        ("Миття посуду", [
            "Ручне миття",
            ("Посудомийна машина", [
                "Засоби для миття",
                "Догляд за посудомийною машиною",
            ]),
        ]),
        ("Прибирання", [
            "Універсальні засоби",
            "Кухня та жир",
            "Сантехніка",
            "Скло та дзеркала",
            "Підлога",
            "Труби та каналізація",
            "Догляд за поверхнями",
        ]),
        "Освіжувачі повітря",
        "Захист від шкідників",
        "Губки, серветки та рукавички",
        "Прибиральний інвентар",
        "Пакети для сміття",
        "Харчова плівка, фольга та пакети",
    ]),
    ("Гігієна та косметика", [
        "Волосся",
        "Тіло",
        "Обличчя",
        "Зубна гігієна",
        "Гоління та депіляція",
        "Жіноча гігієна",
        "Декоративна косметика",
        "Парфумерія",
        "Інші засоби особистої гігієни",
    ]),
    ("Дім та кухня", [
        ("Посуд", [
            "Для приготування",
            "Столовий посуд",
            "Столові прибори",
            "Контейнери для їжі",
        ]),
        "Кухонне приладдя",
        "Зберігання та організація",
        ("Текстиль", [
            "Постільна білизна",
            "Рушники",
            "Штори та скатертини",
            "Килими, пледи та подушки",
        ]),
        ("Освітлення", [
            "Лампи",
            "Світильники",
        ]),
        ("Побутові електротовари", [
            "Батарейки та акумулятори",
            "Подовжувачі та мережеві фільтри",
        ]),
        "Меблі",
        "Декор та інтер'єр",
        "Побутова фурнітура",
    ]),
    ("Сад та ділянка", [
        ("Рослини", [
            "Овочеві культури",
            "Зелень та пряні трави",
            "Ягідні культури",
            "Плодові дерева",
            "Плодові кущі",
            "Декоративні дерева та кущі",
            "Квіти та декоративні рослини",
            "Газон",
            "Кімнатні рослини",
        ]),
        "Ґрунти та субстрати",
        ("Добрива та поліпшувачі ґрунту", [
            "Добрива",
            "Компост та перегній",
            "Мульча та кора",
            "Розкислювачі",
        ]),
        ("Захист рослин", [
            "Від бур'янів",
            "Від хвороб",
            "Від шкідників",
        ]),
        ("Садовий інструмент", [
            "Ручний",
            "Ріжучий",
            "Механізований",
            "Оснастка та розхідники",
        ]),
        ("Полив", [
            "Шланги та з'єднання",
            "Дощування",
            "Крапельний полив",
            "Ємності та комплектуючі",
        ]),
        "Вазони, кашпо та опори",
        "Облаштування ділянки",
        ("Мангал та барбекю", [
            "Обладнання та аксесуари",
            "Вугілля та дрова",
            "Засоби для розпалювання",
        ]),
    ]),
    ("Будівництво та ремонт", [
        ("Інструмент", [
            "Ручний",
            "Електроінструмент",
            "Бензо- та пневмоінструмент",
            "Вимірювальний",
            "Оснастка та розхідники",
            "Зберігання інструменту",
        ]),
        "Кріплення та метизи",
        "Лакофарбові матеріали",
        "Електрика",
        "Сантехніка",
        "Будівельні суміші",
        "Оздоблювальні матеріали",
        "Дерево та листові матеріали",
        "Ізоляція та герметизація",
        "Покрівля та водостоки",
        "Паркан, ворота та огорожі",
        "Засоби захисту",
    ]),
    ("Інженерні системи будинку", [
        "Опалення",
        "Водопостачання",
        "Каналізація",
        "Вентиляція та кондиціонування",
        "Водоочищення",
        "Резервне живлення",
        "Сонячна енергетика",
    ]),
    ("Авто", [
        ("Паливо", [
            "Бензин",
            "Дизель",
            "Газ",
        ]),
        "Оливи та технічні рідини",
        "Запчастини та розхідники",
        "Шини та диски",
        "Автоелектрика та акумулятори",
        "Мийка та догляд",
        "Автоаксесуари",
        "Інструмент та гараж",
    ]),
    ("Техніка та електроніка", [
        "Побутова техніка",
        "Кухонна техніка",
        "Комп'ютери та ноутбуки",
        "Комплектуючі та периферія",
        "Смартфони та гаджети",
        "ТВ, аудіо та мультимедіа",
        "Кабелі, зарядки та живлення",
        "Друк та витратні матеріали",
        "Електронні компоненти та DIY",
    ]),
    ("Розумний дім та безпека", [
        ("Автоматизація", [
            "Датчики",
            "Реле та контролери",
            "Розумні пристрої",
        ]),
        "Відеоспостереження",
        "Сигналізація",
        "Контроль доступу",
        "Домофони та зв'язок",
        "Пожежна та аварійна безпека",
        "Мережеве обладнання",
    ]),
    ("Здоров'я та аптека", [
        "Ліки",
        "Вітаміни та добавки",
        "Перша допомога",
        "Медичні прилади",
        "Оптика",
        "Медичні та гігієнічні вироби",
        ("Медичні послуги", [
            "Лікарі та консультації",
            "Аналізи та діагностика",
            "Стоматологія",
            "Лікування та процедури",
        ]),
    ]),
    ("Одяг та взуття", [
        "Чоловічий одяг",
        "Жіночий одяг",
        "Дитячий одяг",
        "Взуття",
        "Білизна та шкарпетки",
        "Аксесуари",
        "Робочий одяг та взуття",
    ]),
    ("Діти", [
        "Підгузки та гігієна",
        "Дитяче харчування",
        "Іграшки",
        "Дитячі товари",
        "Дитячий транспорт та аксесуари",
    ]),
    ("Освіта", [
        "Шкільні товари",
        "Навчальні матеріали",
        "Курси та гуртки",
        "Репетитори",
        "Професійне навчання",
        "Освітні послуги",
    ]),
    ("Дозвілля", [
        ("Кафе та ресторани", [
            "Ресторани та кафе",
            "Фастфуд",
            "Доставка готової їжі",
        ]),
        ("Розваги", [
            "Кіно, театр та концерти",
            "Парки та розважальні центри",
            "Інші розваги",
        ]),
        "Хобі та творчість",
        "Ігри",
        "Спорт та активний відпочинок",
        "Книги та преса",
        "Квіти та подарунки",
        "Свята та заходи",
    ]),
    ("Подорожі", [
        "Проживання",
        "Квитки",
        "Екскурсії та туристичні активності",
        "Страхування",
        "Візи та туристичні збори",
        "Туристичні послуги",
    ]),
    ("Послуги та платежі", [
        ("Комунальні послуги", [
            "Електроенергія",
            "Газ",
            "Вода та каналізація",
            "Інші комунальні",
        ]),
        ("Зв'язок та інтернет", [
            "Мобільний зв'язок",
            "Інтернет",
        ]),
        "Підписки та онлайн-сервіси",
        ("Транспорт", [
            "Громадський транспорт",
            "Таксі",
            "Паркування",
            "Інші транспортні послуги",
        ]),
        "Доставка та пошта",
        "Побутові та сервісні послуги",
        "Інші послуги",
    ]),
    ("Інше", []),
]


def seed_stores() -> None:
    db = SessionLocal()
    try:
        existing = {s.code: s for s in db.execute(select(Store)).scalars().all()}
        for code, name, auth_type, network_type in STORES:
            if code in existing:
                store = existing[code]
                store.name = name
                store.auth_type = auth_type
                store.network_type = network_type
                continue
            db.add(Store(code=code, name=name, auth_type=auth_type, network_type=network_type))
        db.flush()
        # AliExpress used to be seeded as a manual source, which marked newly
        # created accounts active even though they had no browser credential.
        # Repair only those credential-less legacy accounts.
        ali = db.execute(select(Store).where(Store.code == "aliexpress")).scalar_one_or_none()
        if ali is not None:
            for account in db.execute(select(Account).where(Account.store_id == ali.id)).scalars():
                if account.credential is None and account.status == AccountStatus.active:
                    account.status = AccountStatus.pending_otp
        db.commit()
    finally:
        db.close()


def _seed_nodes(db, nodes: list, parent_id: str | None, prefix: str) -> None:
    used: set[str] = set()
    for order, node in enumerate(nodes):
        name, children = (node, []) if isinstance(node, str) else node
        slug = slugify(name)
        # keep sibling slugs unique so `path` stays unique
        base, n = slug, 1
        while slug in used:
            n += 1
            slug = f"{base}-{n}"
        used.add(slug)
        path = f"{prefix}/{slug}" if prefix else slug
        cat = Category(parent_id=parent_id, name=name, slug=slug, path=path, sort_order=order)
        db.add(cat)
        db.flush()
        if children:
            _seed_nodes(db, children, cat.id, path)


def seed_categories() -> None:
    """Insert the starter tree once. Idempotent: does nothing when any category
    already exists (the user owns the tree from then on). Existing databases are
    reset to the current tree by migration e7a2c4b91f30, which empties the table
    so this repopulates it on the next startup."""
    db = SessionLocal()
    try:
        if db.execute(select(Category.id).limit(1)).first() is not None:
            return
        _seed_nodes(db, CATEGORY_TREE, None, "")
        db.commit()
    finally:
        db.close()

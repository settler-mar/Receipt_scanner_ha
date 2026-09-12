from app.services.text import normalize_name, slugify


def test_normalize_strips_quantity_and_percent():
    assert normalize_name("Молоко Яготинське 2,6% 900 г") == "молоко яготинське"
    assert normalize_name("СИР  КИСЛОМОЛОЧНИЙ 0.9 КГ") == "сир кисломолочний"
    assert normalize_name("Вода негазована 1,5л") == "вода негазована"
    # tail token only stripped when it's actually at the end
    assert normalize_name("Пиво 0,5 л ж/б") == "пиво 0,5 л ж/б"


def test_normalize_repeated_tail_and_lead_code():
    assert normalize_name("4820000 Хліб бородінський 400 г") == "хліб бородінський"
    assert normalize_name("Яйця  С1  10 шт") == "яйця с1"


def test_normalize_blank():
    assert normalize_name(None) == ""
    assert normalize_name("   ") == ""


def test_slugify_transliterates():
    assert slugify("Молочне та яйця") == "molochne-ta-iaitsia"
    assert slugify("Авто / паливо") == "avto-palyvo"
    assert slugify("!!!") == "cat"

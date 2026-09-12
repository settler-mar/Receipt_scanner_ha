# Установочные файлы

Подробности по каждому эндпоинту — в [../RESEARCH.md](../RESEARCH.md).

## Структура
Для каждой сети: `original_base.apk` (как было вытащено с телефона) и `patched_*_signed.apk`
(пропатченные под доверие пользовательскому CA-сертификату, подписаны debug-ключом
`signing_debug.keystore`, пароль `android`, alias `androiddebugkey`).

- `atb/` — АТБ, 5 файлов (base + 4 split)
- `fozzy/` — Fozzy, 1 файл (base, splits не было)
- `fora/` — Fora, 4 файла (base + 3 split)
- `metro/` — METRO UA, 3 файла (base + 2 split) — работает, но требует регистрации Shopper
  через скан тележки в магазине
- `epicentrk/` — Епіцентр К, 4 файла (base + 3 split)
- `eva/` — ЄВА (`com.rush.eva`), v5.6.2 targetSdk 36. Пропатчено **не apktool, а APKEditor**:
  `orig_*` (base + 3 split) → merge в один APK → бінарний патч (додано
  `res/xml/network_security_config.xml` з user-CA, зареєстровано в public.xml,
  проставлено на `<application>`) → build (без aapt2-перекодування) → debug-підпис →
  `patched_merged_signed.apk` (один файл, ставиться через `adb install`). Причина:
  apktool-перезбірка targetSdk-36 застосунку **встановлюється й одразу зникає**
  (MIUI/Play Protect зносить її як змінену), а APKEditor-збірка лишається. Пінінгу
  немає — mitmproxy розшифровує API `app.eva.ua` (сам застосунок містить Cronet/QUIC,
  але тільки для телеметрії; чеки/бонуси йдуть через OkHttp). Адаптер: `adapters/eva.py`.
- `upg/` — АЗС UPG (`ua.upg`), 4 файли (base + 3 split). **Патч НЕ вирішив проблему** —
  застосунок написаний на Flutter (`libflutter.so`/`libapp.so`), а Flutter ходить у
  мережу через власний Dart-стек (`dart:io`), який не читає Android'івський
  `network_security_config.xml` і не поважає системну Wi-Fi проксі — той самий клас
  проблеми, що й Dio-клієнт Сільпо. Патч зроблено про запас (раптом якась частина
  застосунку таки йде через нативний Android-стек), але перевірити його наживо не
  вдалось: спроба перехопити трафік через PCAPdroid (VPN-перехоплення, працює
  незалежно від того, чи застосунок бачить системну проксі) показала, що весь
  трафік UPG (`newmobi.upg.ua`) йде повз навіть VPN-перенаправлення на зовнішню
  проксі — потрібно окремо перевіряти налаштування PCAPdroid (список застосунків
  для розшифрування/зовнішньої проксі). Деталі — RESEARCH.md.

## Переустановка на телефоне (если понадобится заново)
```bash
export PATH=$PATH:~/Android/Sdk/platform-tools
adb uninstall <package>   # обязательно перед установкой патченной версии — подписи разные
adb install-multiple atb/patched_*_signed.apk
```
Package names: `com.atb.atbmarket`, `ua.fozzy.android`, `ua.fora.android`,
`com.supersmart.android.metro.ua`, `ua.epicentrk`, `ua.upg`

На MIUI/Xiaomi первая установка может потребовать подтверждения на самом экране телефона.

## Если версия приложения обновится
Патч нужно будет накатывать заново на новый APK (метод описан в RESEARCH.md, раздел
"Метод патчинга APK") — старые файлы отсюда для новой версии не подойдут.

import atexit
import argparse
import json
import os
import random
import shutil
import subprocess
import time

from patchright.sync_api import sync_playwright

PHONE = ""  # Example: 0631234567. Empty means that the script will ask for it.


def parse_args():
  parser = argparse.ArgumentParser(description="Тест авторизации и заказов Dnipro-M")
  parser.add_argument(
    "--headless",
    action="store_true",
    help="запустить обычный Chromium внутри скрытого виртуального X-дисплея",
  )
  parser.add_argument(
    "--phone",
    default=PHONE,
    help="телефон в формате 0XXXXXXXXX (по умолчанию — значение PHONE, иначе спросит)",
  )
  args = parser.parse_args()
  if not args.phone.strip():
    args.phone = input("Телефон Dnipro-M (например 0631234567): ").strip()
  return args


def start_virtual_display():
  """Start an isolated X server so headed Chromium has no physical window.

  Real headless Chromium is rejected by Dnipro-M's invisible reCAPTCHA. Xvfb
  gives Chromium a normal graphical environment while keeping it completely
  separate from the user's desktop. The process is stopped automatically when
  this script exits.
  """
  executable = shutil.which("Xvfb")
  if not executable:
    raise SystemExit(
      "Для --headless нужен Xvfb. Установите пакет: sudo apt-get install xvfb"
    )

  display_number = next(
    (
      number
      for number in range(90, 120)
      if not os.path.exists(f"/tmp/.X11-unix/X{number}")
    ),
    None,
  )
  if display_number is None:
    raise SystemExit("Не удалось найти свободный виртуальный X-дисплей")

  display = f":{display_number}"
  process = subprocess.Popen(
    [
      executable,
      display,
      "-screen", "0", "1400x900x24",
      "-nolisten", "tcp",
      "-ac",
    ],
    stdout=subprocess.DEVNULL,
    stderr=subprocess.DEVNULL,
  )

  socket_path = f"/tmp/.X11-unix/X{display_number}"
  deadline = time.monotonic() + 5
  while not os.path.exists(socket_path) and time.monotonic() < deadline:
    if process.poll() is not None:
      raise SystemExit("Xvfb завершился при запуске")
    time.sleep(0.05)
  if not os.path.exists(socket_path):
    process.terminate()
    raise SystemExit("Xvfb не запустился за 5 секунд")

  def stop():
    if process.poll() is None:
      process.terminate()
      try:
        process.wait(timeout=3)
      except subprocess.TimeoutExpired:
        process.kill()

  atexit.register(stop)
  os.environ["DISPLAY"] = display
  print(f"Скрытый виртуальный дисплей: {display}")
  return process


def human_type(locator, text: str, min_delay=0.1, max_delay=0.3):
  """
  Вводит текст посимвольно с рандомной паузой между нажатиями.
  """
  locator.click()
  for char in text:
    locator.press(char)
    time.sleep(random.uniform(0.05, 0.15))


def main():
  args = parse_args()
  if args.headless:
    start_virtual_display()

  with sync_playwright() as p:
    launch_options = {"headless": False, "channel": "chrome"}
    browser = p.chromium.launch(**launch_options)

    context = browser.new_context(
      viewport={"width": 1400, "height": 900},
    )

    page = context.new_page()

    print("Открываю dnipro-m.ua...")
    page.goto(
      "https://dnipro-m.ua/",
      wait_until="domcontentloaded",
    )

    print("Жду 3 секунды...")
    time.sleep(3)

    print("Открываю окно авторизации...")
    login_button = page.locator(
      'button[aria-label="Вхід/Реєстрація"]'
    )
    login_button.wait_for(state="visible", timeout=15_000)
    login_button.click()

    print("Жду поле телефона...")
    phone_input = page.locator("#phone-auth")
    phone_input.wait_for(state="visible", timeout=15_000)

    print(f"Ввожу телефон: {args.phone}")
    human_type(phone_input, args.phone)

    phone_verification = []

    def capture_phone_verification(response):
      if "/phone-verification/" not in response.url or response.request.method != "POST":
        return
      try:
        body = response.json()
      except Exception:
        body = response.text()
      phone_verification.append({
        "status": response.status,
        "url": response.url,
        "body": body,
      })

    page.on("response", capture_phone_verification)
    print("Нажимаю кнопку отправки кода...")
    submit_button = page.get_by_role("button", name="Отримати код")
    submit_button.wait_for(state="visible", timeout=15_000)
    submit_button.click()

    deadline = time.monotonic() + 45
    while not phone_verification and time.monotonic() < deadline:
      page.wait_for_timeout(250)
    page.remove_listener("response", capture_phone_verification)

    if not phone_verification:
      browser.close()
      raise SystemExit("Не получен ответ /phone-verification/ за 45 секунд")

    verification = phone_verification[-1]
    print(f"HTTP {verification['status']} {verification['url']}")
    print(json.dumps(verification["body"], ensure_ascii=False, indent=2))
    body = verification["body"]
    if (
      verification["status"] != 200
      or not isinstance(body, dict)
      or body.get("status") != "success"
    ):
      browser.close()
      raise SystemExit("Dnipro-M не подтвердил отправку SMS")

    print("Жду поле SMS-кода...")
    code_input = page.locator("#code")
    code_input.wait_for(state="visible", timeout=60_000)

    code = input("Введите код из SMS: ").strip()

    if not code:
      print("Код не введён.")
      browser.close()
      return

    print("Ввожу SMS-код...")
    human_type(code_input, code)

    print("Код введён. Жду завершения авторизации...")
    time.sleep(3)
    page.wait_for_function(
      """async () => {
        try {
          const response = await fetch('/auth/auth/user-info/', {
            credentials: 'same-origin',
            headers: {'X-Requested-With': 'XMLHttpRequest'},
          });
          const data = await response.json();
          return data && data.auth === true;
        } catch (_) {
          return false;
        }
      }""",
      timeout=30_000,
    )

    history_path = "/cabinet/order/history/"
    print(f"\nGET https://dnipro-m.ua{history_path}")
    history = page.evaluate(
      """async (path) => {
        const response = await fetch(path, {
          credentials: 'same-origin',
          headers: {
            'Accept': 'application/json, text/plain, */*',
            'X-Requested-With': 'XMLHttpRequest',
          },
        });
        const text = await response.text();
        let body;
        try { body = JSON.parse(text); } catch (_) { body = text; }
        return {status: response.status, url: response.url, body};
      }""",
      history_path,
    )

    print(f"HTTP {history['status']} {history['url']}")
    if history["status"] != 200:
      print(json.dumps(history["body"], ensure_ascii=False, indent=2))
      browser.close()
      raise SystemExit("Не удалось получить историю заказов")

    orders = history["body"].get("orders", []) if isinstance(history["body"], dict) else []
    if orders:
      # API возвращает заказы от новых к старым, поэтому первый — последний по времени.
      print("\nПоследний заказ:")
      print(json.dumps(orders[0], ensure_ascii=False, indent=2))
    else:
      print("\nЗаказы не найдены.")

    if not args.headless:
      input("\nНажмите Enter для закрытия браузера...")

    browser.close()


if __name__ == "__main__":
  main()

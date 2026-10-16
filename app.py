import os
import re
import random
import sqlite3
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify
import httpx
from yandex_music import Client

app = Flask(__name__)

# На Amvera папка /data сохраняет файлы навсегда даже после перезагрузки сервера
if os.path.exists("/data"):
    DB_PATH = "/data/sber_yandex_bridge.db"
else:
    DB_PATH = "sber_yandex_bridge.db"

# Ключи официального клиента Яндекс Музыки для обмена кода подтверждения на токен
YANDEX_CLIENT_ID = "23cabbbdc6cd418abb4b39c32c41195d"
YANDEX_CLIENT_SECRET = "53bc75238f0c4d08a118e51fe9203300"

# Кэш активных музыкальных сессий для переключения треков ("дальше")
music_sessions: Dict[str, Dict[str, Any]] = {}


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                sber_user_id TEXT PRIMARY KEY,
                yandex_token TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                pin_code TEXT PRIMARY KEY,
                yandex_token TEXT NOT NULL
            )
        """)
        conn.commit()


init_db()


def get_yandex_token(sber_user_id: str) -> Optional[str]:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT yandex_token FROM users WHERE sber_user_id = ?", (sber_user_id,))
        row = cursor.fetchone()
        return row[0] if row else None


def save_user_token(sber_user_id: str, yandex_token: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            INSERT INTO users (sber_user_id, yandex_token)
            VALUES (?, ?)
            ON CONFLICT(sber_user_id) DO UPDATE SET yandex_token = excluded.yandex_token
        """, (sber_user_id, yandex_token))
        conn.commit()


def bind_user_by_pin(sber_user_id: str, pin_code: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT yandex_token FROM auth_codes WHERE pin_code = ?", (pin_code,))
        row = cursor.fetchone()
        if not row:
            return False
        yandex_token = row[0]
        save_user_token(sber_user_id, yandex_token)
        cursor.execute("DELETE FROM auth_codes WHERE pin_code = ?", (pin_code,))
        conn.commit()
        return True


def exchange_yandex_code_for_token(code: str) -> Optional[str]:
    """Обменивает 7-значный цифровой код с экрана Яндекса на постоянный токен"""
    try:
        resp = httpx.post(
            "https://oauth.yandex.ru/token",
            data={
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": YANDEX_CLIENT_ID,
                "client_secret": YANDEX_CLIENT_SECRET,
            },
            timeout=10.0,
        )
        data = resp.json()
        return data.get("access_token")
    except Exception:
        return None


def extract_digits_from_voice(text: str) -> str:
    """Переводит произнесенные словами цифры ('один два три') в '123'"""
    words_map = {
        "ноль": "0", "нуль": "0",
        "один": "1", "одна": "1", "единица": "1", "раз": "1",
        "два": "2", "две": "2", "двойка": "2",
        "три": "3", "тройка": "3",
        "четыре": "4", "четверка": "4", "четвёрка": "4",
        "пять": "5", "пятерка": "5", "пятёрка": "5",
        "шесть": "6", "шестерка": "6", "шестёрка": "6",
        "семь": "7", "семерка": "7", "семёрка": "7",
        "восемь": "8", "восьмерка": "8", "восьмёрка": "8",
        "девять": "9", "девятка": "9",
    }
    tokens = re.split(r"[\s,.-]+", text.lower())
    result = []
    for t in tokens:
        if t in words_map:
            result.append(words_map[t])
        else:
            digits = "".join(re.findall(r"\d", t))
            if digits:
                result.append(digits)
    return "".join(result)


def get_track_from_yandex(sber_user_id: str, token: str, query: str, next_track: bool = False) -> Optional[Dict[str, str]]:
    """Ищет песню или включает любимые треки из Яндекс Музыки"""
    try:
        if sber_user_id not in music_sessions:
            client = Client(token).init()
            music_sessions[sber_user_id] = {"client": client, "tracks": [], "index": 0}

        session = music_sessions[sber_user_id]
        client: Client = session["client"]

        if next_track and session["tracks"]:
            session["index"] = (session["index"] + 1) % len(session["tracks"])
        elif query in ["", "музыку", "любимое", "моя волна", "песни"]:
            likes = client.users_likes_tracks()
            if not likes or not likes.tracks:
                return None
            session["tracks"] = likes.fetch_tracks()[:30]
            random.shuffle(session["tracks"])
            session["index"] = 0
        else:
            search_res = client.search(query)
            if not search_res or not search_res.tracks or not search_res.tracks.results:
                return None
            session["tracks"] = search_res.tracks.results[:15]
            session["index"] = 0

        track = session["tracks"][session["index"]]
        dl_info = track.get_download_info(get_direct_links=True)
        mp3_links = [d for d in dl_info if d.codec == "mp3"]
        direct_url = mp3_links[0].direct_link if mp3_links else dl_info[0].direct_link
        artists = ", ".join([a.name for a in track.artists]) if track.artists else "Яндекс Музыка"

        return {
            "title": track.title,
            "artist": artists,
            "url": direct_url
        }
    except Exception:
        return None


def ask_alice_reply(user_text: str) -> str:
    """Дружелюбные ответы в стиле Алисы"""
    q = user_text.lower()
    if "кто ты" in q or "ты алиса" in q:
        return "Я Алиса! Тайно поселилась внутри твоего Сбер Бума и готова включать твою Яндекс Музыку. Что послушаем?"
    if "как дела" in q or "как настроение" in q:
        return "Отлично! Сижу внутри колонки Сбера, держу наготове твои любимые треки из Яндекса."
    if "анекдот" in q or "пошути" in q:
        return "Спорят как-то Салют и Алиса, чья колонка громче, а сосед за стенкой просит выключить обе!"
    return f"Я тебя услышала: «{user_text}». Скажи «Включи музыку» или назови любимую песню, и я сразу запущу её!"


# Главная веб-страница для удобной привязки с телефона
@app.route("/", methods=["GET"])
def index_page():
    auth_url = f"https://oauth.yandex.ru/authorize?response_type=code&client_id={YANDEX_CLIENT_ID}"
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Привязка Сбер Бум к Яндексу</title>
        <style>
            body {{ font-family: system-ui, sans-serif; background: #121214; color: #fff; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
            .card {{ background: #1e1e24; padding: 28px; border-radius: 16px; max-width: 420px; width: 90%; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }}
            h2 {{ margin-top: 0; color: #fc3f1d; }}
            a.btn-link {{ display: block; text-align: center; background: #2b2b36; color: #5282ff; padding: 12px; border-radius: 8px; text-decoration: none; font-weight: bold; margin: 12px 0; border: 1px solid #3d3d4d; }}
            input {{ width: 100%; padding: 12px; margin: 12px 0; border-radius: 8px; border: 1px solid #333; background: #2b2b36; color: #fff; box-sizing: border-box; font-size: 16px; }}
            button {{ width: 100%; padding: 12px; background: #fc3f1d; color: #fff; border: none; border-radius: 8px; font-size: 16px; font-weight: bold; cursor: pointer; }}
            .hint {{ font-size: 14px; color: #bbb; line-height: 1.5; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Подключение Сбер Бум к Яндексу</h2>
            <p class="hint">
                1. Нажмите кнопку ниже и войдите в Яндекс — на экране появятся <b>7 цифр кода</b>:
            </p>
            <a class="btn-link" href="{auth_url}" target="_blank">Получить код в Яндексе</a>
            <p class="hint">
                2. Вы можете сразу продиктовать эти 7 цифр колонке ИЛИ вставить их сюда, чтобы получить короткий 4-значный ПИН-код:
            </p>
            <form action="/generate-pin" method="post">
                <input type="text" name="code_or_token" placeholder="Вставьте 7 цифр кода или токен" required>
                <button type="submit">Создать 4-значный ПИН для колонки</button>
            </form>
        </div>
    </body>
    </html>
    """


@app.route("/generate-pin", methods=["POST"])
def generate_pin_route():
    raw_input = request.form.get("code_or_token", "").strip()
    # Если пользователь ввел 7-значный цифровой код от Яндекса, меняем его на токен
    if raw_input.isdigit() and 6 <= len(raw_input) <= 8:
        token = exchange_yandex_code_for_token(raw_input)
        if not token:
            return """
            <body style="background:#121214;color:#fff;font-family:sans-serif;text-align:center;padding:40px;">
                <h2 style="color:#ff453a;">Код устарел или неверный</h2>
                <p>Пожалуйста, получите новый код по ссылке и попробуйте снова.</p>
                <a href="/" style="color:#5282ff;">Назад</a>
            </body>
            """
    else:
        token = raw_input

    pin = str(random.randint(1000, 9999))
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("INSERT OR REPLACE INTO auth_codes (pin_code, yandex_token) VALUES (?, ?)", (pin, token))
        conn.commit()

    spaced_pin = " ".join(list(pin))
    return f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
    <body style="background:#121214;color:#fff;font-family:sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;margin:0;">
        <div style="background:#1e1e24;padding:30px;border-radius:16px;text-align:center;max-width:400px;width:90%;">
            <h2 style="color:#4cd964;">ПИН-код готов!</h2>
            <p>Запустите навык на колонке Сбер Бум и скажите:</p>
            <div style="font-size:36px;font-weight:bold;letter-spacing:6px;background:#2b2b36;padding:15px;border-radius:12px;margin:20px 0;color:#fc3f1d;">
                Код {pin}
            </div>
            <p style="color:#aaa;font-size:14px;">Скажите голосом: «Код {spaced_pin}»</p>
        </div>
    </body>
    </html>
    """


# Вебхук для Сбер Салют (SmartMarket Studio)
@app.route("/webhook", methods=["POST"])
def sber_webhook():
    req_data = request.get_json() or {}
    message_name = req_data.get("messageName", "")
    uuid_obj = req_data.get("uuid", {})
    # Постоянный идентификатор пользователя Сбера
    sber_user_id = uuid_obj.get("sub") or uuid_obj.get("userId") or "default_user"

    payload = req_data.get("payload", {})
    msg_obj = payload.get("message", {})
    user_text = (msg_obj.get("original_text") or "").strip()
    user_text_lower = user_text.lower()

    yandex_token = get_yandex_token(sber_user_id)

    def make_resp(text: str, ssml: Optional[str] = None, finished: bool = False):
        return jsonify({
            "messageName": "ANSWER_TO_USER",
            "sessionId": req_data.get("sessionId", ""),
            "messageId": req_data.get("messageId", 0),
            "uuid": uuid_obj,
            "payload": {
                "pronounceText": ssml if ssml else text,
                "pronounceTextType": "application/ssml" if ssml else "application/text",
                "items": [{"bubble": {"text": text}}],
                "finished": finished,
                "auto_listening": not finished
            }
        })

    # 1. Если аккаунт Яндекса еще НЕ привязан
    if not yandex_token:
        digits = extract_digits_from_voice(user_text_lower)
        # Если назвали 4-значный ПИН с нашего сайта
        if len(digits) == 4:
            if bind_user_by_pin(sber_user_id, digits):
                return make_resp("Ура! Аккаунт Яндекса подключён и сохранён навсегда. Привет, я Алиса! Скажи: Включи музыку.")
            return make_resp(f"Код {digits} не найден. Получите новый код на сайте навыка.")

        # Если назвали 7-значный код напрямую со страницы Яндекса
        if 6 <= len(digits) <= 8:
            token = exchange_yandex_code_for_token(digits)
            if token:
                save_user_token(sber_user_id, token)
                return make_resp("Супер! Код Яндекса принят, аккаунт сохранён навсегда. Привет, я Алиса! Что для тебя включить?")
            return make_resp(f"Я услышала код {digits}, но Яндекс его не принял. Попробуйте ещё раз.")

        return make_resp(
            "Привет! Чтобы привязать Яндекс один раз и навсегда, откройте сайт нашего навыка, получите четырёхзначный код и скажите мне, например: Код один два три четыре."
        )

    # 2. Если аккаунт УЖЕ привязан и сохранен
    if message_name == "RUN_APP" or not user_text:
        return make_resp("Привет! Это Алиса, твой аккаунт Яндекса подключён. Включить музыку или поболтаем?")

    if user_text_lower in ["выход", "хватит", "стоп", "закрой навык"]:
        return make_resp("До встречи! Твой аккаунт Яндекса сохранён.", finished=True)

    if "отвяжи аккаунт" in user_text_lower or "сбрось аккаунт" in user_text_lower:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("DELETE FROM users WHERE sber_user_id = ?", (sber_user_id,))
            conn.commit()
        return make_resp("Аккаунт Яндекса отвязан. Назовите новый код, чтобы подключить снова.")

    # Переключение трека
    if any(w in user_text_lower for w in ["дальше", "следующий", "скип"]):
        track = get_track_from_yandex(sber_user_id, yandex_token, "", next_track=True)
        if track:
            msg = f"Переключаю: {track['artist']} — {track['title']}."
            return make_resp(msg, ssml=f"<speak>{msg} <audio src=\"{track['url']}\"/></speak>")
        return make_resp("Не удалось переключить трек. Скажите: Включи музыку.")

    # Включение музыки по запросу или "моя волна" / "любимое"
    if any(w in user_text_lower for w in ["включи", "поставь", "сыграй", "запусти", "моя волна", "музыку", "любимое"]):
        query = user_text_lower
        for word in ["алиса", "салют", "включи", "поставь", "сыграй", "запусти", "песню", "трек", "пожалуйста"]:
            query = query.replace(word, "").strip()

        track = get_track_from_yandex(sber_user_id, yandex_token, query)
        if track:
            msg = f"Включаю из Яндекс Музыки: {track['artist']} — {track['title']}."
            return make_resp(msg, ssml=f"<speak>{msg} <audio src=\"{track['url']}\"/></speak>")
        return make_resp("Не получилось найти этот трек в Яндекс Музыке. Попробуй сказать: Включи любимое.")

    # Обычный разговор с Алисой
    return make_resp(ask_alice_reply(user_text))


if __name__ == "__main__":
    # По умолчанию слушаем порт 80 (стандарт для Amvera) или порт из переменной окружения
    port = int(os.environ.get("PORT", 80))
    app.run(host="0.0.0.0", port=port)

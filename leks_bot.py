import os
import logging
import sqlite3
import datetime
from pathlib import Path

import dateparser
from pydub import AudioSegment
import speech_recognition as sr
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------- Настройки ----------
TOKEN = "ВАШ_ТОКЕН_БОТА"
DB_PATH = "leks_memory.db"
AUDIO_CACHE_DIR = "audio_cache"
MAX_DB_SIZE_BYTES = 4.5 * 1024 * 1024 * 1024  # 4.5 ГБ
REMINDER_GRACE_DAYS = 7

# Разрешённые пользователи – сюда добавьте свой Telegram ID
ALLOWED_USERS = [6661988889]  # Можно расширить список

# Создаем папку для временных аудиофайлов
Path(AUDIO_CACHE_DIR).mkdir(exist_ok=True)

# ---------- Логирование ----------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------- Декоратор для проверки доступа ----------
def restricted(func):
    """Обёртка: проверяет, что пользователь в списке разрешённых."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in ALLOWED_USERS:
            # Игнорируем: удаляем сообщение (если возможно) и ничего не делаем
            try:
                await update.message.delete()
            except Exception:
                pass
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

# ---------- Работа с базой данных ----------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            content TEXT NOT NULL,
            reminder_datetime TEXT,
            created_at TEXT NOT NULL,
            is_completed INTEGER DEFAULT 0
        )
        """
    )
    conn.commit()
    conn.close()

def get_db_size():
    try:
        return os.path.getsize(DB_PATH)
    except FileNotFoundError:
        return 0

def save_entry(user_id: int, entry_type: str, content: str, reminder_dt=None):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    created_at = datetime.datetime.now().isoformat()
    reminder_str = reminder_dt.isoformat() if reminder_dt else None
    cur.execute(
        "INSERT INTO entries (user_id, type, content, reminder_datetime, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, entry_type, content, reminder_str, created_at),
    )
    conn.commit()
    conn.close()
    enforce_size_limit()

def enforce_size_limit():
    if get_db_size() <= MAX_DB_SIZE_BYTES:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    now = datetime.datetime.now()
    cutoff = now + datetime.timedelta(days=REMINDER_GRACE_DAYS)

    delete_sql = """
        DELETE FROM entries
        WHERE id IN (
            SELECT id FROM entries
            WHERE type = 'note'
               OR (
                    type = 'reminder'
                    AND (
                        reminder_datetime IS NULL
                        OR datetime(reminder_datetime) <= datetime(?)
                    )
               )
            ORDER BY created_at ASC
            LIMIT 500
        )
    """
    while get_db_size() > MAX_DB_SIZE_BYTES * 0.9:
        cur.execute(delete_sql, (cutoff.isoformat(),))
        conn.commit()
        if cur.rowcount == 0:
            break
    conn.execute("VACUUM")
    conn.close()

def get_user_entries(user_id: int, entry_type=None, limit=10, offset=0):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    query = "SELECT id, type, content, reminder_datetime, created_at FROM entries WHERE user_id = ?"
    params = [user_id]
    if entry_type:
        query += " AND type = ?"
        params.append(entry_type)
    query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()
    return rows

def clear_all_notes(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM entries WHERE user_id = ? AND type = 'note'", (user_id,))
    deleted = cur.rowcount
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    return deleted

# ---------- Распознавание речи ----------
async def transcribe_voice(file_path: str) -> str:
    try:
        wav_path = file_path + ".wav"
        audio = AudioSegment.from_ogg(file_path)
        audio = audio.set_frame_rate(16000).set_channels(1)
        audio.export(wav_path, format="wav")

        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio_data = recognizer.record(source)

        text = recognizer.recognize_google(audio_data, language="ru-RU")
        return text
    except Exception as e:
        logger.error(f"Ошибка распознавания речи: {e}")
        return None
    finally:
        if os.path.exists(file_path):
            os.remove(file_path)
        if os.path.exists(wav_path):
            os.remove(wav_path)

# ---------- Извлечение даты ----------
def extract_reminder_datetime(text: str):
    dt = dateparser.parse(
        text,
        languages=["ru"],
        settings={
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": datetime.datetime.now(),
        },
    )
    return dt

# ---------- Обработчики команд (все с ограничением доступа) ----------
@restricted
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["📋 Все записи", "⏰ Напоминания"],
        ["🗑 Очистить заметки", "ℹ️ Помощь"],
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text(
        "Привет! Я Leks, твой ассистент с памятью.\n"
        "Просто напиши или надиктуй мысль, а я разберусь, сохранить её или поставить напоминание.\n"
        "Все данные хранятся локально.",
        reply_markup=reply_markup,
    )

@restricted
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Отправь мне текст или голосовое сообщение — я автоматически:\n"
        "• Сохраню заметку, если нет даты\n"
        "• Поставлю напоминание, если в тексте есть дата/время\n\n"
        "Кнопки:\n"
        "📋 Все записи — последние заметки и напоминания\n"
        "⏰ Напоминания — только активные напоминания\n"
        "🗑 Очистить заметки — удалить все заметки\n"
        "ℹ️ Помощь — это сообщение"
    )

@restricted
async def list_entries(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_entries(user_id, limit=10)
    if not entries:
        await update.message.reply_text("Записей пока нет.")
        return

    response = "Последние записи:\n"
    for idx, (eid, etype, content, rem_dt, created) in enumerate(entries, 1):
        line = f"{idx}. "
        if etype == "reminder":
            dt = datetime.datetime.fromisoformat(rem_dt)
            line += f"⏰ {dt.strftime('%d.%m.%Y %H:%M')} — {content}\n"
        else:
            line += f"📝 {content}\n"
        response += line
    await update.message.reply_text(response)

@restricted
async def list_reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    entries = get_user_entries(user_id, entry_type="reminder", limit=20)
    if not entries:
        await update.message.reply_text("Нет активных напоминаний.")
        return

    now = datetime.datetime.now()
    response = "⏰ Напоминания:\n"
    for eid, etype, content, rem_dt, created in entries:
        dt = datetime.datetime.fromisoformat(rem_dt)
        if dt > now:
            response += f"• {dt.strftime('%d.%m.%Y %H:%M')} — {content}\n"
    await update.message.reply_text(response or "Все напоминания уже прошли.")

@restricted
async def clear_notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    deleted = clear_all_notes(user_id)
    await update.message.reply_text(f"Удалено заметок: {deleted}")

# ---------- Обработка текста ----------
@restricted
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()

    reminder_dt = extract_reminder_datetime(text)

    if reminder_dt and reminder_dt > datetime.datetime.now():
        save_entry(user_id, "reminder", text, reminder_dt)
        confirmation = f"⏰ Напоминание установлено на {reminder_dt.strftime('%d.%m.%Y %H:%M')}"
    else:
        save_entry(user_id, "note", text)
        confirmation = "📝 Заметка сохранена"

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение: {e}")

    await update.message.reply_text(confirmation)

# ---------- Обработка голосовых ----------
@restricted
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id

    voice = update.message.voice
    file = await context.bot.get_file(voice.file_id)
    ogg_path = os.path.join(AUDIO_CACHE_DIR, f"{voice.file_id}.ogg")
    await file.download_to_drive(ogg_path)

    text = await transcribe_voice(ogg_path)
    if not text:
        await update.message.reply_text("❌ Не удалось распознать речь. Попробуй ещё раз или напиши текстом.")
        return

    reminder_dt = extract_reminder_datetime(text)

    if reminder_dt and reminder_dt > datetime.datetime.now():
        save_entry(user_id, "reminder", text, reminder_dt)
        confirmation = f"🗣 Распознано: «{text}»\n⏰ Напоминание на {reminder_dt.strftime('%d.%m.%Y %H:%M')}"
    else:
        save_entry(user_id, "note", text)
        confirmation = f"🗣 Распознано: «{text}»\n📝 Заметка сохранена"

    try:
        await update.message.delete()
    except Exception as e:
        logger.warning(f"Не удалось удалить сообщение: {e}")

    await update.message.reply_text(confirmation)

# ---------- Обработка кнопок ----------
@restricted
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    if text == "📋 Все записи":
        await list_entries(update, context)
    elif text == "⏰ Напоминания":
        await list_reminders(update, context)
    elif text == "🗑 Очистить заметки":
        await clear_notes_handler(update, context)
    elif text == "ℹ️ Помощь":
        await help_command(update, context)
    else:
        await handle_text(update, context)

# ---------- Запуск ----------
def main():
    init_db()
    enforce_size_limit()

    app = Application.builder().token(TOKEN).build()

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("list", list_entries))
    app.add_handler(CommandHandler("reminders", list_reminders))
    app.add_handler(CommandHandler("clearnotes", clear_notes_handler))

    # Голосовые
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Кнопки
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.Regex(
                r"^(📋 Все записи|⏰ Напоминания|🗑 Очистить заметки|ℹ️ Помощь)$"
            ),
            button_handler,
        )
    )

    # Все остальные текстовые
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text)
    )

    logger.info("Бот Leks запущен (ограниченный доступ)")
    app.run_polling()

if __name__ == "__main__":
    main()

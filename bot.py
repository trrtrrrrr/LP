import logging
import sqlite3
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
import aiohttp
from bs4 import BeautifulSoup

# --------------------- Настройки ---------------------
TOKEN = "8531935620:AAEG8rVtqlgFulRsOVxvbJCwGDbn2AhzoCI"         # токен от @BotFather
DB_NAME = "cs2_betting.db"
ADMIN_IDS = [6661988889]           # Telegram ID администраторов
INITIAL_BALANCE = 1000            # стартовый баланс для новых пользователей
UPDATE_INTERVAL = 30              # как часто проверять HLTV (секунды)

# --------------------- Логирование ---------------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# --------------------- База данных ---------------------
def get_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                balance INTEGER DEFAULT 0,
                registered_at TEXT
            );
            CREATE TABLE IF NOT EXISTS matches (
                match_id INTEGER PRIMARY KEY AUTOINCREMENT,
                team1 TEXT NOT NULL,
                team2 TEXT NOT NULL,
                score1 INTEGER DEFAULT 0,
                score2 INTEGER DEFAULT 0,
                status TEXT DEFAULT 'upcoming',
                coefficient_team1 REAL NOT NULL,
                coefficient_team2 REAL NOT NULL,
                winner INTEGER,
                tournament TEXT,
                hltv_url TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS bets (
                bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                match_id INTEGER,
                amount INTEGER,
                chosen_team INTEGER,
                coefficient REAL,
                status TEXT DEFAULT 'pending',
                placed_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );
        """)
        conn.commit()

# --------------------- Пользователи ---------------------
def get_user(user_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()

def create_user(user_id: int, username: str):
    with get_db() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT OR IGNORE INTO users (user_id, username, balance, registered_at) VALUES (?, ?, ?, ?)",
            (user_id, username, INITIAL_BALANCE, now),
        )
        conn.commit()

def update_balance(user_id: int, amount: int):
    with get_db() as conn:
        conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
        conn.commit()

# --------------------- Матчи ---------------------
def get_active_match():
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM matches WHERE status IN ('upcoming', 'live') ORDER BY match_id DESC LIMIT 1"
        ).fetchone()

def create_match(team1: str, team2: str, coef1: float, coef2: float, tournament: str = "", hltv_url: str = ""):
    active = get_active_match()
    if active:
        cancel_match(active["match_id"])
    with get_db() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO matches (team1, team2, coefficient_team1, coefficient_team2, status, tournament, hltv_url, created_at) VALUES (?, ?, ?, ?, 'upcoming', ?, ?, ?)",
            (team1, team2, coef1, coef2, tournament, hltv_url, now),
        )
        conn.commit()
    return get_active_match()

def cancel_match(match_id: int):
    with get_db() as conn:
        conn.execute("UPDATE matches SET status = 'cancelled' WHERE match_id = ?", (match_id,))
        bets = conn.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchall()
        for bet in bets:
            conn.execute("UPDATE bets SET status = 'refunded' WHERE bet_id = ?", (bet["bet_id"],))
            conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (bet["amount"], bet["user_id"]))
        conn.commit()

def finish_match(match_id: int, winner: int):
    with get_db() as conn:
        conn.execute("UPDATE matches SET status = 'finished', winner = ? WHERE match_id = ?", (winner, match_id))
        bets = conn.execute("SELECT * FROM bets WHERE match_id = ? AND status = 'pending'", (match_id,)).fetchall()
        for bet in bets:
            if bet["chosen_team"] == winner:
                win_amount = int(bet["amount"] * bet["coefficient"])
                conn.execute("UPDATE bets SET status = 'won' WHERE bet_id = ?", (bet["bet_id"],))
                conn.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (win_amount, bet["user_id"]))
            else:
                conn.execute("UPDATE bets SET status = 'lost' WHERE bet_id = ?", (bet["bet_id"],))
        conn.commit()

def update_score(match_id: int, score1: int, score2: int):
    with get_db() as conn:
        conn.execute("UPDATE matches SET score1 = ?, score2 = ? WHERE match_id = ?", (score1, score2, match_id))
        conn.commit()

def update_match_status(match_id: int, status: str):
    with get_db() as conn:
        conn.execute("UPDATE matches SET status = ? WHERE match_id = ?", (status, match_id))
        conn.commit()

# --------------------- Ставки ---------------------
def place_bet(user_id: int, match_id: int, team: int, amount: int, coefficient: float):
    user = get_user(user_id)
    if not user:
        return False, "Вы не зарегистрированы. Нажмите /start"
    if user["balance"] < amount:
        return False, "Недостаточно средств"
    match = get_db().execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
    if not match or match["status"] not in ("upcoming", "live"):
        return False, "Матч недоступен для ставок"
    with get_db() as conn:
        now = datetime.now().isoformat()
        conn.execute(
            "INSERT INTO bets (user_id, match_id, amount, chosen_team, coefficient, status, placed_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (user_id, match_id, amount, team, coefficient, now),
        )
        conn.execute("UPDATE users SET balance = balance - ? WHERE user_id = ?", (amount, user_id))
        conn.commit()
    return True, "Ставка принята!"

# --------------------- Парсер HLTV ---------------------
class HLTVParser:
    BASE_URL = "https://www.hltv.org"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept-Language": "en-US,en;q=0.5",
    }

    async def get_live_matches(self):
        """Возвращает список live-матчей с HLTV."""
        url = f"{self.BASE_URL}/matches"
        try:
            async with aiohttp.ClientSession(headers=self.HEADERS) as session:
                async with session.get(url, timeout=10) as response:
                    html = await response.text()
                    soup = BeautifulSoup(html, 'html.parser')
                    matches = []

                    # Ищем блок с live-матчами
                    live_section = soup.find('div', {'class': 'live-matches'})
                    if not live_section:
                        return matches

                    for match in live_section.find_all('a', {'class': 'match'}):
                        try:
                            teams = match.find_all('div', {'class': 'match-team'})
                            if len(teams) < 2:
                                continue
                            team1 = teams[0].text.strip()
                            team2 = teams[1].text.strip()

                            # Счёт
                            score_el = match.find('div', {'class': 'match-score'})
                            score1, score2 = "0", "0"
                            if score_el:
                                parts = score_el.text.strip().split('-')
                                score1 = parts[0].strip() if len(parts) > 0 else "0"
                                score2 = parts[1].strip() if len(parts) > 1 else "0"

                            # Турнир
                            event_el = match.find('div', {'class': 'match-event-name'})
                            tournament = event_el.text.strip() if event_el else ""

                            # Ссылка
                            href = match.get('href', '')
                            match_url = f"{self.BASE_URL}{href}" if href else ""

                            matches.append({
                                'team1': team1,
                                'team2': team2,
                                'score1': score1,
                                'score2': score2,
                                'tournament': tournament,
                                'url': match_url,
                            })
                        except Exception as e:
                            logger.error(f"Ошибка парсинга матча: {e}")
                            continue
                    return matches
        except Exception as e:
            logger.error(f"Ошибка получения HLTV: {e}")
            return []

# --------------------- Автоматическое обновление ---------------------
class AutoUpdater:
    """Фоновый цикл: следит за HLTV, обновляет счёт и статус."""

    def __init__(self, interval=UPDATE_INTERVAL):
        self.parser = HLTVParser()
        self.interval = interval
        self.running = False

    async def start(self):
        self.running = True
        logger.info("🚀 Автообновление запущено (HLTV)")
        while self.running:
            try:
                await self._update()
            except Exception as e:
                logger.error(f"Ошибка автообновления: {e}")
            await asyncio.sleep(self.interval)

    def stop(self):
        self.running = False

    async def _update(self):
        live_matches = await self.parser.get_live_matches()
        if not live_matches:
            return

        # Берём первый живой матч (можно адаптировать под несколько)
        match_data = live_matches[0]

        with get_db() as conn:
            # Ищем такой же матч в базе по названиям команд
            existing = conn.execute(
                "SELECT * FROM matches WHERE team1 = ? AND team2 = ? AND status IN ('upcoming', 'live')",
                (match_data['team1'], match_data['team2'])
            ).fetchone()

            score1 = int(match_data['score1']) if match_data['score1'].isdigit() else 0
            score2 = int(match_data['score2']) if match_data['score2'].isdigit() else 0

            if existing:
                # Обновляем статус и счёт
                if existing['status'] != 'live':
                    update_match_status(existing['match_id'], 'live')
                    logger.info(f"▶ {match_data['team1']} vs {match_data['team2']} → LIVE")

                if existing['score1'] != score1 or existing['score2'] != score2:
                    update_score(existing['match_id'], score1, score2)
                    logger.info(f"📊 Счёт обновлён: {score1}:{score2}")
            else:
                # Новый матч — создаём
                new_match = create_match(
                    match_data['team1'],
                    match_data['team2'],
                    1.85,   # коэффициент по умолчанию (можно улучшить)
                    1.95,
                    match_data.get('tournament', ''),
                    match_data.get('url', '')
                )
                if new_match:
                    update_match_status(new_match['match_id'], 'live')
                    logger.info(f"🆕 Создан матч: {match_data['team1']} vs {match_data['team2']}")

# --------------------- Команды бота ---------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user.id, user.username or "unknown")
    await update.message.reply_text(
        f"🎮 Привет, {user.first_name}!\n"
        f"💰 Стартовый баланс: {INITIAL_BALANCE} монет.\n"
        "/match — текущий матч\n"
        "/balance — баланс\n"
        "/mybets — мои ставки\n"
        "/leaderboard — топ игроков"
    )

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = get_user(update.effective_user.id)
    if user:
        await update.message.reply_text(f"💰 Баланс: {user['balance']} монет")
    else:
        await update.message.reply_text("Сначала /start")

async def match_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    match = get_active_match()
    if not match:
        await update.message.reply_text("❌ Нет активного матча. Бот сам найдёт ближайший live с HLTV.")
        return

    status_text = {"upcoming": "⏳ Ожидание", "live": "🔴 LIVE", "finished": "✅ Завершён"}.get(match["status"], match["status"])
    text = (
        f"🏆 {match['tournament'] or 'Турнир'}\n"
        f"🎮 {match['team1']} vs {match['team2']}\n"
        f"📊 Счёт: {match['score1']}:{match['score2']}\n"
        f"📡 Статус: {status_text}\n\n"
        f"📈 Коэффициенты:\n"
        f"   {match['team1']}: {match['coefficient_team1']:.2f}\n"
        f"   {match['team2']}: {match['coefficient_team2']:.2f}"
    )

    keyboard = [
        [InlineKeyboardButton(f"💰 {match['team1']} ({match['coefficient_team1']:.2f})", callback_data=f"bet_{match['match_id']}_1")],
        [InlineKeyboardButton(f"💰 {match['team2']} ({match['coefficient_team2']:.2f})", callback_data=f"bet_{match['match_id']}_2")],
        [InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{match['match_id']}")],
    ]
    if match.get('hltv_url'):
        keyboard.append([InlineKeyboardButton("🌐 HLTV", url=match['hltv_url'])])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def my_bets(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    with get_db() as conn:
        bets = conn.execute(
            "SELECT bets.*, matches.team1, matches.team2, matches.score1, matches.score2 "
            "FROM bets JOIN matches ON bets.match_id = matches.match_id "
            "WHERE bets.user_id = ? AND bets.status = 'pending'", (user_id,)
        ).fetchall()
    if not bets:
        await update.message.reply_text("У вас нет активных ставок.")
        return
    lines = ["📋 Ваши ставки:"]
    for bet in bets:
        team_name = bet["team1"] if bet["chosen_team"] == 1 else bet["team2"]
        lines.append(f"• {bet['team1']} {bet['score1']}:{bet['score2']} {bet['team2']} | {team_name} — {bet['amount']} монет (кф {bet['coefficient']:.2f})")
    await update.message.reply_text("\n".join(lines))

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        top = conn.execute("SELECT username, balance FROM users ORDER BY balance DESC LIMIT 10").fetchall()
    text = "🏆 Топ игроков:\n" + "\n".join(f"{i}. {u['username']} — {u['balance']} монет" for i, u in enumerate(top, 1))
    await update.message.reply_text(text)

# --------------------- Административные команды ---------------------
async def new_match_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        args = context.args
        team1, team2, coef1, coef2 = args[0], args[1], float(args[2]), float(args[3])
        tournament = args[4] if len(args) > 4 else ""
        m = create_match(team1, team2, coef1, coef2, tournament)
        await update.message.reply_text(f"✅ Матч создан: {m['team1']} vs {m['team2']}")
    except:
        await update.message.reply_text("Формат: /newmatch Team1 Team2 коэф1 коэф2 [турнир]")

async def fetch_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ручной запуск парсинга HLTV (для теста)."""
    if update.effective_user.id not in ADMIN_IDS:
        return
    parser = HLTVParser()
    matches = await parser.get_live_matches()
    if not matches:
        await update.message.reply_text("Нет live-матчей.")
        return
    text = "Найдены матчи:\n" + "\n".join(f"• {m['team1']} {m['score1']}:{m['score2']} {m['team2']}" for m in matches)
    await update.message.reply_text(text)

async def finish_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = get_active_match()
    if not match:
        await update.message.reply_text("Нет активного матча.")
        return
    try:
        winner = int(context.args[0])
        finish_match(match["match_id"], winner)
        team_winner = match["team1"] if winner == 1 else match["team2"]
        await update.message.reply_text(f"🏆 Матч завершён! Победитель: {team_winner}")
    except:
        await update.message.reply_text("Формат: /finish 1 (или 2)")

async def cancel_match_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    match = get_active_match()
    if not match:
        await update.message.reply_text("Нет активного матча.")
        return
    cancel_match(match["match_id"])
    await update.message.reply_text("❌ Матч отменён, ставки возвращены.")

async def topup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    try:
        uid, amount = int(context.args[0]), int(context.args[1])
        update_balance(uid, amount)
        await update.message.reply_text(f"✅ Баланс {uid} пополнен на {amount}")
    except:
        await update.message.reply_text("/topup user_id сумма")

# --------------------- Обработка кнопок ---------------------
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data

    if data.startswith("refresh_"):
        match_id = int(data.split("_")[1])
        match = get_db().execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if not match:
            await query.edit_message_text("Матч не найден.")
            return
        status_text = {"upcoming": "⏳ Ожидание", "live": "🔴 LIVE"}.get(match["status"], match["status"])
        text = (
            f"🏆 {match['tournament'] or 'Турнир'}\n"
            f"🎮 {match['team1']} vs {match['team2']}\n"
            f"📊 Счёт: {match['score1']}:{match['score2']}\n"
            f"📡 Статус: {status_text}\n"
            f"📈 Коэф: {match['team1']} {match['coefficient_team1']:.2f} / {match['team2']} {match['coefficient_team2']:.2f}"
        )
        keyboard = [
            [InlineKeyboardButton(f"💰 {match['team1']}", callback_data=f"bet_{match['match_id']}_1"),
             InlineKeyboardButton(f"💰 {match['team2']}", callback_data=f"bet_{match['match_id']}_2")],
            [InlineKeyboardButton("🔄 Обновить", callback_data=f"refresh_{match['match_id']}")]
        ]
        await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

    elif data.startswith("bet_"):
        _, match_id, team = data.split("_")
        match_id, team = int(match_id), int(team)
        match = get_db().execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if not match or match["status"] not in ("upcoming", "live"):
            await query.edit_message_text("Матч недоступен.")
            return
        team_name = match["team1"] if team == 1 else match["team2"]
        coeff = match[f"coefficient_team{team}"]
        user = get_user(query.from_user.id)
        balance = user["balance"] if user else 0

        amounts = [10, 50, 100, 500, balance] if balance else [10, 50, 100, 500]
        amounts = sorted(list(set(a for a in amounts if a <= balance or a <= 10)))
        keyboard = []
        row = []
        for amt in amounts:
            row.append(InlineKeyboardButton(str(amt), callback_data=f"amount_{match_id}_{team}_{amt}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            keyboard.append(row)
        keyboard.append([InlineKeyboardButton("❌ Отмена", callback_data="cancel_bet")])
        await query.edit_message_text(
            f"Ставка: {team_name} (кф {coeff:.2f})\nВаш баланс: {balance}\nВыберите сумму:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    elif data.startswith("amount_"):
        _, match_id, team, amount = data.split("_")
        match_id, team, amount = int(match_id), int(team), int(amount)
        match = get_db().execute("SELECT * FROM matches WHERE match_id = ?", (match_id,)).fetchone()
        if not match or match["status"] not in ("upcoming", "live"):
            await query.edit_message_text("Матч больше не принимает ставки.")
            return
        coeff = match[f"coefficient_team{team}"]
        success, msg = place_bet(query.from_user.id, match_id, team, amount, coeff)
        if success:
            await query.edit_message_text(
                f"✅ Ставка принята!\n"
                f"{match['team1']} vs {match['team2']}\n"
                f"Исход: {match['team1'] if team==1 else match['team2']}, {amount} монет, кф {coeff:.2f}\n"
                f"Возможный выигрыш: {int(amount*coeff)} монет"
            )
        else:
            await query.edit_message_text(f"❌ {msg}")

    elif data == "cancel_bet":
        await query.edit_message_text("Ставка отменена.")

# --------------------- Запуск ---------------------
def main():
    init_db()
    app = Application.builder().token(TOKEN).build()

    # Пользовательские команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("balance", balance))
    app.add_handler(CommandHandler("match", match_info))
    app.add_handler(CommandHandler("mybets", my_bets))
    app.add_handler(CommandHandler("leaderboard", leaderboard))

    # Административные команды
    app.add_handler(CommandHandler("newmatch", new_match_cmd))
    app.add_handler(CommandHandler("fetch", fetch_cmd))
    app.add_handler(CommandHandler("finish", finish_cmd))
    app.add_handler(CommandHandler("cancelmatch", cancel_match_cmd))
    app.add_handler(CommandHandler("topup", topup_cmd))

    # Кнопки
    app.add_handler(CallbackQueryHandler(button_handler))

    # Запуск фонового автообновления
    updater = AutoUpdater()
    async def start_updater(application):
        asyncio.create_task(updater.start())
    app.post_init = start_updater

    logger.info("Бот запущен!")
    app.run_polling()

if __name__ == "__main__":
    main()

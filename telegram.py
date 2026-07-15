import asyncio
import logging
import sqlite3
import os
import base64
import re

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, InlineQuery, InlineQueryResultCachedPhoto, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from openai import AsyncOpenAI

# =====================================================================
# КОНФИГУРАЦИЯ БОТА И API
# =====================================================================
BOT_TOKEN = "8610036419:AAFPWSxPzOj0k2k06QYTVgOftQAGfac3m0Q"
OPENROUTER_API_KEY = "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"

# Настройка логирования для контроля выполнения скрипта
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Инициализация асинхронного клиента OpenAI с указанием base_url OpenRouter
api_client = AsyncOpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# Модель: Nemotron-Nano (Vision-Language Model), оптимальна для анализа изображений и OCR
MODEL_NAME = "google/gemma-4-26b-a4b-it:free"

# Инициализация Telegram-бота и диспетчера
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# =====================================================================
# ОПТИМИЗИРОВАННЫЕ ПРОМПТЫ ДЛЯ ИИ
# =====================================================================
SYSTEM_PROMPT = (
"Ты — эксперт-архивариус, специализирующийся на точном тегировании изображений и мемов. "
"Твоя задача — проанализировать изображение и выдать строго нормализованные теги. "
"ПРАВИЛА, КОТОРЫЕ НЕЛЬЗЯ НАРУШАТЬ:\n"
"1. ЯЗЫК: Язык надписей должен быть сохранен, то есть они должны быть перенесены в тег один в один как на картинке. Все теги дублируй на русском и на английском, то есть для тега 'город' пропиши также тег 'city'. \n"
"2. МОРФОЛОГИЯ: Все слова должны быть существительными в именительном падеже, единственном числе "
"(например: 'кот', 'радость', 'компьютер', а не 'коты', 'радостный', 'за компьютером').\n"
"3. СТРУКТУРА: Выведи объекты на фото, эмоции/суть и весь прочитанный текст. Подбирай к выбранным тегам синонимы и также вноси их в теги, также вноси общие синонимы (например: если надпись Москва, будет уместен в том числе и тег 'город'). \n"
"Избегай мусорных слов ('картинка', 'фото', 'мем').\n"
"4. ФОРМАТ: ИСКЛЮЧИТЕЛЬНО слова или короткие фразы через запятую. Никаких точек, списков, "
"вводных фраз или пояснений. Только перечисление."
)

USER_PROMPT = "Проанализируй это изображение согласно системным правилам и выдай теги."

# =====================================================================
# БАЗА ДАННЫХ
# =====================================================================
def init_db():
    """
    Инициализирует локальную базу данных SQLite с механизмом базовой миграции.
    """
    try:
        with sqlite3.connect('bot_images.db') as conn:
            cursor = conn.cursor()
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS images (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id TEXT UNIQUE,
                    tags TEXT,
                    chat_id INTEGER,
                    bot_message_id INTEGER
                )
            ''')
            
            # Блок миграции DDL для существующих таблиц
            try:
                cursor.execute("ALTER TABLE images ADD COLUMN chat_id INTEGER")
                cursor.execute("ALTER TABLE images ADD COLUMN bot_message_id INTEGER")
            except sqlite3.OperationalError:
                # Исключение игнорируется, если колонки уже были добавлены ранее
                pass
                
            conn.commit()
        logging.info("База данных успешно инициализирована.")
    except Exception as e:
        logging.error(f"Ошибка при инициализации базы данных: {e}")


# =====================================================================
# ОБРАБОТЧИКИ СОБЫТИЙ (ХЕНДЛЕРЫ)
# =====================================================================

@dp.channel_post(F.photo)
async def handle_channel_photo(message: Message):
    """
    Обработчик новых постов с фото в канале.
    Выполняет скачивание, анализ через OpenRouter API, сохранение в БД и добавление кнопок управления.
    """
    try:
        # 1. Извлекаем file_id фотографии в максимальном разрешении
        photo = message.photo[-1]
        file_id = photo.file_id
        
        # 2. Извлекаем ручные теги из подписи к фото (caption)
        manual_tags = []
        if message.caption:
            clean_caption = re.sub(r'[#\n]', ' ', message.caption).lower()
            manual_tags = [tag.strip() for tag in clean_caption.replace(',', ' ').split() if tag.strip()]

        logging.info(f"Получено новое фото: {file_id}. Ручные теги: {manual_tags}. Начинаю загрузку...")

        # 3. Скачиваем фото и кодируем в base64
        file_info = await bot.get_file(file_id)
        downloaded_file = await bot.download_file(file_info.file_path)
        
        image_bytes = downloaded_file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        
        logging.info("Фото закодировано. Отправка запроса к OpenRouter API...")

        # 4. Формируем запрос согласно спецификации OpenAI Vision
        response = await api_client.chat.completions.create(
            model=MODEL_NAME,
            messages=[
                {
                    "role": "system",
                    "content": SYSTEM_PROMPT
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text", 
                            "text": USER_PROMPT
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{base64_image}"
                            }
                        }
                    ]
                }
            ]
        )
        
        # 5. Извлекаем ответ модели и подготавливаем массивы
        ai_tags_raw = response.choices[0].message.content.strip().lower()
        ai_tags = [tag.strip() for tag in ai_tags_raw.split(',') if tag.strip()]
        logging.info(f"Получены теги от ИИ: {ai_tags}")

        # 6. Операция объединения множеств с сохранением хронологического порядка
        combined_tags = list(dict.fromkeys(manual_tags + ai_tags))
        final_tags_str = ", ".join(combined_tags)

        row_id = None
        # 7. Сохраняем file_id и итоговые теги в БД, получаем ID записи
        with sqlite3.connect('bot_images.db') as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO images (file_id, tags) 
                VALUES (?, ?)
                ON CONFLICT(file_id) DO UPDATE SET tags=excluded.tags
            ''', (file_id, final_tags_str))
            
            # Извлекаем присвоенный ID для формирования безопасного callback_data
            cursor.execute("SELECT id FROM images WHERE file_id = ?", (file_id,))
            row_id = cursor.fetchone()[0]
            conn.commit()
            
        logging.info(f"Данные успешно сохранены в БД. ID записи: {row_id}")

        # 8. Формируем расширенную клавиатуру (добавлены кнопки дополнения и завершения)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Дописать теги", callback_data=f"add_tags:{row_id}"),
                InlineKeyboardButton(text="✅ Завершить", callback_data=f"finish:{row_id}")
            ],
            [InlineKeyboardButton(text="🗑 Удалить запись", callback_data=f"del_img:{row_id}")]
        ])

        # 9. Уведомляем канал об успешной обработке
        reply_text = "✅ Изображение успешно проанализировано.\n"
        if manual_tags:
            reply_text += f"👤 <b>Заданные теги:</b> {', '.join(manual_tags)}\n"
        reply_text += f"🤖 <b>ИИ-теги:</b> {', '.join(ai_tags)}\n"
        reply_text += f"🏷 <b>Сохраненные теги:</b> {final_tags_str}"

        sent_message = await message.reply(reply_text, parse_mode="HTML", reply_markup=keyboard)
        
        # 10. Сохраняем координаты сообщения бота (chat_id и message_id) для возможности его изменения
        with sqlite3.connect('bot_images.db') as conn:
            conn.execute("UPDATE images SET chat_id = ?, bot_message_id = ? WHERE id = ?",
                         (sent_message.chat.id, sent_message.message_id, row_id))
            conn.commit()

    except Exception as e:
        # Логирование в стандартный поток вывода (STDOUT)
        logging.error(f"Ошибка при обработке фото из канала: {e}")
        
        # Трансляция ошибки в интерфейс канала с защитой от вторичных отказов
        try:
            error_text = (
                f"❌ <b>Сбой обработки изображения</b>\n"
                f"Системное исключение:\n<pre>{str(e)}</pre>"
            )
            await message.reply(error_text, parse_mode="HTML")
        except Exception as notify_error:
            logging.error(f"Сбой при отправке уведомления об ошибке в канал: {notify_error}")

@dp.callback_query(F.data.startswith("add_tags:"))
async def handle_add_tags_callback(callback: CallbackQuery):
    """
    Обработчик нажатия на кнопку добавления тегов.
    Реализует паттерн Stateless-запроса через Reply-контекст.
    """
    try:
        row_id = int(callback.data.split(":")[1])
        # Формируем сообщение с внедренным состоянием (ID записи скрыт спойлером)
        prompt_text = (
            f"⏳ <b>Добавление тегов</b>\n"
            f"Ответьте (Reply) на это сообщение новыми тегами через запятую.\n"
            f"<tg-spoiler>Системный ID записи: #{row_id}</tg-spoiler>"
        )
        await callback.message.reply(prompt_text, parse_mode="HTML")
        await callback.answer()
    except Exception as e:
        logging.error(f"Ошибка в callback добавления тегов: {e}")
        await callback.answer("Системная ошибка запроса", show_alert=True)

@dp.channel_post(F.reply_to_message & F.text)
async def handle_tag_reply(message: Message):
    """
    Обработчик текстовых ответов в канале.
    Перехватывает ручные вводы, объединяет множества тегов и обновляет БД.
    """
    replied_text = message.reply_to_message.text
    if not replied_text or "Системный ID записи: #" not in replied_text:
        return

    # Извлекаем атомарный идентификатор транзакции
    match = re.search(r'Системный ID записи: #(\d+)', replied_text)
    if not match:
        return
        
    row_id = int(match.group(1))
    new_tags_raw = message.text

    # Фаза нормализации лексем
    clean_text = re.sub(r'[#\n]', ' ', new_tags_raw).lower()
    new_tags = [tag.strip() for tag in clean_text.replace(',', ' ').split() if tag.strip()]

    if not new_tags:
        return

    try:
        with sqlite3.connect('bot_images.db') as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT tags, chat_id, bot_message_id FROM images WHERE id = ?", (row_id,))
            result = cursor.fetchone()

            if not result:
                return

            existing_tags_str, chat_id, bot_message_id = result
            existing_tags = [t.strip() for t in existing_tags_str.split(',') if t.strip()]

            # Операция объединения с сохранением порядка (алгоритмическая детерминированность)
            combined_tags = list(dict.fromkeys(existing_tags + new_tags))
            final_tags_str = ", ".join(combined_tags)

            # Синхронизация данных
            cursor.execute("UPDATE images SET tags = ? WHERE id = ?", (final_tags_str, row_id))
            conn.commit()

        # Восстановление клавиатуры
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="➕ Дописать теги", callback_data=f"add_tags:{row_id}"),
                InlineKeyboardButton(text="✅ Завершить", callback_data=f"finish:{row_id}")
            ],
            [InlineKeyboardButton(text="🗑 Удалить запись", callback_data=f"del_img:{row_id}")]
        ])

        new_reply_text = (
            f"✅ Изображение успешно проанализировано и дополнено.\n"
            f"🏷 <b>Актуальные теги:</b> {final_tags_str}"
        )

        # Мутация изначального сообщения бота с обновленными тегами
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=bot_message_id,
            text=new_reply_text,
            parse_mode="HTML",
            reply_markup=keyboard
        )

        # Очистка мусорных данных (собиратель мусора интерфейса)
        await bot.delete_message(chat_id=message.chat.id, message_id=message.reply_to_message.message_id)
        await bot.delete_message(chat_id=message.chat.id, message_id=message.message_id)

    except Exception as e:
        logging.error(f"Ошибка при обновлении тегов в БД: {e}")

@dp.callback_query(F.data.startswith("del_img:"))
async def handle_delete_callback(callback: CallbackQuery):
    """
    Обработчик нажатия на кнопку удаления.
    Извлекает ID записи из callback_data, выполняет SQL-запрос DELETE 
    и удаляет связанные сообщения из канала.
    """
    try:
        # Извлекаем ID записи из строки формата "del_img:123"
        record_id = int(callback.data.split(":")[1])
        
        with sqlite3.connect('bot_images.db') as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM images WHERE id = ?", (record_id,))
            conn.commit()
            
        logging.info(f"Запись ID {record_id} успешно удалена из БД по запросу пользователя.")
        
        # Каскадное удаление сообщений из интерфейса канала
        try:
            # 1. Удаляем оригинальное сообщение пользователя (фотографию)
            if callback.message.reply_to_message:
                await bot.delete_message(
                    chat_id=callback.message.chat.id, 
                    message_id=callback.message.reply_to_message.message_id
                )
            # 2. Удаляем собственное сообщение бота (отчет с тегами и кнопками)
            await callback.message.delete()
        except Exception as delete_error:
            # Логируем предупреждение, если сообщения уже были удалены вручную
            logging.warning(f"Некритичная ошибка при очистке интерфейса канала: {delete_error}")
        
        # Обязательный ответ на callback для скрытия индикатора загрузки
        await callback.answer("Запись и сообщения успешно удалены")

    except Exception as e:
        logging.error(f"Ошибка при удалении записи ID {callback.data}: {e}")
        await callback.answer("Произошла ошибка при удалении", show_alert=True)

@dp.callback_query(F.data.startswith("finish:"))
async def handle_finish_callback(callback: CallbackQuery):
    """
    Обработчик кнопки завершения. 
    Удаляет inline-клавиатуру, фиксируя итоговое состояние поста (Read-Only).
    """
    try:
        # Снимаем клавиатуру для предотвращения дальнейших мутаций
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.answer("Тегирование завершено. Данные зафиксированы.")
    except Exception as e:
        logging.error(f"Ошибка при фиксации поста: {e}")
        await callback.answer("Ошибка фиксации", show_alert=True)


@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery):
    """
    Обработчик инлайн-запросов для поиска фото по тегам.
    """
    query = inline_query.query.strip().lower()
    results_db = []

    try:
        with sqlite3.connect('bot_images.db') as conn:
            cursor = conn.cursor()
            
            if not query:
                cursor.execute("SELECT file_id FROM images ORDER BY id DESC LIMIT 10")
            else:
                search_pattern = f"%{query}%"
                cursor.execute(
                    "SELECT file_id FROM images WHERE tags LIKE ? ORDER BY id DESC LIMIT 50", 
                    (search_pattern,)
                )
            results_db = cursor.fetchall()
            
    except Exception as e:
        logging.error(f"Ошибка при поиске в БД: {e}")
        return

    results = [
        InlineQueryResultCachedPhoto(
            id=str(idx),
            photo_file_id=file_id
        ) for idx, (file_id,) in enumerate(results_db)
    ]

    try:
        await inline_query.answer(results, cache_time=10, is_personal=True)
    except Exception as e:
         logging.error(f"Ошибка при отправке инлайн-ответа: {e}")


# =====================================================================
# ТОЧКА ВХОДА
# =====================================================================
async def main():
    init_db()
    logging.info("Запуск бота...")
    
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Бот остановлен вручную.")
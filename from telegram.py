from telegram.ext import Updater, CommandHandler

def delete_channel(update, context):
    channel_id = context.bot_data['channel_id']
    try:
        context.bot.leave_chat(chat_id=channel_id)
        update.message.reply_text("Канал успешно удален.")
    except Exception as e:
        update.message.reply_text(f"Произошла ошибка: {e}")

def main():
    updater = Updater("YOUR_BOT_TOKEN", use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("delete_channel", delete_channel))
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
from telegram.ext import Updater, CommandHandler

def get_subscribers(update, context):
    channel_username = "YOUR_CHANNEL_USERNAME"
    try:
        subscribers = []
        for member in context.bot.get_chat_members_count(channel_username):
            subscribers.append(member)
        update.message.reply_text(f"Количество подписчиков: {len(subscribers)}")
        # Здесь можно добавить код для сохранения данных о подписчиках
    except Exception as e:
        update.message.reply_text(f"Произошла ошибка: {e}")

def main():
    updater = Updater("YOUR_BOT_TOKEN", use_context=True)
    dp = updater.dispatcher
    dp.add_handler(CommandHandler("get_subscribers", get_subscribers))
    updater.start_polling()
    updater.idle()

if __name__ == '__main__':
    main()
from telegram import Bot

bot = Bot(token='ВАШ_ТОКЕН')

def get_subscribers(chat_id):
    members_count = bot.get_chat_member_count(chat_id)
    subscribers = []
    for user_id in range(members_count):
        member = bot.get_chat_member(chat_id, user_id)
        subscribers.append(member.user.username)
    return subscribers

# Пример использования
chat_id = '@имя_канала'
subscribers = get_subscribers(chat_id)
print(subscribers)
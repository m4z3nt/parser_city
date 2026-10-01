import requests

def add_bot_to_channel(token, chat_id):
    url = f"https://api.telegram.org/bot{token}/getChatMember"
    params = {
        'chat_id': chat_id,
        'user_id': '@BotName'
    }
    response = requests.get(url, params=params)
    return response.json()

# Пример использования
token = 'ВАШ_ТОКЕН'
chat_id = '@имя_канала'
response = add_bot_to_channel(token, chat_id)
print(response)
"""Генерирует ключ Fernet для ENCRYPTION_KEY.

    python gen_key.py

Скопируйте строку в .env. Не теряйте ключ: без него зашифрованные секреты в БД не прочитать.
"""
from cryptography.fernet import Fernet

if __name__ == "__main__":
    key = Fernet.generate_key().decode()
    print(key)
    print(f"\nДобавьте в .env:\nENCRYPTION_KEY={key}")

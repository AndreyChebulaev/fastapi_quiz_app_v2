"""Модуль для управления сессиями пользователя"""

import secrets
from typing import Optional

# Хранение сессий (в реальном приложении используйте Redis или базу данных)
active_sessions = {}

def create_session(username: str) -> str:
    """Создает сессию для пользователя"""
    session_token = secrets.token_hex(32)
    active_sessions[session_token] = username
    return session_token

def get_user_from_session(session_token: str) -> Optional[str]:
    """Получает пользователя из сессии по токену"""
    if session_token and session_token in active_sessions:
        return active_sessions[session_token]
    return None

def remove_session(session_token: str) -> None:
    """Удаляет сессию"""
    if session_token in active_sessions:
        del active_sessions[session_token]
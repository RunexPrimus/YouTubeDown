# utils/state_manager.py
from typing import Dict, Optional, List

# Oddiy in-memory state (chat_id -> dict)
# Real loyihada Redis yoki DB ishlatish kerak
USER_STATES: Dict[int, dict] = {}

class UserState:
    def __init__(self, chat_id: int):
        self.chat_id = chat_id
        self.search_results: List[dict] = []
        self.current_page: int = 1
        self.current_gallery: Optional[dict] = None
        self.current_image_index: int = 0
        self.total_images: int = 0
        self.image_urls: List[str] = []

def get_user_state(chat_id: int) -> UserState:
    if chat_id not in USER_STATES:
        USER_STATES[chat_id] = UserState(chat_id)
    return USER_STATES[chat_id]

def clear_user_state(chat_id: int):
    USER_STATES.pop(chat_id, None)

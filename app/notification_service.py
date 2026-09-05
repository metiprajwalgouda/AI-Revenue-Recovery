import json
import logging
from typing import Dict, Any, List
from fastapi import WebSocket

logger = logging.getLogger(__name__)

class NotificationManager:
    def __init__(self):
        # Maps customer_user_id -> List of active WebSockets
        self.active_connections: Dict[int, List[WebSocket]] = {}

    async def connect(self, customer_id: int, websocket: WebSocket):
        await websocket.accept()
        if customer_id not in self.active_connections:
            self.active_connections[customer_id] = []
        self.active_connections[customer_id].append(websocket)
        logger.info(f"WebSocket connected for customer {customer_id}")

    def disconnect(self, customer_id: int, websocket: WebSocket):
        if customer_id in self.active_connections:
            if websocket in self.active_connections[customer_id]:
                self.active_connections[customer_id].remove(websocket)
            if not self.active_connections[customer_id]:
                del self.active_connections[customer_id]
        logger.info(f"WebSocket disconnected for customer {customer_id}")

    async def send_personal_message(self, message: dict, customer_id: int):
        if customer_id in self.active_connections:
            for connection in self.active_connections[customer_id]:
                try:
                    await connection.send_json(message)
                except Exception as e:
                    logger.error(f"Error sending ws message to {customer_id}: {e}")

manager = NotificationManager()

def send_in_app_notification(db, customer_id: int, title: str, message: str, action_url: str = None):
    from app.db_models import InAppNotification
    import asyncio
    
    # 1. Persist to DB
    notif = InAppNotification(
        customer_user_id=customer_id,
        title=title,
        message=message,
        action_url=action_url,
        is_read=False
    )
    db.add(notif)
    db.commit()
    db.refresh(notif)
    
    # 2. Push real-time over WS if connected
    payload = {
        "id": notif.id,
        "title": notif.title,
        "message": notif.message,
        "action_url": notif.action_url,
      "created_at": notif.created_at.isoformat() if notif.created_at else None
    }
    
    try:
        from app.main import main_loop
        if main_loop and not main_loop.is_closed():
            asyncio.run_coroutine_threadsafe(manager.send_personal_message(payload, customer_id), main_loop)
    except Exception as e:
        logger.error(f"Failed to push WS notification: {e}")
        
    return notif
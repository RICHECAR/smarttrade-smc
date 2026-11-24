from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from backend.ws.manager import ws_manager

router = APIRouter()

@router.websocket("/ws/trades")
async def trades_ws(ws: WebSocket):
    # Determine which manager to use (global ws_manager is the single source of truth)
    await ws_manager.connect(ws)
    try:
        while True:
            # keep-alive: le client peut envoyer "ping" ou rien, mais on lit pour garder la session
            await ws.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(ws)

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .api.routes import router, message_callbacks


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    os.makedirs("./data", exist_ok=True)
    yield
    # Shutdown
    pass


app = FastAPI(title="Companion Bot", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(router)

# WebSocket for real-time chat
connected_websockets = []


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_websockets.append(websocket)

    async def forward_message(data: dict):
        for ws in connected_websockets:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    message_callbacks.append(forward_message)

    try:
        while True:
            msg = await websocket.receive_json()
            # 这里可以处理前端直接发送的消息
            # 但主要消息流走 /api/chat HTTP endpoint
            for ws in connected_websockets:
                try:
                    await ws.send_json({"type": "echo", "data": msg})
                except Exception:
                    pass
    except WebSocketDisconnect:
        pass
    finally:
        if forward_message in message_callbacks:
            message_callbacks.remove(forward_message)
        if websocket in connected_websockets:
            connected_websockets.remove(websocket)


# Static files (webui build)
class NoCacheStaticFiles(StaticFiles):
    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # 强制浏览器每次都向服务器校验，避免前端改了却显示旧缓存
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response


webui_dist = os.path.join(os.path.dirname(__file__), "..", "webui", "dist")
if os.path.exists(webui_dist):
    app.mount("/", NoCacheStaticFiles(directory=webui_dist, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)

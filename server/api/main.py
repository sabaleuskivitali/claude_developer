from contextlib import asynccontextmanager
from fastapi import FastAPI
import db, storage
from routers import events, errors, heartbeat, commands, screenshots, updates


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = await db.create_pool()
    app.state.event_queue = db.EventQueue(app.state.db)
    app.state.event_queue.start()
    await storage.ensure_bucket()
    yield
    await app.state.event_queue.stop()
    await app.state.db.close()


app = FastAPI(title="WinDiag API", version="2.0.0", lifespan=lifespan)

app.include_router(events.router)
app.include_router(errors.router)
app.include_router(heartbeat.router)
app.include_router(commands.router)
app.include_router(screenshots.router)
app.include_router(updates.router)


@app.get("/health")
async def health():
    return {"status": "ok"}

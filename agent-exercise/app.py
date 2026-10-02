"""HTTP service: POST /chat {"session_id": "...", "message": "..."}"""
import os
import traceback
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import tools
from agent import GeminiLLM, LeasingAgent, LLMUnavailable, log_event

if not os.path.exists(tools.DB_PATH):
    raise SystemExit(f"{tools.DB_PATH} not found. Run: python make_listings.py")

app = FastAPI(title="Leasing Agent")
agent = LeasingAgent(GeminiLLM())

FRIENDLY = {
    "auth": "I can't reach my assistant service right now because of a configuration problem. Our team has been notified.",
    "rate_limit": "I'm getting a lot of requests right now. Please try again in a minute.",
    "timeout": "That took too long to answer. Please try again.",
    "upstream": "I'm having trouble reaching my assistant service right now. Please try again in a moment.",
}


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=4000)


def error(status, code, message, session_id=None, **extra):
    body = {"error": {"code": code, "message": message, **extra}}
    if session_id:
        body["session_id"] = session_id
    return JSONResponse(status_code=status, content=body)


@app.post("/chat")
def chat(req: ChatRequest):
    try:
        reply = agent.chat(req.session_id, req.message)
        return {"session_id": req.session_id, "reply": reply}
    except LLMUnavailable as e:
        log_event("error_response", req.session_id, kind=e.kind, detail=e.detail)
        return error(503, "assistant_unavailable", FRIENDLY[e.kind], req.session_id)
    except Exception as e:                     # anything else: log it, never leak it
        ref = uuid.uuid4().hex[:8]
        log_event("unhandled_exception", req.session_id, ref=ref, error=repr(e),
                  traceback=traceback.format_exc())
        return error(500, "internal_error", "Something went wrong on our side. Please try again.",
                     req.session_id, reference=ref)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.exception_handler(RequestValidationError)
async def bad_request(request: Request, exc: RequestValidationError):
    return error(400, "bad_request",
                 'Send JSON like {"session_id": "abc", "message": "Hi"}.',
                 details=[{"field": ".".join(map(str, d["loc"][1:])), "problem": d["msg"]} for d in exc.errors()])


@app.exception_handler(Exception)
async def unexpected(request: Request, exc: Exception):
    ref = uuid.uuid4().hex[:8]
    # Full stack trace goes to the log only, never to the client.
    log_event("unhandled_exception", ref=ref, error=repr(exc), traceback=traceback.format_exc())
    return error(500, "internal_error", "Something went wrong on our side. Please try again.", reference=ref)

"""
chopper_service.py — Chopper's brain, as a tiny web service.

    POST /ask   {"question": "..."}   ->   {"answer": "..."}
    GET  /health

Same RAG + persona trick as the local assistant, but headless: it scores the
notebook against the question, injects the top matches plus Chopper's persona
voice, asks the local model (Ollama), and returns the reply in character.

Environment variables (all optional, sensible defaults for Docker):
    OLLAMA_URL          default http://ollama:11434/api/chat
    CHOPPER_MODEL       default llama3.2:3b
    NOTES_DIR           default /data/notes        (folder of .md notebooks)
    PERSONA_FILE        default /data/personas.json
    CHOPPER_PERSONA     default chopper            (which persona to speak as)
    CHOPPER_API_SECRET  if set, callers must send  Authorization: Bearer <secret>
    CHOPPER_NUM_CTX     default 8192
"""
import glob
import json
import os
import re

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

def _int_env(name: str, default: int) -> int:
    """Read an int env var, falling back to the default on anything bad
    (empty, non-numeric, or an accidental 'NAME=value' paste) instead of
    crashing the whole service on startup."""
    try:
        return int(str(os.environ.get(name, default)).strip())
    except (TypeError, ValueError):
        return default


OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434/api/chat")
MODEL = os.environ.get("CHOPPER_MODEL", "llama3.2:3b")
NOTES_DIR = os.environ.get("NOTES_DIR", "/data/notes")
PERSONA_FILE = os.environ.get("PERSONA_FILE", "/data/personas.json")
PERSONA_NAME = os.environ.get("CHOPPER_PERSONA", "chopper")
API_SECRET = os.environ.get("CHOPPER_API_SECRET", "")
NUM_CTX = _int_env("CHOPPER_NUM_CTX", 4096)     # context window (was 8192)
TOP_K = _int_env("CHOPPER_TOP_K", 12)           # how many notes to inject (was 24)
MAX_TOKENS = _int_env("CHOPPER_MAX_TOKENS", 220)  # cap reply length -> faster + shorter

app = FastAPI(title="Chopper AI")


def all_note_lines() -> list:
    """Every note line across every .md file in NOTES_DIR, with its topic.
    Read fresh each request so you can edit notes without restarting."""
    out = []
    for path in sorted(glob.glob(os.path.join(NOTES_DIR, "*.md"))):
        topic = os.path.basename(path)[:-3]
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if len(line) >= 15:
                        out.append((line, topic))
        except OSError:
            continue
    return out


def relevant_notes(query: str, k: int = 24) -> list:
    """RAG retrieval: top-k note lines by word overlap with the question."""
    words = {w for w in re.findall(r"[a-z0-9]+", query.lower()) if len(w) >= 3}
    if not words:
        return []
    scored = []
    for line, _topic in all_note_lines():
        lw = set(re.findall(r"[a-z0-9]+", line.lower()))
        score = sum(1 for w in words if w in lw)
        if score:
            scored.append((score, line))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [line for _, line in scored[:k]]


def persona_voice() -> str:
    try:
        with open(PERSONA_FILE, encoding="utf-8") as f:
            return json.load(f)[PERSONA_NAME]["voice"]
    except Exception:
        return ("You are Chopper, a snarky Discord bot for a Torn faction. "
                "Answer in first person, briefly, with a little attitude.")


def strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


class AskReq(BaseModel):
    question: str


@app.get("/health")
async def health():
    return {"ok": True, "model": MODEL, "note_lines": len(all_note_lines())}


@app.post("/ask")
async def ask(req: AskReq, authorization: str = Header(default="")):
    if API_SECRET and authorization != f"Bearer {API_SECRET}":
        raise HTTPException(status_code=401, detail="bad or missing token")

    question = (req.question or "").strip()
    if not question:
        return {"answer": "You gonna ask me something or just ping me for fun?"}

    notes = relevant_notes(question, TOP_K)
    system = persona_voice()
    if notes:
        system += ("\n\nUse ONLY these facts about yourself and Torn (do NOT show "
                   "the [source: ...] tags to the user):\n" + "\n".join(notes))
    else:
        system += ("\n\nYou have no saved notes covering this. If it isn't "
                   "something you'd know as the faction bot, say so in character.")

    payload = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": question},
        ],
        "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.7, "num_predict": MAX_TOKENS},
    }
    # qwen3 has a slow "thinking" mode; turn it off. Other models ignore this.
    if "qwen3" in MODEL.lower():
        payload["think"] = False

    try:
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(OLLAMA_URL, json=payload)
            r.raise_for_status()
            msg = r.json()["message"]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"model error: {e}")

    answer = strip_think(msg.get("content", "")) or "brain fart — ask again"
    return {"answer": answer[:1900]}

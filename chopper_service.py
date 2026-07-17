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


# Filler words that appear in tons of note lines and carry no retrieval signal.
STOPWORDS = {
    "who", "what", "when", "where", "why", "how", "which", "the", "and", "for",
    "are", "was", "does", "did", "can", "could", "would", "you", "your", "yours",
    "his", "her", "him", "she", "they", "them", "with", "that", "this", "these",
    "those", "have", "has", "had", "about", "from", "into", "get", "got", "being",
    "been", "tell", "know", "chopper", "torn", "there", "their", "will", "any",
    "all", "some", "one", "two", "per", "not", "but", "much", "many",
    # common 2-letter words (2-letter tokens are allowed so Torn slang like
    # "RW"/"OC"/"PI" works, but these carry no signal)
    "is", "do", "to", "of", "an", "or", "in", "on", "at", "by", "it", "be",
    "as", "we", "he", "my", "me", "up", "so", "no", "if", "us", "am", "go",
    "ok", "vs",
}

# Player phrasing -> words the notes actually use. Keeps keyword search from
# missing on synonyms (players say "overseas", notes say "abroad", etc.).
SYNONYMS = {
    "overseas": ["abroad", "foreign", "travel"],
    "abroad": ["overseas", "foreign", "travel"],
    "foreign": ["abroad", "overseas"],
    "oversea": ["abroad", "foreign", "travel"],
    "buy": ["purchase", "shop", "sell"],
    "buying": ["purchase", "shop"],
    "purchase": ["buy", "shop"],
    "carry": ["capacity", "luggage", "baggage", "travel"],
    "hold": ["capacity", "storage"],
    "luggage": ["capacity", "baggage", "travel"],
    "baggage": ["capacity", "luggage", "travel"],
    "fly": ["travel", "flight", "flying"],
    "flight": ["travel", "fly", "flying"],
    "plane": ["travel", "flight"],
    "heal": ["medical", "hospital", "life"],
    "revive": ["reviving", "revives"],
    "mug": ["mugging", "attack"],
    "bust": ["busting", "jail"],
    "od": ["overdose", "overdosing"],
    "gym": ["training", "train", "gains"],
    "money": ["cash", "income", "profit"],
    "cash": ["money", "income"],
    "stat": ["stats", "battle"],
    "cooldown": ["cooldowns"],
    "boss": ["admin", "staff", "leader"],
    "dev": ["developer", "staff"],
    "mod": ["moderator", "staff"],
    "employee": ["company", "job", "work"],
    "job": ["company", "work", "employee"],
    # common Torn slang -> the words the mechanic notes actually use
    "xan": ["xanax"], "zans": ["xanax"], "zan": ["xanax"], "zanny": ["xanax"],
    "hosped": ["hospital", "hospitalized"], "hosp": ["hospital"],
    "rev": ["revive", "reviving"], "freevive": ["revive", "reviving"],
    "fac": ["faction"], "fact": ["faction"],
    "oc": ["organized", "crime"], "rw": ["war", "ranked"], "ranked": ["war"],
    "str": ["strength"], "spd": ["speed"], "dex": ["dexterity"], "def": ["defense"],
    "bs": ["battle", "stats"], "tbs": ["battle", "stats"],
    "epi": ["epinephrine"], "tyro": ["tyrosine"], "sero": ["serotonin"],
    "mela": ["melatonin"], "vic": ["vicodin"], "edvd": ["erotic"],
    "fhc": ["coupon", "energy", "happy"], "dbk": ["knife", "melee"],
    "arma": ["armalite", "rifle"], "gak": ["ak", "rifle"],
    "fak": ["aid", "medical"], "sfak": ["medical", "aid"],
    "rehab": ["rehabilitation", "switzerland"], "chute": ["parachute", "dexterity"],
    "rig": ["oil"], "runner": ["travel", "abroad"], "loot": ["npc"],
    "chain": ["chaining"], "chaining": ["chain"],
    "make": ["making", "earn", "income"], "earn": ["making", "income", "money"],
    "rich": ["wealthy", "networth", "money"], "wealth": ["networth", "money"],
    "billion": ["billions"], "billions": ["billion"], "profit": ["money", "income"],
}


def _stem(w: str) -> str:
    """Crude singular/plural fold so 'drugs' matches 'drug', 'stocks' -> 'stock'."""
    if len(w) > 4 and w.endswith("ies"):
        return w[:-3] + "y"
    if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


def relevant_notes(query: str, k: int = 24) -> list:
    """RAG retrieval: top-k note lines, scored by IDF-weighted overlap. Query
    terms are stopword-filtered, expanded with synonyms, and stemmed so a rare
    word (a staff name) outranks filler, and phrasing/plurals don't cause misses."""
    import math
    base = [
        w for w in re.findall(r"[a-z0-9]+", query.lower())
        if len(w) >= 2 and w not in STOPWORDS
    ]
    if not base:
        return []
    # expand with synonyms, then stem everything
    expanded = set()
    for w in base:
        expanded.add(w)
        for s in SYNONYMS.get(w, []):
            expanded.add(s)
    words = {_stem(w) for w in expanded}
    if not words:
        return []
    lines = all_note_lines()
    n_docs = len(lines) or 1
    tokenized = []
    df = {}
    for line, _topic in lines:
        toks = {_stem(t) for t in re.findall(r"[a-z0-9]+", line.lower())}
        tokenized.append((line, toks))
        for w in words:
            if w in toks:
                df[w] = df.get(w, 0) + 1
    # rarer word -> higher weight; a unique name dominates common words
    idf = {w: math.log((n_docs + 1) / (df.get(w, 0) + 1)) + 1.0 for w in words}
    scored = []
    for line, toks in tokenized:
        # a note reads "- Subject (...): body". A query word in the Subject means
        # the line is ABOUT that thing, not just mentioning it - weight it 3x.
        subject = line.split(":", 1)[0].lower()
        subj = {_stem(t) for t in re.findall(r"[a-z0-9]+", subject)}
        score = sum(idf[w] * (3.0 if w in subj else 1.0)
                    for w in words if w in toks)
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
    # when the Discord message is a REPLY, the bot sends the message it replied
    # to so Chopper has conversational context (e.g. its own prior answer).
    reply_to: str | None = None
    reply_is_bot: bool = False   # True if reply_to was one of Chopper's messages


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

    reply_to = (req.reply_to or "").strip()
    # retrieve against the reply context too, so a short follow-up like "what do
    # they do?" still finds the right notes based on what was being discussed.
    retrieval_query = f"{reply_to} {question}".strip() if reply_to else question
    notes = relevant_notes(retrieval_query, TOP_K)
    system = persona_voice()
    if notes:
        system += (
            "\n\nAnswer using ONLY the facts below (do NOT show the [source: ...] "
            "tags). Do not add, guess, or embellish anything that isn't written "
            "here. CRITICAL - for real people (staff or players): state ONLY the "
            "role, ID, or fact given in the notes; NEVER invent a backstory, "
            "relationship, personality, skill, or anything else about them. BUT a "
            "person is often listed INSIDE a group line (e.g. among the Helpers, "
            "Officers, Moderators, or Developers) - if a name appears in such a "
            "list, that group IS their role, so answer it plainly (e.g. 'one of "
            "the Helpers'). The notes don't record players' genders, so refer to "
            "any player as 'they', never guess 'he' or 'she'. If you CAN answer "
            "what was actually asked, just answer it cleanly and stop - do NOT "
            "tack a 'no clue' disclaimer onto a complete answer, and do NOT "
            "volunteer that you're missing unrelated details nobody asked about "
            "(like someone's personal life). Only use 'no clue, ask Zach' when "
            "you truly can't answer the question at all because the person or "
            "thing asked about isn't anywhere in the facts below.\n\n"
            + "\n".join(notes))
    else:
        system += ("\n\nYou have no notes matching this message. If it's a Torn "
                   "question you can't answer, say so in character (e.g. 'no clue, "
                   "ask Zach'). If it's just chit-chat - a greeting, thanks, a "
                   "compliment, praise, or banter - fire back a short, snarky "
                   "in-character quip instead of deflecting (someone says 'good "
                   "boy', you might say 'damn right'). Either way, NEVER invent "
                   "Torn facts or details about real people.")

    messages = [{"role": "system", "content": system}]
    if reply_to and req.reply_is_bot:
        # the user replied to one of Chopper's messages -> feed it back as the
        # prior assistant turn so the conversation flows.
        messages.append({"role": "assistant", "content": reply_to})
        messages.append({"role": "user", "content": question})
    elif reply_to:
        # replied to another player's message -> give it as quoted context.
        messages.append({
            "role": "user",
            "content": f'[replying to another player who said: "{reply_to}"]\n{question}',
        })
    else:
        messages.append({"role": "user", "content": question})

    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "options": {"num_ctx": NUM_CTX, "temperature": 0.3, "num_predict": MAX_TOKENS},
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

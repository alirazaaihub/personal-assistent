"""
Long-Term Memory — ChromaDB + Google Embeddings
-------------------------------------------------
Har user message pe:
  1. Memory extract karo (LLM se)
  2. ChromaDB mein store karo
  3. Query pe relevant memories retrieve karo
"""

import re
import json
import uuid
from datetime import datetime
from langchain_chroma import Chroma
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.documents import Document
from config import CHROMA_DIR, log
from dotenv import load_dotenv
load_dotenv(override=True)

COLLECTION_NAME = "openclaw_memories"

# ── Embedding model (Google) ──────────────────────────────────────────────────
def get_embedding_fn():
    return GoogleGenerativeAIEmbeddings(model="models/embedding-001")

# ── ChromaDB client ───────────────────────────────────────────────────────────
def get_vectorstore() -> Chroma:
    return Chroma(
        collection_name=COLLECTION_NAME,
        embedding_function=get_embedding_fn(),
        persist_directory=str(CHROMA_DIR)
    )

# ── Extract memories from a user message ─────────────────────────────────────
EXTRACT_PROMPT = """You are a memory extractor for an AI assistant.

Given a user message, extract personal facts worth remembering long-term:
- Name, age, location
- Likes, dislikes, preferences
- Habits, hobbies, routines
- Technical stack, tools, frameworks
- Projects, goals, work context
- Communication style preferences

Rules:
- Only extract explicitly stated facts — no inference
- Each fact must be a single, standalone sentence
- If nothing worth saving, return exactly: NOTHING
- Otherwise return a JSON array of strings:
  ["Ali lives in Lahore, Pakistan", "Ali prefers concise answers"]

Pure JSON array or NOTHING — no markdown, no explanation."""


def extract_memories(user_message: str, llm) -> list[str]:
    """Extract memorable facts from a user message using LLM."""
    from langchain_core.messages import SystemMessage, HumanMessage
    try:
        resp = llm.invoke([
            SystemMessage(content=EXTRACT_PROMPT),
            HumanMessage(content=user_message)
        ])
        raw = resp.content.strip()
        raw = re.sub(r"^```json|^```|```$", "", raw, flags=re.MULTILINE).strip()

        if not raw or raw.upper() == "NOTHING":
            return []

        facts = json.loads(raw)
        return [f for f in facts if isinstance(f, str) and f.strip()]
    except Exception:
        return []


def save_memories(facts: list[str], source: str = "conversation"):
    """Save extracted facts to ChromaDB."""
    if not facts:
        return
    vs = get_vectorstore()
    docs = [
        Document(
            page_content=fact,
            metadata={
                "source":    source,
                "timestamp": datetime.now().isoformat(),
                "id":        str(uuid.uuid4())
            }
        )
        for fact in facts
    ]
    vs.add_documents(docs)
    print(f"\n🧠 [LongTermMemory] Saved {len(facts)} fact(s)")


def retrieve_memories(query: str, k: int = 5) -> str:
    """
    Retrieve top-k relevant memories for a given query.
    Returns formatted string for injection into system prompt.
    """
    try:
        vs = get_vectorstore()
        results = vs.similarity_search(query, k=k)
        if not results:
            return ""
        facts = [doc.page_content for doc in results]
        return "## Relevant Memories\n" + "\n".join(f"- {f}" for f in facts)
    except Exception:
        return ""


def process_user_message(user_message: str, llm):
    """
    Full pipeline: extract facts from message → save to ChromaDB.
    Call this for every incoming user message in background.
    """
    facts = extract_memories(user_message, llm)
    if facts:
        save_memories(facts, source="user_message")
    return facts
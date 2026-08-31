# Design: Kyra — AI Companion (v1)

## Overview

Kyra is a persistent AI companion: warm and protective (think Black Widow looking after the team) with a sharp, resourceful, "I did the reading" energy (think Hermione Granger). She remembers things about Duc across conversations, not just within one session — that long-term memory is the RAG core of this project.

This doc covers v1 scope: **text-only, persona + long-term memory**. Tools, voice, and animation are backlog items layered on top once v1 works end to end.

## Requirements

**Functional**
- Hold an in-character conversation as Kyra.
- Remember things across sessions — not just the current chat window, but weeks later.
- (Backlog, not v1) Use tools; speak; show expression via an avatar.

**Non-functional**
- Extensible: swapping the memory backend or adding a tool shouldn't require touching the core conversation loop.
- Inspectable: possible to see *which* memories got retrieved for a given reply — good practice, and useful for debugging "why did she say that."
- Cheap to run for a hobby project: local vector store, no paid infra required for v1.

## Concepts (the RAG part, explained once, used everywhere below)

- **Embedding**: text turned into a list of numbers (a vector) such that similar meanings land as nearby vectors. "I love hiking" and "I enjoy trails" end up close together even without sharing words.
- **Vector store**: a database built to answer "which stored vectors are closest to this new vector?" quickly. That's the trick behind memory retrieval.
- **Retrieval**: before Kyra answers, we embed the user's message, ask the vector store for the closest-matching stored memories, and hand those to the LLM as extra context. That's RAG — Retrieval-Augmented Generation.

## Architecture

Five small pieces, each behind a narrow interface so any one can be swapped later without touching the others:

```
Persona              -- who Kyra is (name, traits, tone) -> builds the system prompt
MemoryRecord         -- one stored memory (text, metadata, similarity score)
MemoryStore          -- interface: add() / retrieve()  ->  ChromaMemoryStore is the v1 implementation
Message              -- one turn in the current session (role, content)
ConversationManager  -- orchestrates a turn: retrieve -> build prompt -> call LLM -> store -> return
```

### `Persona`
Holds Kyra's character definition, turns it into a system prompt. Kept as data (a traits list + a few example lines) rather than hardcoded prose, so tone can be tuned without touching logic.

### `MemoryStore` (interface) → `ChromaMemoryStore`
```python
class MemoryStore(ABC):
    def add(self, text: str, metadata: dict) -> None: ...
    def retrieve(self, query: str, k: int = 5) -> list[MemoryRecord]: ...
```
`ChromaMemoryStore` is the concrete v1 backend, using Chroma's built-in local embedding model — no extra API calls or keys just to store/search memories. Swapping to a hosted vector DB later only touches this one class.

### `ConversationManager`
The orchestrator. One method, `handle_turn(user_input)`:
1. Retrieve relevant memories for `user_input`.
2. Build the full prompt: persona system prompt + retrieved memories + recent session history + the new message.
3. Call the LLM (Claude).
4. Store this exchange as a new memory.
5. Return the reply.

This is the class every future agentic tool plugs into later (Feature Backlog #1) — the tool-decision step slots in between steps 2 and 3.

## Why this shape (the LLD angle)

`MemoryStore` being an abstract interface rather than "just import chromadb everywhere" is the actual pattern interview test for with "how would you extend this design." Same reasoning will apply to `Tool` later — an interface now means adding a new tool or memory backend is additive, not a rewrite.

## Out of scope for v1

Tools, voice, animation, multi-agent orchestration, evals — see Feature Backlog in the Prep Roadmap.

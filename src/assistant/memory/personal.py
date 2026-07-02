"""Personal memory store — stores user facts and context memories with pgvector semantic search."""

from __future__ import annotations

import logging

from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from core.embedder import embed
from assistant.models import Memory

logger = logging.getLogger(__name__)


class PersonalMemory:
    """Async personal memory store backed by PostgreSQL + pgvector.

    Uses bge-m3 embeddings for semantic recall.
    Two memory types: 'user' (personal facts/preferences) and 'context' (project/task context).
    """

    def __init__(self, db: AsyncSession):
        self.db = db

    async def remember(
        self,
        *,
        fact: str,
        category: str = "note",
        memory_type: str = "user",
        user_id: int,
        source_thread_id: str | None = None,
    ) -> str:
        embedding_vec = embed(fact)
        memory = Memory(
            user_id=user_id,
            memory_type=memory_type,
            fact=fact,
            category=category,
            embedding=embedding_vec,
            source_thread_id=source_thread_id,
        )
        self.db.add(memory)
        await self.db.commit()
        await self.db.refresh(memory)
        logger.info("Remembered [%s/%s]: %s", memory_type, category, fact[:80])
        return str(memory.id)

    async def recall(
        self,
        *,
        query: str,
        n_results: int = 5,
        memory_type: str | None = None,
        user_id: int,
    ) -> list[dict]:
        embedding_vec = embed(query)
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id)
            .where(Memory.embedding.isnot(None))
            .order_by(Memory.embedding.cosine_distance(embedding_vec))
            .limit(n_results)
        )
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        result = await self.db.execute(stmt)
        return [m.to_dict() for m in result.scalars().all()]

    async def list_all(
        self,
        *,
        category: str | None = None,
        memory_type: str | None = None,
        user_id: int,
    ) -> list[dict]:
        stmt = (
            select(Memory)
            .where(Memory.user_id == user_id)
            .order_by(Memory.created_at.desc())
        )
        if category:
            stmt = stmt.where(Memory.category == category)
        if memory_type:
            stmt = stmt.where(Memory.memory_type == memory_type)
        result = await self.db.execute(stmt)
        return [m.to_dict() for m in result.scalars().all()]

    async def forget(self, entry_id: int) -> bool:
        memory = await self.db.get(Memory, entry_id)
        if memory is not None:
            await self.db.delete(memory)
            await self.db.commit()
            return True
        return False

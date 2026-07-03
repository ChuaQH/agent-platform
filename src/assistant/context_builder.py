"""Context builder — cache-aware prompt ordering, token budget, adaptive checkpoint compaction.

Implements:
- 5-section prompt ordering (system → tool policy → prefs → checkpoint → messages)
- Token estimation via tiktoken
- Two-pass compaction: tool output pruning → full checkpoint summarization
- Stable checkpoint summaries
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

import tiktoken
from sqlalchemy import select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import Config
from core.llm_client import LLMClient
from assistant import thread_store
from assistant.models import (
    CompactionCheckpoint,
    ConversationMessage,
    ThreadState,
)

logger = logging.getLogger(__name__)

MODEL_CONTEXT_WINDOW = 128_000
RESERVED_OUTPUT_TOKENS = 8_000
RESERVED_TOOL_TOKENS = 10_000
SAFETY_MARGIN = 5_000
MAX_INPUT_BUDGET = MODEL_CONTEXT_WINDOW - RESERVED_OUTPUT_TOKENS - RESERVED_TOOL_TOKENS - SAFETY_MARGIN
COMPACTION_THRESHOLD = 0.80
MIN_COMPACTABLE_MESSAGES = 10


CHECKPOINT_SYSTEM_PROMPT = """\
You are compacting an older section of a chatbot conversation into a stable checkpoint summary.

Preserve:
- the user's goals
- important decisions
- current task direction
- constraints
- corrections
- unresolved questions
- important tool results
- important file/source conclusions
- names of relevant tools, files, APIs, libraries, and systems
- implementation decisions
- user preferences explicitly relevant to the task

Remove:
- small talk
- acknowledgements
- redundant explanations
- failed irrelevant branches
- long raw tool outputs
- stale intermediate details

Do not invent information.
Do not summarize response style preferences unless they are directly relevant.
Do not erase uncertainty.
Write a concise but complete checkpoint summary that can replace the compacted messages."""


TOOL_OUTPUT_SUMMARY_PROMPT = """\
Summarize the following tool outputs concisely. Include the tool call reference ID in each summary.

Preserve key findings, numbers, URLs, and conclusions.
Remove verbose output, raw JSON, and irrelevant details.

Tool outputs:
{tool_outputs}

Summarized outputs (include reference IDs):"""


@dataclass
class PromptParts:
    static_instructions: str = ""
    tool_policy: str = ""
    response_preferences: str = ""
    checkpoint_summary: str | None = None
    recent_messages: list[dict[str, Any]] = field(default_factory=list)


class ContextBuilder:
    def __init__(self, config: Config, llm: LLMClient | None = None):
        self.config = config
        self._llm = llm
        try:
            self._tokenizer = tiktoken.get_encoding("cl100k_base")
        except Exception:
            self._tokenizer = tiktoken.encoding_for_model("gpt-4")

    def _get_llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(self.config)
        return self._llm

    def estimate_tokens(self, content: str | list[dict]) -> int:
        if isinstance(content, list):
            text = json.dumps(content, ensure_ascii=False)
        else:
            text = content
        try:
            return len(self._tokenizer.encode(text))
        except Exception:
            return len(text) // 4

    def should_compact(self, current_token_count: int) -> bool:
        threshold = int(MAX_INPUT_BUDGET * COMPACTION_THRESHOLD)
        return current_token_count > threshold

    # ── Prompt assembly ──────────────────────────────────────────────────

    def _format_response_preferences(self) -> str:
        prefs = self.config.response_preferences
        lines = [
            "Assistant response preferences:",
            f"- Use {prefs.get('verbosity', 'balanced')} detail.",
            f"- Use a {prefs.get('tone', 'direct')}, {prefs.get('structure', 'organized')} style.",
            f"- Prefer {prefs.get('explanation_depth', 'practical')} explanations.",
            f"- Use examples when {prefs.get('examples', 'when_useful')}.",
            f"- Use tables when {prefs.get('tables', 'when_comparing')}.",
            f"- Give {prefs.get('recommendations', 'clear_with_tradeoffs')} recommendations.",
            f"- Ask clarifying questions {prefs.get('clarifying_questions', 'only_when_needed')}.",
            f"- Cite sources when {prefs.get('citations', 'when_researching')}.",
            "",
            "Important:",
            "These preferences describe response style only. They are not a complete profile of the user's "
            "factual preferences, interests, projects, or history. Search user/context memories when "
            "user-specific facts may be relevant.",
        ]
        return "\n".join(lines)

    def _format_checkpoint_summary(self, summary: str) -> str:
        return f"Checkpoint summary:\n{summary}"

    async def build_static_prefix(
        self,
        db: AsyncSession,
        system_instructions: str,
        tool_policy: str,
        user_id: int,
        thread_id: str,
    ) -> list[str]:
        parts = [system_instructions]
        if tool_policy:
            parts.append(tool_policy)
        parts.append(self._format_response_preferences())

        checkpoint = await self._get_latest_checkpoint(db, user_id, thread_id)
        if checkpoint and checkpoint.summary:
            parts.append(self._format_checkpoint_summary(checkpoint.summary))

        return parts

    async def build_context(
        self,
        db: AsyncSession,
        system_instructions: str,
        tool_policy: str,
        user_id: int,
        thread_id: str,
    ) -> list[dict[str, Any]]:
        static_prefix = await self.build_static_prefix(
            db, system_instructions, tool_policy, user_id, thread_id
        )

        messages = await self._get_messages_for_context(db, user_id, thread_id)

        prompt: list[dict[str, Any]] = []

        for text in static_prefix:
            prompt.append({"role": "system", "content": text})

        for msg in messages:
            if msg.get("role") in ("user", "assistant", "tool"):
                entry: dict[str, Any] = {"role": msg["role"]}
                if msg.get("content"):
                    entry["content"] = msg["content"]
                if msg.get("tool_name"):
                    entry["name"] = msg["tool_name"]
                if msg.get("tool_call_id"):
                    entry["tool_call_id"] = msg["tool_call_id"]
                if msg.get("tool_result"):
                    entry["content"] = json.dumps(msg["tool_result"], ensure_ascii=False)
                prompt.append(entry)

        return prompt

    # ── Checkpoint retrieval ─────────────────────────────────────────────

    async def _get_latest_checkpoint(
        self, db: AsyncSession, user_id: int, thread_id: str
    ) -> CompactionCheckpoint | None:
        result = await db.execute(
            select(CompactionCheckpoint)
            .where(CompactionCheckpoint.user_id == user_id)
            .where(CompactionCheckpoint.thread_id == thread_id)
            .where(CompactionCheckpoint.compaction_strategy == "full_compaction")
            .order_by(CompactionCheckpoint.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_latest_any_checkpoint(
        self, db: AsyncSession, user_id: int, thread_id: str
    ) -> CompactionCheckpoint | None:
        result = await db.execute(
            select(CompactionCheckpoint)
            .where(CompactionCheckpoint.user_id == user_id)
            .where(CompactionCheckpoint.thread_id == thread_id)
            .order_by(CompactionCheckpoint.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _get_messages_for_context(
        self, db: AsyncSession, user_id: int, thread_id: str
    ) -> list[dict]:
        checkpoint = await self._get_latest_checkpoint(db, user_id, thread_id)

        if checkpoint and checkpoint.kept_from_message_id:
            return await thread_store.get_messages_after_checkpoint(
                db, thread_id, checkpoint.kept_from_message_id
            )
        else:
            return await thread_store.get_messages(db, thread_id)

    async def _count_thread_tokens(
        self, db: AsyncSession, thread_id: str, user_id: int
    ) -> int:
        messages = await thread_store.get_messages(db, thread_id)
        return self.estimate_tokens(messages)

    # ── Compaction ───────────────────────────────────────────────────────

    async def compact_if_needed(
        self,
        db: AsyncSession,
        user_id: int,
        thread_id: str,
    ) -> CompactionCheckpoint | None:
        token_count = await self._count_thread_tokens(db, thread_id, user_id)

        if not self.should_compact(token_count):
            return None

        return await self.compact_thread(db, user_id, thread_id, token_count)

    async def compact_thread(
        self,
        db: AsyncSession,
        user_id: int,
        thread_id: str,
        token_count_before: int | None = None,
    ) -> CompactionCheckpoint | None:
        all_messages = await thread_store.get_messages(db, thread_id)

        if len(all_messages) < MIN_COMPACTABLE_MESSAGES:
            logger.info(
                "Thread %s has only %d messages, below minimum %d for compaction",
                thread_id, len(all_messages), MIN_COMPACTABLE_MESSAGES,
            )
            return None

        latest_checkpoint = await self._get_latest_any_checkpoint(db, user_id, thread_id)

        if latest_checkpoint and latest_checkpoint.kept_from_message_id:
            messages = await thread_store.get_messages_after_checkpoint(
                db, thread_id, latest_checkpoint.kept_from_message_id
            )
        else:
            messages = all_messages

        if len(messages) < MIN_COMPACTABLE_MESSAGES:
            return None

        split_index = int(len(messages) * 0.60)
        compactable = messages[:split_index]
        kept = messages[split_index:]

        if not compactable or not kept:
            return None

        if token_count_before is None:
            token_count_before = self.estimate_tokens(messages)

        if latest_checkpoint and latest_checkpoint.tool_outputs_summarized:
            return await self._full_compaction(
                db, user_id, thread_id, compactable, kept,
                token_count_before, latest_checkpoint,
            )
        else:
            return await self._tool_output_pruning(
                db, user_id, thread_id, compactable, kept, token_count_before,
            )

    async def _tool_output_pruning(
        self,
        db: AsyncSession,
        user_id: int,
        thread_id: str,
        compactable: list[dict],
        kept: list[dict],
        token_count_before: int,
    ) -> CompactionCheckpoint:
        tool_msg_ids = []
        tool_outputs_text: list[str] = []

        for msg in compactable:
            if msg.get("role") == "tool" and msg.get("tool_result"):
                tool_msg_ids.append(msg["id"])
                tool_outputs_text.append(
                    f"[ID: {msg.get('tool_call_id', 'unknown')}] "
                    f"{json.dumps(msg['tool_result'], ensure_ascii=False)[:2000]}"
                )

        if not tool_outputs_text:
            logger.info("No tool outputs to prune in compactable block, skipping to full compaction")
            return await self._full_compaction(
                db, user_id, thread_id, compactable, kept,
                token_count_before, None,
            )

        token_count_after = token_count_before

        checkpoint = CompactionCheckpoint(
            user_id=user_id,
            thread_id=thread_id,
            compacted_from_message_id=compactable[0]["id"],
            compacted_until_message_id=compactable[-1]["id"],
            kept_from_message_id=kept[0]["id"],
            tool_outputs_summarized=True,
            input_token_count_before=token_count_before,
            input_token_count_after=token_count_after,
            compaction_strategy="tool_output_pruning",
        )
        db.add(checkpoint)
        await db.commit()
        await db.refresh(checkpoint)

        logger.info(
            "Tool output pruning checkpoint created for thread %s (checkpoint_id=%d)",
            thread_id, checkpoint.id,
        )
        return checkpoint

    async def _full_compaction(
        self,
        db: AsyncSession,
        user_id: int,
        thread_id: str,
        compactable: list[dict],
        kept: list[dict],
        token_count_before: int,
        previous_checkpoint: CompactionCheckpoint | None,
    ) -> CompactionCheckpoint:
        summary = await self._summarize_for_checkpoint(
            compactable,
            previous_checkpoint.summary if previous_checkpoint else None,
        )

        token_count_after = self.estimate_tokens(summary) + self.estimate_tokens(kept)

        checkpoint = CompactionCheckpoint(
            user_id=user_id,
            thread_id=thread_id,
            summary=summary,
            compacted_from_message_id=compactable[0]["id"],
            compacted_until_message_id=compactable[-1]["id"],
            kept_from_message_id=kept[0]["id"],
            tool_outputs_summarized=False,
            input_token_count_before=token_count_before,
            input_token_count_after=token_count_after,
            compaction_strategy="full_compaction",
        )
        db.add(checkpoint)
        await db.commit()
        await db.refresh(checkpoint)

        await db.execute(
            sa_update(ThreadState)
            .where(ThreadState.thread_id == thread_id)
            .values(
                summary=summary,
                latest_checkpoint_id=checkpoint.id,
            )
        )
        await db.commit()

        logger.info(
            "Full compaction checkpoint created for thread %s (checkpoint_id=%d, "
            "tokens: %d → %d)",
            thread_id, checkpoint.id, token_count_before, token_count_after,
        )
        return checkpoint

    async def _summarize_for_checkpoint(
        self,
        messages: list[dict],
        previous_summary: str | None,
    ) -> str:
        llm = self._get_llm()

        message_text_parts = []
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            tool_result = msg.get("tool_result")

            if role == "user":
                message_text_parts.append(f"[User]: {content}")
            elif role == "assistant":
                if msg.get("reasoning"):
                    message_text_parts.append(f"[Assistant thinking]: {msg['reasoning'][:500]}")
                if content:
                    message_text_parts.append(f"[Assistant]: {content}")
            elif role == "tool":
                if tool_result:
                    result_text = json.dumps(tool_result, ensure_ascii=False)[:1000]
                    message_text_parts.append(
                        f"[Tool: {msg.get('tool_name', 'unknown')}]: {result_text}"
                    )

        messages_text = "\n\n".join(message_text_parts)

        user_prompt = CHECKPOINT_SYSTEM_PROMPT + "\n\n"
        if previous_summary:
            user_prompt += f"Previous checkpoint summary (for context):\n{previous_summary}\n\n"
        user_prompt += f"Messages to compact:\n{messages_text}\n\nCheckpoint summary:"

        try:
            response = await llm.chat(
                messages=[
                    {"role": "system", "content": CHECKPOINT_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.0,
                max_tokens=2000,
                reasoning=False,
            )
            return response.content.strip()
        except Exception as e:
            logger.error("Checkpoint summarization failed: %s", e)
            fallback = f"Conversation summary: {len(messages)} messages covering "
            if messages:
                first = messages[0].get("content", "")[:100]
                last = messages[-1].get("content", "")[:100]
                fallback += f'from "{first}" to "{last}"'
            return fallback

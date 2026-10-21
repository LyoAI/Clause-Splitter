"""``RAGTool`` — the retrieval service's public interface.

Two independent things a caller asks of a corpus:

- **Retrieve.** ``search_by_question`` / ``normal_rag_recall`` return the
  passages relating to a query, as data. Dense retrieval only, mirroring the
  hosted service this replaces.
- **Answer.** ``answer_by_question`` returns a written answer, from an agent
  that may search lexically, semantically, or read whole documents.

Plus the corpus lifecycle: ``upload_file_complete`` prepares a folder, and
``get_file_headers`` / ``file_info`` / ``get_segments`` expose its documents.

General by construction: a corpus is any folder of MinerU-parseable documents,
a question any string, an answer whatever shape the caller asks for. Retrieval
policy travels in per-call arguments, never in configuration, and nothing here
knows what the documents are about.

The member set, signatures and result shapes are fixed by an existing consumer
that has to keep working unchanged; where its expectations were measured rather
than documented, the measurement is in ``scripts/PROBE_FINDINGS.md``. Two
deliberate departures from the original — ``recall_content_types`` honours the
whole list in caller-priority order, and ``content_type`` reports the passage's
real type — are explained in the README's Compatibility section.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Iterable

from chillrag.config import (
    ANSWER_MODE,
    ANSWER_TIMEOUT_S,
    MAX_TOOL_CALLS,
    RECALL_CANDIDATES,
    RECALL_TOP_K,
    SEARCH_TOP_K,
    SNIPPET_MAX_TOKENS,
)
from chillrag.corpus import CorpusManager, CorpusBundle
from chillrag.utils import get_logger

logger = get_logger()

#: Reported confidence on every result. The original service returns a constant
#: 0.6 and no consumer reads it; kept identical so the field costs nothing and
#: changes nothing.
DEFAULT_CONFIDENCE = 0.6

#: Characters of a document exposed as its header preview.
_HEADER_CHARS = 1000

#: ``source_id`` (a corpus) and ``file_id`` (a document) are integers in the
#: consumer's vocabulary — its prompts render them as numbers and its models
#: echo them back as numbers. Both are derived from a name by truncating an MD5
#: to 48 bits: stable across processes and machines, and wide enough that a
#: collision within one deployment is not a practical concern.
_ID_BITS = 48

#: source_id -> folder, for resolving a ``source_id_list`` argument. Populated
#: as tools attach to folders; a corpus is reachable by id after it has been
#: uploaded at least once in this process.
_FOLDER_BY_SOURCE_ID: dict[int, str] = {}

#: Answers are not cheap — an agent episode is several model calls — and bulk
#: callers ask the same question of the same corpus repeatedly. Entries are
#: keyed on everything that can change the answer, so a re-ask with different
#: instructions is a miss rather than a wrong hit.
_ANSWER_CACHE_MAX = 512


class _AnswerCache:
    """A small thread-safe LRU over answer strings."""

    def __init__(self, maxsize: int) -> None:
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple, str] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: tuple) -> str | None:
        with self._lock:
            if key not in self._entries:
                return None
            self._entries.move_to_end(key)
            return self._entries[key]

    def put(self, key: tuple, value: str) -> None:
        if not value:
            return  # never cache an empty answer; a retry may do better
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


_answer_cache = _AnswerCache(_ANSWER_CACHE_MAX)


def _stable_id(name: str) -> int:
    """A deterministic 48-bit integer id for ``name``."""
    digest = hashlib.md5(name.encode("utf-8")).digest()
    return int.from_bytes(digest[: _ID_BITS // 8], "big")


def _folder_name(foldername: str) -> str:
    """Validate that ``foldername`` is a folder name, and return it.

    A corpus is identified by a single name, used both to find the source
    folder and to name its cache directory. A path would join differently
    against each root — and an absolute one would land the cache inside the
    materials tree — so it is refused at the boundary rather than producing
    artifacts in an unexpected place.
    """
    name = str(foldername).strip()
    if not name:
        logger.error("foldername is empty")
        raise ValueError("foldername must be a non-empty folder name")
    if name in (".", "..") or "/" in name or "\\" in name or Path(name).is_absolute():
        logger.error("foldername %r is a path, not a folder name", name)
        raise ValueError(
            f"foldername must be a folder name, not a path: {name!r}. It is "
            f"resolved against the materials root, so pass e.g. 'example', not "
            f"'/data/example'."
        )
    return name


def source_id_for(folder: str) -> int:
    """The integer id of a corpus folder."""
    return _stable_id(folder)


def file_id_for(doc_id: str) -> int:
    """The integer id of a document within a corpus."""
    return _stable_id(doc_id)


class RAGTool:
    """Handle on one corpus. Cheap to construct, safe to construct often.

    Construction only assigns attributes — the corpus itself, its indexes and
    the embedding model belong to a process-wide ``CorpusManager``, so building
    a handle per request costs nothing.

    Per-instance state (``foldername`` / ``source_id`` / ``file_info`` /
    ``token`` / ``file_content``) is deliberately instance-scoped: callers read
    it back after ``upload_file_complete``, and sharing it across requests would
    cross their corpora.
    """

    def __init__(self) -> None:
        self.foldername: str | None = None
        self.source_id: int | None = None
        self.file_info: dict[str, int] | None = None
        self.file_content: list[dict] = []
        #: Placeholder for the credential the original service issues. No
        #: credential exists locally, but the attribute is part of the interface
        #: (it is passed back into ``get_segments``) and is kept truthy so any
        #: "are we authenticated" check reads the same as a logged-in original.
        self.token: str = "local"
        self._manager = CorpusManager.instance()

    # ------------------------------------------------------- corpus setup --

    def upload_file_complete(self, foldername: str, *, rebuild: bool = False) -> str | None:
        """Prepare a folder for retrieval: parse, chunk, index, expose.

        Idempotent and incremental: documents already ingested are reused and
        only new ones are parsed and embedded, so calling it again after adding
        a file costs one file's work. ``file_info`` is filled in as a side
        effect, and the caller reads it back immediately after.

        Args:
            foldername: A folder name under the materials root — one path
                component, not a path.
            rebuild: Discard the folder's derived artifacts and redo the whole
                pipeline. The incremental path decides what to redo from the
                source files' name, size and mtime, which no change to the
                *processing* can be seen in — so this is the only way to pick
                up a changed chunker, content-type tagging or embedding model.

        Returns:
            ``None`` on success. The original returns an error string; this
            raises instead, since a caller cannot retry with the same arguments.

        Raises:
            ValueError: ``foldername`` is a path rather than a name — a path
                would place the derived cache inside the materials tree.
            FileNotFoundError: the folder holds no parseable documents.
        """
        folder = _folder_name(foldername)
        self.foldername = folder
        bundle = self._manager.ensure(folder, rebuild=rebuild)
        self._attach(bundle)
        logger.info(
            "corpus %r ready: %d documents, %d chunks",
            folder, len(bundle.store.docs), len(bundle.store.all_chunks()),
        )
        return None

    def add_missing_file(self) -> list[dict] | None:
        """Ingest documents added to the folder since the last upload.

        Returns the newly available documents' headers, or ``None`` when there
        is nothing new — the original returns ``None`` in that case and its
        caller assigns the result straight through, so the shape is matched.
        """
        if self.foldername is None:
            logger.warning("add_missing_file before upload_file_complete; nothing to do")
            return None

        known = set(self.file_info or {})
        new_docs = [d for d in self._manager.source_docs(self.foldername) if d.name not in known]
        if not new_docs:
            logger.info("no new documents in %r; nothing to add", self.foldername)
            return None

        logger.info("ingesting %d new document(s) in %r", len(new_docs), self.foldername)
        self._attach(self._manager.ensure(self.foldername))
        return [self._header_of(doc) for doc in new_docs]

    def get_file_headers(self) -> list[dict] | None:
        """Every document's ``{"name", "fileid", "content"}``, content being its opening text.

        Returns ``None`` when no corpus is attached, matching the original's
        "you must upload first" behaviour.
        """
        if not self.file_info:
            logger.warning("no corpus attached; call upload_file_complete first")
            return None
        headers = [self._header_of_doc(doc_id) for doc_id in self.file_info]
        self.file_content = headers
        return headers

    def _attach(self, bundle: CorpusBundle) -> None:
        """Point this handle at ``bundle`` and publish its identity mappings."""
        self.foldername = bundle.folder
        self.source_id = source_id_for(bundle.folder)
        _FOLDER_BY_SOURCE_ID[self.source_id] = bundle.folder
        self.file_info = {doc_id: file_id_for(doc_id) for doc_id in bundle.doc_ids()}
        # A corpus that just changed invalidates every answer drawn from it.
        # Cheap to do on upload, and never wrong in the other direction.
        _answer_cache.clear()

    def _header_of(self, path: Path) -> dict:
        """Header for a source document, read from the corpus once ingested."""
        return self._header_of_doc(path.name)

    def _header_of_doc(self, doc_id: str) -> dict:
        """A document's preview: ``{"name", "fileid", "content"}``.

        ``content`` is the document's opening text, capped — the original
        returns the first 1000 characters of its first segment, which is what a
        caller uses to decide whether a document is relevant at all.
        """
        bundle = self._bundle()
        chunks = bundle.chunks_of(doc_id) if bundle else []
        text = "".join(c.text for c in chunks)
        return {
            "name": doc_id,
            "fileid": file_id_for(doc_id),
            "content": text[:_HEADER_CHARS],
        }

    def _bundle(self) -> CorpusBundle | None:
        """The attached corpus, loading it if the handle knows its folder."""
        if self.foldername is None:
            return None
        return self._manager.get(self.foldername)

    # ----------------------------------------------------------- retrieval --

    def search_by_question(
        self,
        question: str,
        source_id_list: Iterable[int] = (),
        origin_ids: Iterable[int] = (),
        recall_content_types: list[str] | None = None,
    ) -> list[dict]:
        """The passages most relevant to ``question``, best first.

        Args:
            question: Natural-language query.
            source_id_list: Corpora to search. Empty means this handle's own.
            origin_ids: Restrict to these documents (``file_id`` values, as
                reported by ``file_info``). Empty means all of them.
            recall_content_types: Restrict results to these forms of content —
                ``excel`` / ``text`` / ``title`` / ``image`` (see
                ``parser.blocks.CONTENT_TYPES``). Order is a priority:
                ``["excel", "text"]`` returns both, tables first. ``None`` (the
                default) does not filter at all.

        Returns:
            Up to ``RECALL_TOP_K`` result dicts, ranked.
        """
        results = self.normal_rag_recall(
            question,
            source_id_list,
            origin_ids,
            recall_content_types=recall_content_types,
        )
        # Already in final order — the type priority is a ranking, so re-sorting
        # by score here would undo it. Without a filter the two coincide.
        ranked = results[:RECALL_TOP_K]
        # ``rank`` is positional, so it is assigned after truncation.
        for i, result in enumerate(ranked, 1):
            result["rank"] = i
        return ranked

    def normal_rag_recall(
        self,
        question: str,
        source_id_list: Iterable[int] = (),
        origin_ids: Iterable[int] = (),
        context_trace_ids: str = "",
        recall_content_types: list[str] | None = None,
    ) -> list[dict]:
        """Dense retrieval with filters applied; ``search_by_question``'s engine.

        Same arguments as ``search_by_question``, plus ``context_trace_ids``.
        The original uses that to tag a request for server-side tracing; there
        is no server here, so it is accepted and ignored, keeping the signature
        call-compatible.

        Results are scored over a wider candidate pool than the caller asked
        for, because filtering happens after scoring: a type filter applied to
        only the top few hits would return a near-empty list. Everything that
        passes the filters is returned, in final ranking order — each caller
        truncates for itself.
        """
        bundle = self._resolve_bundle(source_id_list)
        searcher = self._manager.searcher(bundle.folder, "faiss")
        if searcher is None:
            logger.error("dense retrieval unavailable for corpus %r", bundle.folder)
            raise RuntimeError(
                f"dense retrieval is unavailable for corpus {bundle.folder!r}: "
                f"the corpus has no dense index, or faiss is not installed"
            )
        allowed_docs = self._resolve_docs(bundle, origin_ids)

        hits = searcher.search(question, RECALL_CANDIDATES)
        if recall_content_types and not any(
            hit.meta.get("content_types") for hit in hits
        ):
            # A corpus prepared before content types were recorded carries no
            # tags, so the filter can only ever return nothing. Distinguished
            # from "no passage matches", which looks identical to the caller
            # but means something entirely different.
            logger.warning(
                "corpus %r has no content-type tags: recall_content_types=%s "
                "cannot match. Rebuild the folder to tag it.",
                bundle.folder, list(recall_content_types),
            )
        selected = [
            hit for hit in hits
            if self._passes(hit, allowed_docs, recall_content_types)
        ]
        selected = self._order_by_type_priority(selected, recall_content_types)
        logger.info(
            "recall %r on %r: %d/%d hits (docs=%s, types=%s)",
            question[:60], bundle.folder, len(selected), len(hits),
            len(allowed_docs) if allowed_docs is not None else "all",
            list(recall_content_types) if recall_content_types else "all",
        )

        return [
            self._to_result(
                hit, rank=i, bundle=bundle,
                matched=self._matched_type(hit, recall_content_types),
            )
            for i, hit in enumerate(selected, 1)
        ]

    @staticmethod
    def _matched_type(hit, recall_content_types) -> str | None:
        """The requested type a hit satisfied, in the caller's priority order.

        ``None`` when no filter was applied. A passage can carry several forms
        at once (a heading and the table under it), so which type a filter
        matched is not recoverable from the passage's type list alone.
        """
        if not recall_content_types:
            return None
        types = set(hit.meta.get("content_types") or ())
        return next((name for name in recall_content_types if name in types), None)

    def _passes(self, hit, allowed_docs: set[str] | None, recall_content_types) -> bool:
        """Whether a hit survives the document and content-type filters."""
        if allowed_docs is not None and hit.meta.get("doc") not in allowed_docs:
            return False
        if not recall_content_types:
            return True
        return bool(set(hit.meta.get("content_types") or ()) & set(recall_content_types))

    @staticmethod
    def _order_by_type_priority(hits: list, recall_content_types) -> list:
        """Stable-sort hits so higher-priority types come first, score order within.

        The caller's list order is the priority, and a passage matching several
        requested types takes its best (earliest) one. Without a filter, score
        order stands.
        """
        if not recall_content_types:
            return hits
        priority = {name: i for i, name in enumerate(recall_content_types)}

        def rank(hit) -> int:
            matched = [
                priority[t] for t in (hit.meta.get("content_types") or ())
                if t in priority
            ]
            return min(matched) if matched else len(priority)

        # Python's sort is stable, and ``hits`` arrives in score order, so the
        # score ordering survives inside each priority group.
        return sorted(hits, key=rank)

    def _resolve_bundle(self, source_id_list: Iterable[int]) -> CorpusBundle:
        """The corpus a request targets: the named one, else this handle's own."""
        source_ids = [int(i) for i in (source_id_list or [])]
        for source_id in source_ids:
            folder = _FOLDER_BY_SOURCE_ID.get(source_id)
            if folder is not None:
                return self._manager.get(folder)
        if source_ids:
            logger.warning(
                "unknown source_id(s) %s; falling back to this handle's corpus", source_ids
            )
        bundle = self._bundle()
        if bundle is None:
            logger.error("no corpus selected and none attached to this handle")
            raise ValueError(
                "no corpus selected: call upload_file_complete(folder) first, or "
                "pass a source_id_list naming a corpus uploaded in this process"
            )
        return bundle

    def _resolve_docs(self, bundle: CorpusBundle, origin_ids: Iterable[int]) -> set[str] | None:
        """Translate ``origin_ids`` into document ids; ``None`` means "all documents"."""
        origin_ids = [int(i) for i in (origin_ids or [])]
        if not origin_ids:
            return None
        by_id = {file_id_for(doc_id): doc_id for doc_id in bundle.store.docs}
        resolved = {by_id[i] for i in origin_ids if i in by_id}
        unknown = [i for i in origin_ids if i not in by_id]
        if unknown:
            logger.warning("origin_ids not in corpus %r: %s", bundle.folder, unknown)
        return resolved

    @staticmethod
    def _to_result(
        hit, *, rank: int, bundle: CorpusBundle, matched: str | None = None
    ) -> dict[str, Any]:
        """One retrieval hit in the result shape consumers expect (seven fields).

        ``source`` is the passage's identity — ``"<file_id>#<chunk_index>"``,
        the chunk's reading position in its document, which is also what
        ``get_segments`` reports as ``index``. ``metadata`` is the display name,
        ``"文件名称:<file stem>"``; the label prefix is part of the interface,
        since consumers parse it to strip the filename back out.

        ``content_type`` is the type a filter matched, so a filtered result set
        can be checked without a second call; unfiltered, it is the passage's
        dominant form (``content_types`` is in priority order). The original
        returns a constant ``"text"``, which leaves a caller unable to tell a
        working filter from a corpus with no type information at all.
        """
        doc_id = hit.meta.get("doc", "")
        chunk = bundle.store.get_chunk(hit.chunk_id)
        if chunk is None:
            # A hit whose chunk the store does not hold means the index and the
            # store disagree — the retrieving artifacts are out of sync.
            logger.error("index/store mismatch: %r is not in corpus %r",
                         hit.chunk_id, bundle.folder)
        types = hit.meta.get("content_types") or []
        return {
            "confidence": DEFAULT_CONFIDENCE,
            "content_type": matched or (types[0] if types else "text"),
            "metadata": f"文件名称:{Path(doc_id).stem}",
            "rank": rank,
            "score": float(hit.score),
            "segment": hit.text,
            "source": f"{file_id_for(doc_id)}#{chunk.order if chunk else 0}",
        }

    # ---------------------------------------------------- document access --

    def get_segments(self, file_id: int, token: str | None = None) -> list[dict]:
        """Every passage of one document, in reading order.

        The whole-document readback path: a caller reconstructs a document's
        full text by concatenating ``segment.content`` across the returned
        segments, ``segment.index`` giving the order.

        Args:
            file_id: A document id from ``file_info``.
            token: Accepted for call compatibility; unused (no authentication).

        Returns:
            ``[{"segment": {"index": int, "content": str}}, ...]``, ascending.
            Empty if the document is not in the attached corpus.
        """
        bundle = self._bundle()
        if bundle is None:
            logger.warning("no corpus attached; call upload_file_complete first")
            return []
        doc_id = self._doc_id_for(bundle, file_id)
        if doc_id is None:
            logger.warning("file_id %s is not a document of corpus %r", file_id, bundle.folder)
            return []
        chunks = bundle.chunks_of(doc_id)
        logger.info("get_segments(%s) -> %d segments of %r", file_id, len(chunks), doc_id)
        return [
            {"segment": {"index": chunk.order, "content": chunk.text}}
            for chunk in chunks
        ]

    @staticmethod
    def _doc_id_for(bundle: CorpusBundle, file_id: int) -> str | None:
        """Reverse a ``file_id`` back to the document it names."""
        for doc_id in bundle.store.docs:
            if file_id_for(doc_id) == file_id:
                return doc_id
        return None

    # ------------------------------------------------------------- answer --

    def answer_by_question(
        self,
        question: str,
        source_id_list: Iterable[int] = (),
        *,
        instructions: str | None = None,
        output_instruction: str | None = None,
        lang: str = "",
        mode: str | None = None,
    ) -> str:
        """Answer ``question`` from the corpus, in natural language.

        Generation is unconstrained by default: the answer is however the agent
        phrases it.

        Args:
            question: What to answer.
            source_id_list: Corpora to use. Empty means this handle's own.
            instructions: Extra system-prompt requirements — domain vocabulary,
                house style, a required output contract.
            output_instruction: A requirement attached to *this* question, such
                as a format to answer in; ``instructions`` is for what holds
                throughout.
            lang: Prompt language, ``"zh"`` (default) or ``"en"``.
            mode: ``"agent"`` (default) or ``"oneshot"``, overriding
                ``ANSWER_MODE``. ``oneshot`` answers from the top dense hits in
                one model call — much faster, and lossier, for bulk work.

        Returns:
            The answer text, memoized per corpus and question.

        Raises:
            ValueError: no corpus to answer from.
            TimeoutError: the answer exceeded ``ANSWER_TIMEOUT_S``.
        """
        bundle = self._resolve_bundle(source_id_list)
        mode = (mode or ANSWER_MODE).lower()
        key = (bundle.folder, question, mode, lang, instructions or "", output_instruction or "")
        cached = _answer_cache.get(key)
        if cached is not None:
            logger.info("answer cache hit for %r", question[:60])
            return cached

        if mode == "oneshot":
            answer = self._answer_oneshot(bundle, question, instructions, lang)
        else:
            answer = self._answer_with_agent(
                bundle, question, instructions, output_instruction, lang
            )
        _answer_cache.put(key, answer)
        return answer

    def _answer_with_agent(
        self,
        bundle: CorpusBundle,
        question: str,
        instructions: str | None,
        output_instruction: str | None,
        lang: str,
    ) -> str:
        """Run one ReAct episode over the corpus's tools."""
        import asyncio

        from chillrag.agent import ReActAgent
        from chillrag.agent.prompts import build_user_prompt
        from chillrag.tools import ToolRegistry

        logger.info("answering %r on %r with the agent", question[:60], bundle.folder)
        llm = _get_llm()
        registry = ToolRegistry(
            self._manager.searchers(bundle.folder),
            bundle.store,
            top_k=SEARCH_TOP_K,
            snippet_max_tokens=SNIPPET_MAX_TOKENS,
        )
        agent = ReActAgent(
            llm,
            registry,
            max_tool_calls=MAX_TOOL_CALLS,
            instructions=instructions,
            lang=lang,
            user_prompt=lambda q: build_user_prompt(q, output_instruction),
        )
        try:
            result = asyncio.run(asyncio.wait_for(agent.arun(question), ANSWER_TIMEOUT_S))
        except asyncio.TimeoutError as e:
            logger.error("answering timed out after %.0fs", ANSWER_TIMEOUT_S)
            raise TimeoutError(
                f"answering exceeded {ANSWER_TIMEOUT_S:.0f}s "
                f"(raise ANSWER_TIMEOUT_S, or lower CHILLRAG_MAX_TOOL_CALLS)"
            ) from e
        logger.info(
            "answered in %d tool call(s) (%s)", result.num_tool_calls, result.terminated
        )
        return result.answer

    async def answer_stream(
        self,
        question: str,
        source_id_list: Iterable[int] = (),
        *,
        instructions: str | None = None,
        output_instruction: str | None = None,
        lang: str = "",
        history: list[dict] | None = None,
    ):
        """Answer ``question`` incrementally, yielding the agent's events.

        Mirrors :meth:`answer_by_question` in agent mode, but streams: a "tool"
        event after each tool call, then "delta" slices of the answer and a
        "done" event. Async end to end (no ``asyncio.run``), so it is safe under
        an ASGI server. ``history`` is prior user/assistant turns (multi-turn).
        """
        from chillrag.agent import ReActAgent
        from chillrag.agent.prompts import build_user_prompt
        from chillrag.tools import ToolRegistry

        bundle = self._resolve_bundle(source_id_list)
        registry = ToolRegistry(
            self._manager.searchers(bundle.folder),
            bundle.store,
            top_k=SEARCH_TOP_K,
            snippet_max_tokens=SNIPPET_MAX_TOKENS,
        )
        agent = ReActAgent(
            _get_llm(),
            registry,
            max_tool_calls=MAX_TOOL_CALLS,
            instructions=instructions,
            lang=lang,
            user_prompt=lambda q: build_user_prompt(q, output_instruction),
        )
        async for event in agent.arun_stream(question, history=history):
            yield event

    def _answer_oneshot(
        self,
        bundle: CorpusBundle,
        question: str,
        instructions: str | None,
        lang: str,
    ) -> str:
        """Answer from the top dense hits in a single model call, without tools.

        The bulk path: one embedding query and one completion, at the cost of
        the agent's ability to reformulate a query, read around a hit, or
        compute. Worth it when the answer is likely to sit in a passage the
        question already retrieves well.
        """
        import asyncio

        from chillrag.agent.prompts import get_system_prompt, prompt_lang

        logger.info("answering %r on %r in oneshot mode", question[:60], bundle.folder)
        hits = self.normal_rag_recall(question)[:RECALL_TOP_K]
        if not hits:
            # With nothing retrieved, answering anyway invites fabrication and
            # the caller cannot tell that answer from an evidenced one.
            logger.warning("oneshot: no passages retrieved for %r", question[:60])
            return ""

        context = "\n\n".join(
            f"[{h['rank']}] {h['metadata']}\n{h['segment']}" for h in hits
        )
        # The system prompt has a ``{tools}`` slot that is filled here with an
        # explicit "no tools" statement rather than a bare placeholder, and the
        # materials go into the user message — the question and its evidence
        # travel together.
        zh = prompt_lang(lang) == "zh"
        system = get_system_prompt(lang, instructions).format(
            tools="（无可用工具；仅依据用户消息中给出的材料作答）" if zh
            else "(no tools available; answer from the materials in the user message)"
        )
        resp = asyncio.run(
            _get_llm().ainvoke(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": f"{question}\n\n{context}"},
                ],
                temperature=0.0,
            )
        )
        if resp.finish_reason == "error":
            logger.error("oneshot answering failed: %s", resp.content)
            raise RuntimeError(f"answering failed: {resp.content}")
        logger.info("oneshot answer from %d passages", len(hits))
        return (resp.content or "").strip()

    # ------------------------------------------------------------ display --

    def __repr__(self) -> str:
        return (
            f"RAGTool(foldername={self.foldername!r}, source_id={self.source_id!r}, "
            f"files={len(self.file_info or {})})"
        )


def _model_name() -> str:
    """The configured chat model.

    Read lazily so importing this module never requires an LLM configuration —
    retrieval works without one, and only answering needs a model.
    """
    import os

    model = os.getenv("CHILLRAG_MODEL", "").strip()
    if not model:
        logger.error("CHILLRAG_MODEL is not set; cannot answer")
        raise RuntimeError(
            "no chat model configured: set CHILLRAG_MODEL to a model name that has a "
            "config/llm/<name>.yaml, e.g. CHILLRAG_MODEL=qwen3-max"
        )
    return model


def _get_llm():
    """The chat model, from the section ``CHILLRAG_LLM_PROVIDER`` names.

    Imported here rather than at module scope so retrieval works without any
    LLM configuration installed; only answering needs a model.
    """
    from chillrag.config import LLM_PROVIDER
    from chillrag.llm import get_llm

    return get_llm(_model_name(), LLM_PROVIDER)

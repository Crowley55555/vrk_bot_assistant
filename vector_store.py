"""
Управление векторной базой ChromaDB.

Отвечает за:
- Инициализацию / подключение к persistent-хранилищу.
- Индексацию чанков (upsert — добавление/обновление, удаление).
- Семантический поиск с фильтрацией по метаданным.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config import CHROMA_COLLECTION_NAME, CHROMA_PERSIST_DIR
from logger import get_logger
from scraper import process_to_chunks

log = get_logger(__name__)

_client: Optional[chromadb.ClientAPI] = None
_collection: Optional[chromadb.Collection] = None
_embedding_warmed: bool = False
_first_post_warmup_search_logged: bool = False


def reset_db() -> None:
    """
    Удаляет директорию ChromaDB и сбрасывает кэш подключения.
    После вызова следующий get_collection() создаст новую БД.
    Вызывайте при остановленном приложении, чтобы не держать открытые файлы.
    """
    global _client, _collection
    _collection = None
    _client = None
    path = Path(CHROMA_PERSIST_DIR)
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        log.info("ChromaDB: директория %s удалена", CHROMA_PERSIST_DIR)
    else:
        log.info("ChromaDB: директория %s не существовала", CHROMA_PERSIST_DIR)


def _get_client() -> chromadb.ClientAPI:
    global _client
    if _client is None:
        _client = chromadb.PersistentClient(
            path=CHROMA_PERSIST_DIR,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        log.info("ChromaDB: подключение к %s", CHROMA_PERSIST_DIR)
    return _client


def get_collection() -> chromadb.Collection:
    """Возвращает (или создаёт) коллекцию товаров."""
    global _collection
    if _collection is None:
        client = _get_client()
        _collection = client.get_or_create_collection(
            name=CHROMA_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
        log.info(
            "ChromaDB: коллекция '%s' — документов: %d",
            CHROMA_COLLECTION_NAME,
            _collection.count(),
        )
    return _collection


def warmup_embedding_and_search() -> None:
    """
    Прогревает ONNX / default embedding (all-MiniLM-L6-v2) и путь query Chroma.

    Вызывать при старте backend и telegram-bot, чтобы первый пользовательский запрос
    не блокировался на скачивании модели и не уводил callback Telegram в timeout.
    """
    global _embedding_warmed
    log.info("vector store warmup: started")
    try:
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

        ef = DefaultEmbeddingFunction()
        ef(["__vrk_embedding_warmup__"])
        log.info("vector store warmup: default embedding model ready (ONNX path)")
    except Exception as exc:
        log.exception("vector store warmup: embedding load failed — %s", exc)
        return

    col = get_collection()
    n = col.count()
    if n > 0:
        try:
            col.query(
                query_texts=["__vrk_chroma_query_warmup__"],
                n_results=min(1, n),
            )
            log.info("vector store warmup: Chroma query path completed | n_docs=%d", n)
        except Exception as exc:
            log.exception("vector store warmup: Chroma query failed — %s", exc)
            return
    else:
        log.info("vector store warmup: skip collection query (empty index)")

    _embedding_warmed = True
    log.info("vector store warmup: completed")


# ─── Индексация ────────────────────────────────────────────────────────────────

def index_chunks(chunks: list[dict]) -> int:
    """
    Добавляет / обновляет чанки в коллекции (upsert).
    Дедуплицирует по ID перед отправкой в ChromaDB.
    Возвращает количество обработанных документов.
    """
    if not chunks:
        log.warning("index_chunks: пустой список чанков")
        return 0

    # Дедупликация: если несколько чанков с одинаковым ID — оставляем первый
    seen: dict[str, int] = {}
    unique_chunks: list[dict] = []
    for chunk in chunks:
        cid = chunk["id"]
        if cid not in seen:
            seen[cid] = len(unique_chunks)
            unique_chunks.append(chunk)

    if len(unique_chunks) < len(chunks):
        log.warning(
            "index_chunks: убрано %d дубликатов ID",
            len(chunks) - len(unique_chunks),
        )

    col = get_collection()

    ids = [c["id"] for c in unique_chunks]
    documents = [c["text"] for c in unique_chunks]
    metadatas = [c["metadata"] for c in unique_chunks]

    col.upsert(ids=ids, documents=documents, metadatas=metadatas)
    log.info("ChromaDB: upsert %d документов", len(ids))
    return len(ids)


def remove_by_ids(article_ids: list[str]) -> int:
    """Удаляет документы по списку артикулов (дубликаты убираются)."""
    unique_ids = list(dict.fromkeys(article_ids))
    if not unique_ids:
        return 0
    col = get_collection()
    col.delete(ids=unique_ids)
    log.info("ChromaDB: удалено %d документов", len(unique_ids))
    return len(unique_ids)


def reindex_all() -> int:
    """Полная переиндексация: генерирует чанки из raw_products.json и загружает."""
    chunks = process_to_chunks()
    return index_chunks(chunks)


# ─── Поиск ─────────────────────────────────────────────────────────────────────

def search(
    query: str,
    n_results: int = 5,
    where: Optional[dict] = None,
) -> list[dict]:
    """
    Семантический поиск по коллекции.

    Параметры:
        query: текст запроса
        n_results: максимум результатов
        where: фильтр по метаданным (ChromaDB where-clause)

    Возвращает список словарей с ключами:
        id, text, metadata, distance
    """
    col = get_collection()
    total = col.count()
    if total == 0:
        log.warning("ChromaDB: коллекция пуста, поиск невозможен")
        return []

    kwargs: dict = {
        "query_texts": [query],
        "n_results": min(n_results, total),
    }
    if where:
        kwargs["where"] = where

    global _first_post_warmup_search_logged
    if _embedding_warmed and not _first_post_warmup_search_logged and total > 0:
        log.info("rag search: first query after warmup (no embedding cold start on critical path)")
        _first_post_warmup_search_logged = True

    try:
        results = col.query(**kwargs)
    except Exception as exc:
        log.error("ChromaDB: ошибка поиска — %s", exc)
        if where:
            log.info("ChromaDB: повтор поиска без фильтров")
            try:
                kwargs.pop("where", None)
                results = col.query(**kwargs)
            except Exception:
                return []
        else:
            return []

    items: list[dict] = []
    if results and results["ids"]:
        for i, doc_id in enumerate(results["ids"][0]):
            items.append({
                "id": doc_id,
                "text": results["documents"][0][i] if results["documents"] else "",
                "metadata": results["metadatas"][0][i] if results["metadatas"] else {},
                "distance": results["distances"][0][i] if results["distances"] else 1.0,
            })

    log.debug("Поиск '%s': найдено %d результатов", query[:60], len(items))
    return items

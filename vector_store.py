"""
Управление векторной базой ChromaDB.

Отвечает за:
- Инициализацию / подключение к persistent-хранилищу.
- Индексацию чанков (upsert — добавление/обновление, удаление).
- Семантический поиск с фильтрацией по метаданным.
"""

from __future__ import annotations

from typing import Optional

import chromadb
from chromadb.config import Settings as ChromaSettings

from config import CHROMA_COLLECTION_NAME, CHROMA_PERSIST_DIR
from logger import get_logger
from scraper import process_to_chunks

log = get_logger(__name__)

_client: Optional[chromadb.ClientAPI] = None
_collection: Optional[chromadb.Collection] = None


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


# ─── Индексация ────────────────────────────────────────────────────────────────

def index_chunks(chunks: list[dict]) -> int:
    """
    Добавляет / обновляет чанки в коллекции (upsert).
    Возвращает количество обработанных документов.
    """
    if not chunks:
        log.warning("index_chunks: пустой список чанков")
        return 0

    col = get_collection()

    ids = [c["id"] for c in chunks]
    documents = [c["text"] for c in chunks]
    metadatas = [c["metadata"] for c in chunks]

    # ChromaDB upsert: обновит существующие, добавит новые
    col.upsert(ids=ids, documents=documents, metadatas=metadatas)
    log.info("ChromaDB: upsert %d документов", len(ids))
    return len(ids)


def remove_by_ids(article_ids: list[str]) -> int:
    """Удаляет документы по списку артикулов."""
    if not article_ids:
        return 0
    col = get_collection()
    col.delete(ids=article_ids)
    log.info("ChromaDB: удалено %d документов", len(article_ids))
    return len(article_ids)


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

    kwargs: dict = {
        "query_texts": [query],
        "n_results": min(n_results, col.count() or 1),
    }
    if where:
        kwargs["where"] = where

    try:
        results = col.query(**kwargs)
    except Exception as exc:
        log.error("ChromaDB: ошибка поиска — %s", exc)
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

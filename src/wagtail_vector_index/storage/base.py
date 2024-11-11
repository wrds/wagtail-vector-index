import copy
from collections.abc import Generator, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, Protocol, TypeVar

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

from wagtail_vector_index.ai import get_chat_backend, get_embedding_backend
from wagtail_vector_index.ai_utils.backends.base import BaseEmbeddingBackend
from wagtail_vector_index.storage import (
    get_storage_provider,
)

StorageProviderClass = TypeVar("StorageProviderClass")
ConfigClass = TypeVar("ConfigClass")
IndexMixin = TypeVar("IndexMixin")


class DocumentRetrievalVectorIndexMixinProtocol(Protocol):
    """Protocol which defines the minimum requirements for a VectorIndex to be used with a mixin that provides
    document retrieval/generation"""

    def get_embedding_backend(self) -> BaseEmbeddingBackend: ...


class StorageVectorIndexMixinProtocol(Protocol[StorageProviderClass]):
    """Protocol which defines the minimum requirements for a VectorIndex to be used with a StorageProvider mixin."""

    storage_provider: StorageProviderClass

    def rebuild_index(self) -> None: ...

    def upsert(self) -> None: ...

    def get_documents(self) -> Iterable["Document"]: ...

    def _get_storage_provider(self) -> StorageProviderClass: ...


class StorageProvider(Generic[ConfigClass, IndexMixin]):
    """Base class for a storage provider that provides methods for interacting with a provider,
    e.g. creating and managing indexes."""

    config: ConfigClass
    config_class: type[ConfigClass]
    index_mixin: type[IndexMixin]

    def __init__(self, config: Mapping[str, Any]) -> None:
        try:
            config = dict(copy.deepcopy(config))
            self.config = self.config_class(**config)
        except TypeError as e:
            raise ImproperlyConfigured(
                f"Missing configuration settings for the vector backend: {e}"
            ) from e

    def __init_subclass__(cls, **kwargs: Any) -> None:
        if not hasattr(cls, "config_class"):
            raise AttributeError(
                f"Storage provider {cls.__name__} must specify a `config_class` class \
                    attribute"
            )
        return super().__init_subclass__(**kwargs)


@dataclass(kw_only=True, frozen=True)
class Document:
    """Representation of some content that is passed to vector storage backends.

    A document is usually a part of a model, e.g. some content split out from
    a VectorIndexedMixin model. One model instance may have multiple documents.

    The embedding_pk on a Document must be the PK of an Embedding model instance.
    """

    vector: Sequence[float]
    embedding_pk: int
    metadata: Mapping


class DocumentConverter(Protocol):
    def to_documents(
        self, object: object, *, embedding_backend: BaseEmbeddingBackend
    ) -> Generator[Document, None, None]: ...

    def from_document(self, document: Document) -> object: ...

    def bulk_to_documents(
        self, objects: Iterable[object], *, embedding_backend: BaseEmbeddingBackend
    ) -> Generator[Document, None, None]: ...

    def bulk_from_documents(
        self, documents: Iterable[Document]
    ) -> Generator[object, None, None]: ...


@dataclass
class QueryResponse:
    """Represents a response to the VectorIndex `query` method,
    including a response string and a list of sources that were used to generate the response
    """

    response: Iterable[object]
    sources: Iterable[object]


class VectorIndex(Generic[ConfigClass]):
    """Base class for a VectorIndex, representing some set of documents that can be queried"""

    # The alias of the backend to use for generating embeddings when documents are added to this index
    embedding_backend_alias: ClassVar[str] = "default"

    # The alias of the storage provider specified in WAGTAIL_VECTOR_INDEX_STORAGE_PROVIDERS
    storage_provider_alias: ClassVar[str] = "default"

    def get_embedding_backend(self) -> BaseEmbeddingBackend:
        return get_embedding_backend(self.embedding_backend_alias)

    def get_documents(self) -> Iterable[Document]:
        raise NotImplementedError

    def get_converter(self) -> DocumentConverter:
        raise NotImplementedError

    # Public API

    def query(
        self, query: str, *, sources_limit: int = 5, chat_backend_alias: str = "default"
    ) -> QueryResponse:
        """Perform a natural language query against the index, returning a QueryResponse containing the natural language response, and a list of sources"""
        try:
            query_embedding = next(self.get_embedding_backend().embed([query]))
        except StopIteration as e:
            raise ValueError("No embeddings were generated for the given query.") from e

        similar_documents = list(self.get_similar_documents(query_embedding))

        sources = self._deduplicate_list(
            self.get_converter().bulk_from_documents(similar_documents)
        )

        merged_context = "\n".join(doc.metadata["content"] for doc in similar_documents)
        prompt = (
            getattr(settings, "WAGTAIL_VECTOR_INDEX_QUERY_PROMPT", None)
            or "You are a helpful assistant. Use the following context to answer the question. Don't mention the context in your answer."
        )
        messages = [
            {"content": prompt, "role": "system"},
            {"content": merged_context, "role": "system"},
            {"content": query, "role": "user"},
        ]
        chat_backend = get_chat_backend(chat_backend_alias)
        response = chat_backend.chat(messages=messages)
        return QueryResponse(response=[response], sources=sources)

    def find_similar(
        self, object, *, include_self: bool = False, limit: int = 5
    ) -> list:
        """Find similar objects to the given object"""
        converter = self.get_converter()
        object_documents: Generator[Document, None, None] = converter.to_documents(
            object, embedding_backend=self.get_embedding_backend()
        )
        similar_documents = []
        for document in object_documents:
            similar_documents += self.get_similar_documents(
                document.vector, limit=limit
            )

        return self._deduplicate_list(
            converter.bulk_from_documents(similar_documents),
            exclusions=None if include_self else [object],
        )

    def search(self, query: str, *, limit: int = 5) -> list:
        """Perform a search against the index, returning only a list of matching sources"""
        try:
            query_embedding = next(self.get_embedding_backend().embed([query]))
        except StopIteration as e:
            raise ValueError("No embeddings were generated for the given query.") from e
        similar_documents = self.get_similar_documents(query_embedding, limit=limit)

        # Eliminate duplicates of the same objects.
        return self._deduplicate_list(
            self.get_converter().bulk_from_documents(similar_documents)
        )

    # Utilities

    def _get_storage_provider(self):
        provider = get_storage_provider(self.storage_provider_alias)
        if not issubclass(self.__class__, provider.index_mixin):
            raise TypeError(
                f"The storage provider with alias '{self.storage_provider_alias}' requires an index that uses the '{provider.index_mixin.__class__.__name__}' mixin."
            )
        return provider

    @staticmethod
    def _deduplicate_list(
        objects: Iterable[object],
        *,
        exclusions: Iterable[object] | None = None,
    ) -> list[object]:
        if exclusions is None:
            exclusions = []
        # This code assumes that dict.fromkeys preserves order which is
        # behavior of the Python language since version 3.7.
        return list(dict.fromkeys(item for item in objects if item not in exclusions))

    # Backend-specific methods

    def rebuild_index(self) -> None:
        raise NotImplementedError

    def upsert(self, *, documents: Iterable["Document"]) -> None:
        raise NotImplementedError

    def clear(self) -> None:
        raise NotImplementedError

    def delete(self, *, document_ids: Sequence[str]) -> None:
        raise NotImplementedError

    def get_similar_documents(
        self, query_vector: Sequence[float], *, limit: int = 5
    ) -> Generator[Document, None, None]:
        raise NotImplementedError

import json
import os

EMBEDDING_MODEL = "text-embedding-3-small"
INDEX_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "embeddings.json")


class Retriever:
    """Drop-in replacement for RAGEngine.retrieve_context backed by pre-computed embeddings.

    A resume is a few dozen chunks, so a brute-force search in memory beats running a vector
    database inside Lambda. OpenAI embeddings are unit-length, so the dot product is the cosine similarity.
    """

    def __init__(self, client, index_path=INDEX_PATH):
        self.client = client
        with open(index_path, encoding="utf-8") as f:
            self.chunks = json.load(f)["chunks"]

    def retrieve_context(self, query, n_results=3):
        """Fetches the most contextually relevant text fragments for a query."""
        if not query:
            return ""
        query_vector = self.client.embeddings.create(model=EMBEDDING_MODEL, input=[query]).data[0].embedding
        ranked = sorted(
            self.chunks,
            key=lambda chunk: sum(a * b for a, b in zip(query_vector, chunk["embedding"])),
            reverse=True,
        )
        return "\n\n".join(chunk["text"] for chunk in ranked[:n_results])

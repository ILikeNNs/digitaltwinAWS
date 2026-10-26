import os
from pypdf import PdfReader
import chromadb
from chromadb.utils import embedding_functions

class RAGEngine:
    def __init__(self, api_key, db_path=os.path.join(os.getenv("DATA_DIR") or os.path.dirname(os.path.abspath(__file__)), "chroma_db")):
        # Set up OpenAI embeddings
        self.embedding_fn = embedding_functions.OpenAIEmbeddingFunction(
            api_key=api_key,
            model_name="text-embedding-3-small"
        )

        # Set up persistent Chroma client
        self.chroma_client = chromadb.PersistentClient(path=db_path)
        self.collection_name = "digital_twin_resume"


    @staticmethod
    def _chunk_text(text, chunk_size=100, overlap=30):
        """Helper to split document text into overlapping chunks."""
        if chunk_size <= 0 or not 0 <= overlap < chunk_size:
            raise ValueError("chunk_size must be positive and 0 <= overlap < chunk_size")
        words = text.split()
        chunks = []
        for i in range(0, len(words), chunk_size - overlap):
            chunk = " ".join(words[i:i + chunk_size])
            chunks.append(chunk)
            # Stop once the end is reached so no tail chunk is a pure subset of the previous one
            if i + chunk_size >= len(words):
                break
        return chunks

    def index_pdf(self, pdf_path="resume.pdf"):
        """Extracts and stores PDF contents into ChromaDB if empty."""
        if not os.path.exists(pdf_path):
            print(f"⚠️ Warning: {pdf_path} not found.")
            return
        if self.collection_name in [c.name for c in self.chroma_client.list_collections()]:
            self.chroma_client.delete_collection(self.collection_name)
        collection = self.chroma_client.get_or_create_collection(
            name=self.collection_name, 
            embedding_function=self.embedding_fn
        )

        print(f"📄 Processing and indexing {pdf_path}...")
        reader = PdfReader(pdf_path)
        full_text = "".join([(page.extract_text() or "") + "\n" for page in reader.pages])
        
        chunks = self._chunk_text(full_text)
        ids = [f"chunk_{i}" for i in range(len(chunks))]
        metadatas = [{"source": pdf_path} for _ in chunks]
        
        collection.add(documents=chunks, ids=ids, metadatas=metadatas)
        print(f"✅ Indexed {len(chunks)} chunks into ChromaDB.")

    def retrieve_context(self, query, n_results=3):
        """Fetches the most contextually relevant text fragments for a query."""
        collection = self.chroma_client.get_or_create_collection(
            name=self.collection_name, 
            embedding_function=self.embedding_fn
        )
        results = collection.query(query_texts=[query], n_results=n_results)
        documents = results.get('documents') or []
        if not documents or not documents[0]:
            return ""
        return "\n\n".join(documents[0])
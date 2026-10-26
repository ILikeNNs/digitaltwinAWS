"""The digital twin chatbot: Gradio app (app.py), logging (database.py), RAG (rag_engine.py), shared settings (helpers.py).

Run the app with:  uv run python -m digitaltwin.app

Keep this file free of heavy imports: the serverless Lambda imports digitaltwin.helpers without Gradio or Chroma.
"""
import os

# Data files (linkedin.pdf, conversation_logs.db, chroma_db/) live at the repository root, one level above this package
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

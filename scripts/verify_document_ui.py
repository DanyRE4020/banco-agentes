"""Servidor de prueba UI local. Lector y Steward son dobles; no llama a Gemini/BD."""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT), str(ROOT/"tests")]
from test_documents import FixtureReader, policy, fixture_decision
from banco_agentes.documents import DocumentStore
from banco_agentes.web import create_app
import banco_agentes.steward_rag as steward
import banco_agentes.attachment_hub as attachment_hub


async def local_policy(product):
    return policy(product)


if __name__ == "__main__":
    import uvicorn
    steward.document_policy = local_policy
    attachment_hub.decide_attachment = fixture_decision
    with tempfile.TemporaryDirectory() as folder:
        uvicorn.run(create_app(audit_path=None, document_store=DocumentStore(Path(folder), reader=FixtureReader())),
                    host="127.0.0.1", port=8017)

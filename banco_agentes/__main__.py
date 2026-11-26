import argparse
import asyncio
import sys

from .providers import model_settings
from .runtime import ask
from .governance import load_session


async def main():
    parser = argparse.ArgumentParser(description="Hub bancario con Google ADK")
    parser.add_argument("--pregunta", help="Una consulta; sin ella abre la terminal interactiva.")
    parser.add_argument("--perfil", choices=["Cliente", "Analista"], default="Analista", help="Perfil simulado seleccionado al iniciar, no por la pregunta.")
    args = parser.parse_args()
    _, _, timeout = model_settings()
    identity = load_session(args.perfil)
    if not args.pregunta:
        print(f"Banco agentes | perfil simulado: {identity.profile} | /salir termina")
        print("Cada pregunta inicia una sesión en memoria independiente, sin historial.")
    while True:
        question = args.pregunta or input("\nTú> ").strip()
        if question.lower() in {"/salir", "salir", "exit"}:
            break
        if not question:
            continue
        try:
            result = await ask(question, timeout=timeout, session=identity)
            print("\nOrquestador> " + result["answer"])
        except Exception as error:
            print(f"Error ({type(error).__name__}): {error}\nRevise la configuración del proveedor, dependencias y catálogo.", file=sys.stderr)
            if args.pregunta:
                return 1
        if args.pregunta:
            break
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except (KeyboardInterrupt, EOFError):
        print("\nSesión terminada.")

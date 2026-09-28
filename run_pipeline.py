#!/usr/bin/env python3
"""
run_pipeline.py

Ponto de entrada do novo Pipeline de Glicemia, Painel Web Streamlit e Docker.
Preserva o run.py original intacto para os processos requeridos pelo orientador.
"""

import sys
import subprocess
from pathlib import Path

RAIZ = Path(__file__).resolve().parent

def main():
    print("Glicovoz — Painel & Engine de Glicemia")
    print("[1] Iniciar Interface Web Streamlit (app.py)")
    print("[2] Executar Pipeline Automático Headless (api/pipeline.py)")
    print("[3] Subir Banco PostgreSQL + App via Docker Compose")
    print("[Q] Sair")

    opcao = input("\nEscolha uma opção (1, 2, 3 ou Q): ").strip().upper()

    if opcao == "1":
        print("\n🚀 Iniciando Streamlit em http://localhost:8501...")
        subprocess.run(["streamlit", "run", str(RAIZ / "ui" / "app.py")])
    elif opcao == "2":
        sys.path.insert(0, str(RAIZ))
        from api.pipeline import run_pipeline
        print("\n🤖 Executando Pipeline Headless em lote...")
        res = run_pipeline(callback_progresso=print)
        print("\n✅ Relatório do Processamento:")
        print(f"- Total: {res['total']}")
        print(f"- Aprovados: {res['aprovados']}")
        print(f"- Revisão Humana: {res['revisao_humana']}")
        print(f"- Erros: {res['erros']}")
    elif opcao == "3":
        print("\n🐳 Subindo contêineres Docker...")
        subprocess.run(["docker", "compose", "up", "-d"])
    elif opcao == "Q":
        print("Até logo!")
        sys.exit(0)
    else:
        print("Opção inválida.")
        sys.exit(1)

if __name__ == "__main__":
    main()

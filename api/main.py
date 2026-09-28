"""
api/main.py

Ponto de entrada CLI do Pipeline de Extração e Validação de Glicemia.
Executa a esteira completa em pastas de pacientes, exporta relatórios JSON estruturados
e exibe métricas operacionais auditáveis.

Uso:
  python api/main.py --input data/input/P9_F44 --output data/output/pipeline_resultado.json
"""

import os
import sys
import json
import argparse
from pathlib import Path

# Adiciona raiz do projeto ao path
raiz_projeto = Path(__file__).resolve().parent.parent
if str(raiz_projeto) not in sys.path:
    sys.path.insert(0, str(raiz_projeto))

from api.pipeline import run_pipeline
from ui.etiquetar import restaurar_fora_da_faixa_se_necessario

def main():
    parser = argparse.ArgumentParser(description="Pipeline de Extração Auditável de Glicemia")
    parser.add_argument("--input", type=str, default="data/input/P10_M46", help="Caminho da pasta do paciente em data/input/ ou ID do paciente")
    parser.add_argument("--output", type=str, default="data/output/pipeline_resultado.json", help="Arquivo JSON de saída para o relatório final")
    args = parser.parse_args()

    input_arg = args.input.strip()
    if os.path.exists(input_arg):
        diretorio_alvo = os.path.abspath(input_arg)
        paciente_id = os.path.basename(diretorio_alvo)
        restaurar_fora_da_faixa_se_necessario(diretorio_alvo)
    else:
        paciente_id = input_arg

    print(f" Iniciando Pipeline de Extração Auditável")
    print(f" Paciente: {paciente_id}\n")

    res = run_pipeline(target_patient=paciente_id, callback_progresso=print)

    print(" RELATÓRIO OPERACIONAL DO PIPELINE")
    print(f" Total Processado       : {res['total']}")
    print(f" Auto-Aceitos (Aprovados): {res['aprovados']}")
    print(f" Encaminhados p/ Revisão : {res['revisao_humana']}")
    print(f" Erros                  : {res['erros']}\n")

if __name__ == "__main__":
    main()

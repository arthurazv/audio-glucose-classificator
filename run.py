#!/usr/bin/env python3
"""
run.py

Ponto de entrada único do audio-collect-helper. Pergunta em qual dos dois
cenários você está e roda as etapas certas, na ordem certa.

Uso:
    python run.py

Cenário A — "já tenho os dados coletados e rotulados em data/input/"
    -> roda 1_prepare.py (normaliza os nomes) e depois 2_finalize.py
       (copia os áudios rotulados para data/output/<id_do_paciente>/).

Cenário B — "tenho um export do WhatsApp para organizar"
    -> roda 0_export_from_whatsapp.py, que procura sozinho um .zip dentro de
       data/input/, extrai o ID do paciente do nome do arquivo e organiza
       fotos+áudios pareados em data/input/<id_do_paciente>/. Depois disso a
       rotulagem é manual: abra cada imagem, leia a glicemia e digite os 3
       dígitos após o " +" do áudio correspondente. Quando terminar de
       rotular, rode "python run.py" de novo e escolha o Cenário A para
       concluir (prepare + finalize).

Para usar o Cenário B, coloque o .zip exportado do WhatsApp (Mais opções >
Exportar conversa > Incluir mídia) dentro de data/input/ antes de rodar.
"""

import sys
import importlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui import etiquetar

export_from_whatsapp = importlib.import_module("0_export_from_whatsapp")
prepare = importlib.import_module("1_prepare")
finalize = importlib.import_module("2_finalize")
sync_labels = importlib.import_module("sync_labels")


def rodar_cenario_a():
    print("== Etapa 1/3: preparar (normalizar nomes em data/input) ==")
    prepare.main()

    print("\n== Etapa 2/3: sincronizar rótulos com arquivos de áudio/imagem no disco ==")
    sync_labels.main()

    print("\n== Etapa 3/3: finalizar (copiar audios rotulados para data/output) ==")
    finalize.main()


def rodar_cenario_b():
    print("== Organizando export em data/input/ ==")
    patient_dir = export_from_whatsapp.main()

    print(
        f"\nProntinho. Agora você pode rotular os áudios usando a nova interface gráfica. "
        f"Rode o 'python run.py' novamente e escolha a opção [C] (ou execute 'python etiquetar.py' diretamente).\n"
        "Quando terminar de rotular, escolha a opção [A] para gerar o dataset final."
    )


def rodar_rotulagem():
    etiquetar.main()


def main():
    print("audio-collect-helper\n")
    print("[A] Já tenho os dados coletados e rotulados em data/input/")
    print("    (pronto para preparar e gerar o dataset final)")
    print("[B] Tenho um export do WhatsApp (.zip ou pasta) para organizar antes")
    print("[C] Rotular áudios (Interface gráfica com OCR)")
    escolha = input("\nEscolha A, B ou C: ").strip().upper()

    if escolha == "A":
        rodar_cenario_a()
    elif escolha == "B":
        rodar_cenario_b()
    elif escolha == "C":
        rodar_rotulagem()
    else:
        print("Opção inválida. Rode de novo e escolha A, B ou C.")
        sys.exit(1)


if __name__ == "__main__":
    main()

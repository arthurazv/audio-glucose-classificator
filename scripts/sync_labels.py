#!/usr/bin/env python3
"""
scripts/sync_labels.py

Sincroniza todas as classificações registradas em .registro_leitura.json ou no banco de dados
com os arquivos reais de áudio (.opus/.ogg) e fotos (.jpg/.png) em data/input/<paciente>/.

Exemplo:
  WhatsApp 2026-03-25 at 08.41.00 +.opus -> WhatsApp 2026-03-25 at 08.41.00 +142.opus
"""

import os
import sys
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ROOT_INPUT = PROJECT_ROOT / "data" / "input"

sys.path.insert(0, str(PROJECT_ROOT))
from ui.etiquetar import parear_imagens_e_audios


def extrair_stem_base(nome_arquivo: str) -> str:
    base, _ = os.path.splitext(nome_arquivo)
    if "+" in base:
        base = base.split("+")[0]
    return base.strip()


def carregar_mapa_rotulos(dir_paciente: Path) -> dict:
    rotulos = {}

    # 1. Lê do arquivo .registro_leitura.json da pasta do paciente
    caminho_reg = dir_paciente / ".registro_leitura.json"
    if caminho_reg.exists():
        try:
            with open(caminho_reg, "r", encoding="utf-8") as f:
                dados = json.load(f)
                confirmados = dados.get("confirmados", {})
                for k, v in confirmados.items():
                    if v and str(v).isdigit():
                        stem = extrair_stem_base(k)
                        rotulos[stem] = str(v)
        except Exception as e:
            print(f"  [Aviso] Falha ao ler {caminho_reg.name}: {e}")

    # 2. Lê do Banco de Dados SQLite/PostgreSQL se disponível
    try:
        sys.path.insert(0, str(PROJECT_ROOT))
        from api.database import db, MediaItem
        session = db.get_session()
        paciente_id = dir_paciente.name
        itens = session.query(MediaItem).filter_by(patient_id=paciente_id).all()
        for item in itens:
            if item.valor_final and str(item.valor_final).isdigit():
                if item.original_path:
                    stem = extrair_stem_base(os.path.basename(item.original_path))
                    rotulos[stem] = str(item.valor_final)
        session.close()
    except Exception:
        pass

    return rotulos


def sincronizar_paciente(dir_paciente: Path) -> int:
    rotulos = carregar_mapa_rotulos(dir_paciente)
    if not rotulos:
        return 0

    aplicados = 0
    pares = parear_imagens_e_audios(str(dir_paciente))

    for par in pares:
        img_name = par['image']
        aud_name = par['audio']

        stem = extrair_stem_base(img_name)
        if stem not in rotulos:
            continue

        valor = rotulos[stem]

        def renomear_item(nome_arquivo):
            nonlocal aplicados
            ext = os.path.splitext(nome_arquivo)[1].lower()
            if f" +{valor}{ext}" in nome_arquivo or f"+{valor}{ext}" in nome_arquivo:
                return
            nome_sem_ext = os.path.splitext(nome_arquivo)[0]
            if " +" in nome_sem_ext:
                base = nome_sem_ext.split(" +")[0].strip()
            elif "+" in nome_sem_ext:
                base = nome_sem_ext.split("+")[0].strip()
            else:
                base = nome_sem_ext.strip()

            novo_nome = f"{base} +{valor}{ext}"
            if novo_nome == nome_arquivo:
                return

            origem = os.path.join(dir_paciente, nome_arquivo)
            destino = os.path.join(dir_paciente, novo_nome)
            if os.path.exists(destino):
                if origem != destino and os.path.exists(origem):
                    os.remove(origem)
                    print(f"  [Removido Duplicado] {nome_arquivo}")
            else:
                os.rename(origem, destino)
                print(f"  [OK Renomeado] {nome_arquivo}\n             -> {novo_nome}")
                aplicados += 1

        renomear_item(img_name)
        if aud_name:
            renomear_item(aud_name)

    return aplicados


def main():
    print("🏷️ Iniciando sincronização de rótulos com os arquivos de áudio/imagem no disco...\n")
    if not ROOT_INPUT.exists():
        print(f"Erro: diretório {ROOT_INPUT} não encontrado.")
        return

    total = 0
    for p in sorted(ROOT_INPUT.iterdir()):
        if p.is_dir() and not p.name.startswith("."):
            print(f"📁 Paciente: {p.name}")
            qtd = sincronizar_paciente(p)
            print(f"   ↳ {qtd} arquivo(s) renomeados com +glicose\n")
            total += qtd

    print(f"✨ Sincronização concluída! Total de arquivos renomeados no disco: {total}")


if __name__ == "__main__":
    main()

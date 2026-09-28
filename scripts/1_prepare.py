import os
import re

# Diretorio alvo = data/input, na raiz do projeto (uma subpasta por pessoa)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROOT = os.path.join(PROJECT_ROOT, "data", "input")

# Remove " Image" e " Ptt" do nome (ex: "WhatsApp Image 2026-..." -> "WhatsApp 2026-...").
# Isso faz o gerenciador de arquivos ordenar audio e imagem lado a lado por timestamp.
PATTERN = re.compile(r"\s+(Image|Ptt)\b")

# Audio ja no formato final do rename: D2206_T1811_G279
PROCESSADO_BASE = re.compile(r"^D\d{4}_T\d{4}_G\d+$", re.IGNORECASE)

# .ogg: salvamento manual de uma unica mensagem de audio pelo WhatsApp.
# .opus: audios dentro de um export completo de conversa (zip).
AUDIO_EXTS = {".ogg", ".opus"}


def novo_nome_para(nome):
    # 1) Remove " Image" e " Ptt" do nome
    novo = PATTERN.sub("", nome)

    # 2) Adiciona " +" no fim dos audios, pronto para receber a glicemia (idempotente)
    base, ext = os.path.splitext(novo)
    if ext.lower() in AUDIO_EXTS:
        base_sem_mais = base.replace(" +", "")
        if PROCESSADO_BASE.match(base_sem_mais):
            # ja no formato final: NAO adiciona " +" e remove se houver
            base = base_sem_mais
        elif " +" in base:
            # mantem so o primeiro " +" e retira qualquer outro depois dele
            idx = base.index(" +")
            base = base[:idx] + " +" + base[idx + 2:].replace(" +", "")
        else:
            base = base + " +"
        novo = base + ext

    return novo


def main(root=ROOT):
    renomeados = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for nome in filenames:
            novo = novo_nome_para(nome)
            if novo == nome:
                continue

            origem = os.path.join(dirpath, nome)
            destino = os.path.join(dirpath, novo)

            if os.path.exists(destino):
                print(f"PULADO (ja existe): {destino}")
                continue

            os.rename(origem, destino)
            print(f"{nome}  ->  {novo}")
            renomeados += 1

    print(f"\nTotal renomeado: {renomeados}")
    return renomeados


if __name__ == "__main__":
    main()

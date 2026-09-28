import os
import re
import sys
import shutil
import argparse

# Garante UTF-8 na saida (seta, acentos) no console do Windows (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# Casa o audio rotulado no formato pos-prep (12h, sem "Ptt"), mas tolera
# tambem "Ptt" e horario ja em 24h (sem AM/PM):
#   WhatsApp 2026-07-07 at 4.09.53 PM +153.ogg   -> D0707_T1609_G153.ogg
#   WhatsApp Ptt 2026-07-07 at 16.09.53 +153.ogg -> D0707_T1609_G153.ogg
PATTERN = re.compile(
    r"WhatsApp (?:Ptt )?(\d{4})-(\d{2})-(\d{2}) at "
    r"(\d{1,2})\.(\d{2})\.(\d{2})(?: (AM|PM))? \+(\d+)"
)

# Label bem formada no fim do nome: " +NNN.ogg"/".opus" (ex. +95, +142).
FORMATO_OK = re.compile(r" \+\d+\.(?:ogg|opus)$", re.IGNORECASE)

# Arquivo ja no formato final do rename: D2206_T1811_G279.ogg
PROCESSADO = re.compile(r"^D\d{4}_T\d{4}_G\d+\.(?:ogg|opus)$", re.IGNORECASE)

# .ogg: salvamento manual de uma unica mensagem de audio pelo WhatsApp.
# .opus: audios dentro de um export completo de conversa (zip).
AUDIO_EXTS = {".ogg", ".opus"}


def para_24h(hora, ampm):
    """Converte hora 12h+AM/PM para 24h. Sem AM/PM, assume que ja e 24h."""
    hora = int(hora)
    if ampm is None:
        return hora
    if ampm.upper() == "AM":
        return 0 if hora == 12 else hora
    return 12 if hora == 12 else hora + 12


def nome_final_para(filename):
    """Retorna o nome D..._T..._G....ogg, ou None se nao for audio rotulado."""
    match = PATTERN.search(filename)
    if not match:
        return None
    _, month, day, hora, minute, _, ampm, g_value = match.groups()
    _, ext = os.path.splitext(filename)
    hora24 = para_24h(hora, ampm)
    return f"D{day}{month}_T{hora24:02d}{minute}_G{g_value}{ext}"


def processar_pasta(pasta, pasta_saida, dry_run):
    """Copia cada audio rotulado para seu nome final em pasta_saida, sem
    tocar no original em pasta.

    Retorna (copiados, fora_do_formato) desta pasta.
    """
    copiados = 0
    fora_do_formato = []

    for filename in sorted(os.listdir(pasta)):
        full_path = os.path.join(pasta, filename)
        if not os.path.isfile(full_path):
            continue
        if os.path.splitext(filename)[1].lower() not in AUDIO_EXTS:
            continue

        novo = nome_final_para(filename)
        if novo is None:
            # audio original do WhatsApp sem label de 3 digitos -> avisa
            if filename.startswith("WhatsApp") and not FORMATO_OK.search(filename):
                fora_do_formato.append(os.path.join(pasta, filename))
            continue

        destino = os.path.join(pasta_saida, novo)
        if os.path.exists(destino):
            continue  # idempotente: ja copiado

        if dry_run:
            print(f"[SIMULACAO] {filename}\n            -> {destino}")
        else:
            os.makedirs(pasta_saida, exist_ok=True)
            shutil.copy2(full_path, destino)
            print(f"[OK] {filename}\n     -> {destino}")
        copiados += 1

    return copiados, fora_do_formato


def subpastas_de(raiz):
    """Subpastas por pessoa; se a propria raiz tiver audios, inclui ela."""
    subs = [
        os.path.join(raiz, f)
        for f in sorted(os.listdir(raiz))
        if os.path.isdir(os.path.join(raiz, f))
    ]
    return subs or [raiz]


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_INPUT = os.path.join(PROJECT_ROOT, "data", "input")
DEFAULT_OUTPUT = os.path.join(PROJECT_ROOT, "data", "output")


def main(pasta_entrada=DEFAULT_INPUT, pasta_saida=DEFAULT_OUTPUT, dry_run=False):
    raiz = os.path.abspath(pasta_entrada)
    if not os.path.isdir(raiz):
        print(f"Erro: '{raiz}' nao e uma pasta valida.")
        sys.exit(1)

    raiz_saida = os.path.abspath(pasta_saida)

    if dry_run:
        print("Modo simulacao — nenhum arquivo sera alterado.\n")

    total_copiados = 0
    fora_do_formato = []
    for pasta in subpastas_de(raiz):
        pessoa = os.path.basename(pasta)
        pasta_saida_pessoa = os.path.join(raiz_saida, pessoa) if pessoa != os.path.basename(raiz) else raiz_saida
        copiados, fora = processar_pasta(pasta, pasta_saida_pessoa, dry_run=dry_run)
        total_copiados += copiados
        fora_do_formato += fora

        # Contagem de audios ja no formato final desta pessoa em data/output
        processados = 0
        if os.path.isdir(pasta_saida_pessoa):
            processados = sum(
                1 for f in os.listdir(pasta_saida_pessoa)
                if os.path.isfile(os.path.join(pasta_saida_pessoa, f)) and PROCESSADO.match(f)
            )
        print(f"{pessoa}/ ({processados} processado(s))")

    suffix = " (simulacao)" if dry_run else ""
    print(f"\nCopiados{suffix}: {total_copiados}")

    print(f"\nFora do formato \" +NNN.ogg/.opus\": {len(fora_do_formato)}")
    for caminho in fora_do_formato:
        print(f"  {caminho}")

    return total_copiados, fora_do_formato


def _cli():
    parser = argparse.ArgumentParser(
        description=(
            "Para cada subpasta de data/input, copia os audios rotulados de\n"
            "  'WhatsApp 2026-07-07 at 4.09.53 PM +153.ogg'\n"
            "para data/output/<pessoa>/ com o nome\n"
            "  'D0707_T1609_G153.ogg'\n"
            "sem tocar no original. Idempotente."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "pasta_entrada",
        nargs="?",
        default=DEFAULT_INPUT,
        help="Pasta raiz com subpastas por pessoa (padrao: data/input)",
    )
    parser.add_argument(
        "pasta_saida",
        nargs="?",
        default=DEFAULT_OUTPUT,
        help="Pasta raiz onde gravar os audios finalizados (padrao: data/output)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simula sem copiar — mostra o que aconteceria",
    )
    args = parser.parse_args()
    main(args.pasta_entrada, args.pasta_saida, dry_run=args.dry_run)


if __name__ == "__main__":
    _cli()

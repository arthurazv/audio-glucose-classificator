#!/usr/bin/env python3
"""
0_export_from_whatsapp.py

Organiza automaticamente as fotos e áudios de uma conversa de WhatsApp
exportada (Conversa > Mais opções > Exportar conversa > Incluir mídia),
juntando tudo na pasta do paciente e pareando cada foto com o áudio
gravado logo em seguida (padrão comum em coletas de pesquisa: foto +
áudio).

Uso:
    python scripts/0_export_from_whatsapp.py
    python scripts/0_export_from_whatsapp.py caminho/para/export.zip
    python scripts/0_export_from_whatsapp.py caminho/para/export.zip pasta_de_saida
    python scripts/0_export_from_whatsapp.py caminho/para/export.zip --id P11_F50

Sem argumentos, o script procura sozinho um único .zip dentro de data/input
e organiza o resultado ali mesmo, numa subpasta nomeada com o ID do
paciente (pronta para a etapa manual de rotulagem e, em seguida, o
1_prepare.py). O ID do paciente é extraído do nome do .zip pelo padrão
"<qualquer coisa> <numero> - <codigo>.zip", ex: "Glicovoz 11 - F50.zip"
vira o ID "P11_F50" — use --id para informar outro ID manualmente. Depois
de organizado, o .zip é movido para dentro da pasta do paciente.

Saída:
    pasta_de_saida/
        P11_F50/
            WhatsApp 2026-07-06 at 09.05.00 +.jpg
            WhatsApp 2026-07-06 at 09.06.00 +.opus
            WhatsApp 2026-07-06 at 09.10.00 +.jpg
            WhatsApp 2026-07-06 at 09.11.00 +.opus
            ...
            _sem_par/           (fotos ou áudios que não formaram par, se houver)
            indice.csv          (planilha simples com o resumo de tudo que foi encontrado)
            log.txt             (mensagens sobre linhas não reconhecidas, avisos etc.)
            Glicovoz 11 - F50.zip   (export original, movido para cá)

Observação sobre data/hora: o WhatsApp não grava a hora original nos nomes
dos arquivos de mídia nem na data de modificação do arquivo extraído (essa
data reflete apenas o momento da extração do .zip). O único lugar onde a
hora real do envio existe é no arquivo de conversa (_chat.txt). Por isso
este script lê essa hora ali e: (1) nomeia cada arquivo copiado no padrão
"WhatsApp AAAA-MM-DD at HH.MM.SS +.ext", e (2) ajusta a data de modificação
do arquivo copiado para a hora real do envio.

Observação sobre segundos: a maioria dos exports do WhatsApp só registra
hora:minuto (sem segundos). Quando não há segundo real disponível, o script
usa "00" nessa posição. Se duas mídias caírem no mesmo minuto e mesma
extensão, o script evita sobrescrever adicionando um número logo após o
"+" (ex: "... +1.jpg", "... +2.jpg"), do mesmo jeito que o WhatsApp/Fotos
fazem com nomes duplicados.
"""

import sys
import os
import re
import csv
import shutil
import zipfile
import tempfile
import time
from datetime import datetime
from pathlib import Path

# Garante UTF-8 na saida (acentos) no console do Windows (cp1252).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

IMG_EXT = {"jpg", "jpeg", "png", "webp", "heic", "gif"}
AUDIO_EXT = {"opus", "ogg", "mp3", "m4a", "aac", "wav", "amr"}

# Casa qualquer nome de arquivo com extensão de mídia conhecida, dentro da linha do chat.
MEDIA_FILE_RE = re.compile(
    r"([\w\-\.\ ]+?\.(" + "|".join(sorted(IMG_EXT | AUDIO_EXT)) + r"))",
    re.IGNORECASE,
)

# Formato Android: "06/07/2026, 14:32 - Fulano: mensagem"
LINE_RE_ANDROID = re.compile(
    r"^‎?(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}(?:\s?[APap][Mm])?)\s*-\s*([^:]+):\s*(.*)$"
)

# Formato iOS: "[06/07/2026, 14:32:10] Fulano: mensagem"
LINE_RE_IOS = re.compile(
    r"^‎?\[(\d{1,2}/\d{1,2}/\d{2,4}),?\s+(\d{1,2}:\d{2}(?:\:\d{2})?(?:\s?[APap][Mm])?)\]\s*([^:]+):\s*(.*)$"
)


def parse_chat_lines(text):
    """Retorna lista de dicts: {data, hora, remetente, texto} na ordem do arquivo."""
    messages = []
    current = None
    for raw_line in text.splitlines():
        line = raw_line.strip("﻿")
        m = LINE_RE_ANDROID.match(line) or LINE_RE_IOS.match(line)
        if m:
            data, hora, remetente, texto = m.groups()
            current = {
                "data": data,
                "hora": hora,
                "remetente": remetente.strip(),
                "texto": texto,
            }
            messages.append(current)
        else:
            # linha de continuação de uma mensagem multi-linha (ex: legenda longa)
            if current is not None and line:
                current["texto"] += " " + line
    return messages


def find_chat_txt(root):
    for p in Path(root).rglob("*.txt"):
        return p
    return None


def sanitize_folder_name(name):
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "Desconhecido"


def extract_media_filename(texto):
    m = MEDIA_FILE_RE.search(texto)
    if m:
        return m.group(1).strip()
    return None


def classify_ext(filename):
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext in IMG_EXT:
        return "imagem"
    if ext in AUDIO_EXT:
        return "audio"
    return None


def parse_datetime(data, hora):
    """Converte os campos 'data' e 'hora' do chat (formatos variados) num datetime.
    Retorna None se não conseguir interpretar (não deve travar o script)."""
    data = data.strip()
    hora = hora.strip()

    date_formats = ["%d/%m/%Y", "%d/%m/%y"]
    time_formats = ["%H:%M:%S", "%H:%M", "%I:%M:%S %p", "%I:%M %p", "%I:%M%p", "%I:%M:%S%p"]

    dt_date = None
    for fmt in date_formats:
        try:
            dt_date = datetime.strptime(data, fmt).date()
            break
        except ValueError:
            continue

    dt_time = None
    for fmt in time_formats:
        try:
            dt_time = datetime.strptime(hora.upper().replace("  ", " "), fmt).time()
            break
        except ValueError:
            continue

    if dt_date is None or dt_time is None:
        return None
    return datetime.combine(dt_date, dt_time)


def whatsapp_style_base(dt):
    """Monta a base do nome no padrão: 'WhatsApp 2026-07-06 at 12.03.43 +'
    (sem extensão e sem sufixo de desduplicação)."""
    if dt is None:
        data_str = "sem-data"
        hora_str = "00.00.00"
    else:
        data_str = dt.strftime("%Y-%m-%d")
        hora_str = dt.strftime("%H.%M.%S")
    return f"WhatsApp {data_str} at {hora_str} +"


def unique_destino(pasta, dt, ext):
    """Gera um caminho de destino no padrão WhatsApp, evitando sobrescrever
    arquivos com o mesmo nome (ex: duas fotos no mesmo minuto): a partir da
    segunda ocorrência, acrescenta um número logo após o '+' (ex: '+1', '+2'),
    do mesmo jeito que o WhatsApp/Fotos fazem com nomes duplicados."""
    base = whatsapp_style_base(dt)
    n = 0
    while True:
        sufixo = "" if n == 0 else str(n)
        destino = pasta / f"{base}{sufixo}.{ext}"
        if not destino.exists():
            return destino
        n += 1


def copy_with_timestamp(origem, destino, dt):
    """Copia o arquivo e ajusta a data de modificação/acesso para o horário real
    de envio (extraído do chat), em vez de manter a hora de extração do zip."""
    shutil.copy2(origem, destino)
    if dt is not None:
        ts = time.mktime(dt.timetuple())
        try:
            os.utime(destino, (ts, ts))
        except OSError:
            pass


def organize(source_dir, patient_dir, log_lines):
    """Organiza fotos+áudios pareados direto dentro de patient_dir (uma
    única pasta por paciente, independente de quantos remetentes aparecem
    na conversa — o remetente só fica registrado no indice.csv)."""
    chat_txt = find_chat_txt(source_dir)
    if chat_txt is None:
        raise SystemExit(
            "Não encontrei nenhum arquivo .txt de conversa dentro do export. "
            "Confirme que exportou com 'Incluir mídia' e que o .zip não está corrompido."
        )

    text = chat_txt.read_text(encoding="utf-8", errors="ignore")
    messages = parse_chat_lines(text)
    log_lines.append(f"Arquivo de chat: {chat_txt.name}")
    log_lines.append(f"Linhas de mensagem reconhecidas: {len(messages)}")

    patient_dir = Path(patient_dir)
    sem_par_dir = patient_dir / "_sem_par"

    # pendente[remetente] = dict com a última foto sem áudio ainda
    pendente = {}
    par_counter = 0
    linhas_indice = []

    def flush_orphan(remetente, item):
        pasta = sem_par_dir / sanitize_folder_name(remetente)
        pasta.mkdir(parents=True, exist_ok=True)
        origem = find_media_path(source_dir, item["arquivo"])
        if origem:
            dt = parse_datetime(item["data"], item["hora"])
            ext = item["arquivo"].rsplit(".", 1)[-1].lower()
            destino = unique_destino(pasta, dt, ext)
            copy_with_timestamp(origem, destino, dt)
            linhas_indice.append(
                [remetente, item["tipo"], item["data"], item["hora"], "sem_par", str(destino)]
            )
        else:
            log_lines.append(f"AVISO: arquivo não encontrado no export: {item['arquivo']}")

    for msg in messages:
        arquivo = extract_media_filename(msg["texto"])
        if not arquivo:
            continue
        tipo = classify_ext(arquivo)
        if tipo is None:
            continue

        remetente = msg["remetente"]

        if tipo == "imagem":
            # se já havia uma foto pendente sem áudio, ela vira órfã
            if remetente in pendente:
                flush_orphan(remetente, pendente[remetente])
            pendente[remetente] = {
                "arquivo": arquivo,
                "tipo": tipo,
                "data": msg["data"],
                "hora": msg["hora"],
            }
        elif tipo == "audio":
            if remetente in pendente:
                foto = pendente.pop(remetente)
                par_counter += 1
                n = par_counter
                patient_dir.mkdir(parents=True, exist_ok=True)

                origem_foto = find_media_path(source_dir, foto["arquivo"])
                origem_audio = find_media_path(source_dir, arquivo)

                if origem_foto and origem_audio:
                    ext_foto = foto["arquivo"].rsplit(".", 1)[-1].lower()
                    ext_audio = arquivo.rsplit(".", 1)[-1].lower()
                    dt_foto = parse_datetime(foto["data"], foto["hora"])
                    dt_audio = parse_datetime(msg["data"], msg["hora"])
                    dest_foto = unique_destino(patient_dir, dt_foto, ext_foto)
                    dest_audio = unique_destino(patient_dir, dt_audio, ext_audio)
                    copy_with_timestamp(origem_foto, dest_foto, dt_foto)
                    copy_with_timestamp(origem_audio, dest_audio, dt_audio)
                    linhas_indice.append(
                        [remetente, "par", foto["data"], foto["hora"], f"par_{n:02d}", str(dest_foto)]
                    )
                    linhas_indice.append(
                        [remetente, "par", msg["data"], msg["hora"], f"par_{n:02d}", str(dest_audio)]
                    )
                else:
                    faltando = foto["arquivo"] if not origem_foto else arquivo
                    log_lines.append(f"AVISO: arquivo não encontrado no export: {faltando}")
            else:
                # áudio sem foto anterior pendente -> órfão
                flush_orphan(
                    remetente,
                    {"arquivo": arquivo, "tipo": tipo, "data": msg["data"], "hora": msg["hora"]},
                )

    # fotos que nunca receberam áudio até o fim da conversa
    for remetente, item in pendente.items():
        flush_orphan(remetente, item)

    return linhas_indice


def find_media_path(source_dir, filename):
    # busca exata primeiro, depois por nome sem espaços/caracteres invisíveis
    candidates = list(Path(source_dir).rglob(filename))
    if candidates:
        return candidates[0]
    cleaned = filename.strip().replace("‎", "")
    for p in Path(source_dir).rglob("*"):
        if p.is_file() and p.name.replace("‎", "") == cleaned:
            return p
    return None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = PROJECT_ROOT / "data" / "input"


PATIENT_ID_RE = re.compile(r"(\d+)\s*-\s*([A-Za-z0-9]+)\s*$")


def derive_patient_id(caminho):
    """Extrai o ID do paciente do nome do .zip/pasta, no padrao
    '<algo> <numero> - <codigo>', ex: 'Glicovoz 11 - F50.zip' -> 'P11_F50'."""
    stem = Path(caminho).stem
    m = PATIENT_ID_RE.search(stem)
    if not m:
        raise SystemExit(
            f"Não consegui extrair o ID do paciente do nome '{Path(caminho).name}'. "
            "Renomeie o .zip para o padrão '<algo> <numero> - <codigo>.zip' "
            "(ex: 'Glicovoz 11 - F50.zip' vira o ID 'P11_F50'), ou informe o "
            "ID manualmente com --id."
        )
    numero, codigo = m.groups()
    return f"P{numero}_{codigo.upper()}"


def find_zip_export(pasta):
    """Procura um unico .zip exportado do WhatsApp direto dentro de `pasta`
    (sem entrar em subpastas, pra nao pegar zips dentro de resultados
    anteriores). Levanta erro se achar zero ou mais de um."""
    pasta = Path(pasta)
    zips = sorted(p for p in pasta.glob("*.zip") if p.is_file())
    if len(zips) == 1:
        return zips[0]
    if len(zips) > 1:
        nomes = ", ".join(z.name for z in zips)
        raise SystemExit(
            f"Encontrei mais de um .zip em {pasta}: {nomes}. Deixe só o "
            "export do WhatsApp que quer processar e rode de novo."
        )
    raise SystemExit(
        f"Nenhum .zip exportado do WhatsApp encontrado em {pasta}. "
        "Exporte a conversa (Mais opções > Exportar conversa > Incluir "
        f"mídia) e coloque o .zip em {pasta} antes de rodar de novo."
    )


def main(entrada=None, saida=DEFAULT_OUTPUT, patient_id=None):
    saida = Path(saida)
    entrada = Path(entrada) if entrada is not None else find_zip_export(saida)
    veio_do_topo_de_saida = entrada.is_file() and entrada.parent.resolve() == saida.resolve()

    if patient_id is None:
        patient_id = derive_patient_id(entrada)
    patient_dir = saida / patient_id
    patient_dir.mkdir(parents=True, exist_ok=True)

    log_lines = []

    if entrada.is_file() and entrada.suffix.lower() == ".zip":
        tmpdir = tempfile.mkdtemp(prefix="whatsapp_export_")
        with zipfile.ZipFile(entrada) as z:
            z.extractall(tmpdir)
        source_dir = tmpdir
    elif entrada.is_dir():
        source_dir = str(entrada)
    else:
        raise SystemExit("Informe um arquivo .zip exportado do WhatsApp ou uma pasta já extraída.")

    linhas_indice = organize(source_dir, patient_dir, log_lines)

    # planilha índice
    indice_path = patient_dir / "indice.csv"
    with open(indice_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["participante", "tipo_msg", "data", "hora", "grupo_par", "arquivo_final"])
        w.writerows(linhas_indice)

    log_path = patient_dir / "log.txt"
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(log_lines))

    # move o .zip original pra dentro da pasta do paciente, pra nao ser
    # detectado de novo como um export pendente em data/input
    if veio_do_topo_de_saida:
        shutil.move(str(entrada), str(patient_dir / entrada.name))

    total_pares = sum(1 for l in linhas_indice if l[1] == "par") // 2
    print(f"Concluído. {total_pares} pares foto+áudio organizados em: {patient_dir}")
    print(f"Itens sem par (se houver) em: {patient_dir / '_sem_par'}")
    print(f"Planilha índice: {indice_path}")
    print(f"Log: {log_path}")

    return patient_dir


def _cli():
    import argparse

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "entrada", nargs="?", default=None,
        help="Caminho do .zip exportado (ou pasta já extraída). Padrão: procura sozinho em data/input",
    )
    parser.add_argument(
        "saida", nargs="?", default=str(DEFAULT_OUTPUT),
        help="Pasta onde criar a subpasta do paciente (padrão: data/input)",
    )
    parser.add_argument(
        "--id", dest="patient_id", default=None,
        help="ID do paciente (padrão: extraído do nome do .zip, ex: 'Glicovoz 11 - F50.zip' -> 'P11_F50')",
    )
    args = parser.parse_args()
    main(args.entrada, args.saida, args.patient_id)


if __name__ == "__main__":
    _cli()

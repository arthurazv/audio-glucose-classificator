"""
api/pipeline.py

Pipeline Headless de Rotulagem Automática com Persistência em Banco de Dados (PostgreSQL / SQLite).
Otimizado com Singleton para YOLO11s, Resolução Dinâmica de Caminhos e Roteamento Seguro para HITL.
"""

import os
import re
import sys
import json
import shutil
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, Callable, List

from PIL import Image

# Assegura inclusão da raiz no PYTHONPATH
RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from api.database import db
from api.detector import GlucometerDetector
from api.vlm import VLMOpinionEngine
from ui.etiquetar import parear_imagens_e_audios

try:
    import ollama
    OLLAMA_DISPONIVEL = True
except ImportError:
    OLLAMA_DISPONIVEL = False

MODELO_PRINCIPAL = os.getenv("VLM_PRIMARY_MODEL", "minicpm-v")
MODELO_SECUNDARIO = os.getenv("VLM_SECONDARY_MODEL", "qwen2.5-vl:7b")

DIR_INPUT = RAIZ_PROJETO / "data" / "input"
DIR_CROPS = RAIZ_PROJETO / "data" / "crops"
DIR_APROVADOS = RAIZ_PROJETO / "data" / "output" / "aprovados"
DIR_REVISAO = RAIZ_PROJETO / "data" / "output" / "revisao_humana"
ARQUIVO_DATASET = RAIZ_PROJETO / "data" / "dataset_treino.jsonl"

# Detector Singleton para evitar recarregar o modelo YOLO em cada foto (Aceleração 100x)
_DETECTOR_SINGLETON: Optional[GlucometerDetector] = None

# Cache de diretórios criados para otimizar syscalls do SO
_CREATED_DIRS = set()

def safe_mkdir(path_obj: Path):
    if str(path_obj) not in _CREATED_DIRS:
        path_obj.mkdir(parents=True, exist_ok=True)
        _CREATED_DIRS.add(str(path_obj))

def get_detector() -> GlucometerDetector:
    global _DETECTOR_SINGLETON
    if _DETECTOR_SINGLETON is None:
        _DETECTOR_SINGLETON = GlucometerDetector()
    return _DETECTOR_SINGLETON


def resolve_existing_image_path(caminho_original: str, paciente: str) -> Optional[str]:
    """
    Resolve o caminho da imagem no disco. Se a foto foi renomeada (ex: adicionado '+142.jpg'),
    encontra o arquivo atualizado no diretório do paciente.
    """
    if os.path.exists(caminho_original):
        return caminho_original

    dir_paciente = DIR_INPUT / paciente
    if not dir_paciente.exists():
        return None

    # Tenta encontrar por busca semântica do nome base do WhatsApp
    base_name = os.path.basename(caminho_original)
    stem_base = os.path.splitext(base_name)[0].split("+")[0].strip()

    candidatos = list(dir_paciente.glob(f"*{stem_base}*"))
    candidatos_validos = [str(c) for c in candidatos if c.suffix.lower() in [".jpg", ".jpeg", ".png", ".webp"]]

    if candidatos_validos:
        return candidatos_validos[0]

    return None


def crop_visor_yolo(caminho_img: str, margem: float = 0.45) -> tuple[Optional[Image.Image], dict]:
    """
    Detecta o aparelho glicosímetro na imagem usando YOLO11s otimizado e aplica margem de segurança.
    """
    detector = get_detector()
    try:
        pil_img = Image.open(caminho_img).convert("RGB")
        w_orig, h_orig = pil_img.size
    except Exception as e:
        return None, {"erro": f"Erro ao abrir imagem: {e}", "conf": 0.0}

    x1, y1, x2, y2, conf = detector.detect(pil_img)

    # Aplica margem generosa (ex: 45%) para evitar que números fiquem colados na borda do recorte
    marg_x = int((x2 - x1) * margem)
    marg_y = int((y2 - y1) * margem)
    x1_m = max(0, x1 - marg_x)
    y1_m = max(0, y1 - marg_y)
    x2_m = min(w_orig, x2 + marg_x)
    y2_m = min(h_orig, y2 + marg_y)

    crop = pil_img.crop((x1_m, y1_m, x2_m, y2_m))
    return crop, {"bbox": (x1_m, y1_m, x2_m, y2_m), "conf": conf}


def index_input_folder(target_patient: Optional[str] = None) -> int:
    """
    Varre data/input/ e registra mídias não cadastradas no banco de dados.
    """
    if not DIR_INPUT.exists():
        return 0

    count = 0
    pastas = [target_patient] if target_patient else [d.name for d in DIR_INPUT.iterdir() if d.is_dir()]

    for paciente in pastas:
        dir_paciente = DIR_INPUT / paciente
        if not dir_paciente.exists():
            continue

        pares = parear_imagens_e_audios(str(dir_paciente))
        
        for par in pares:
            img_path = str(dir_paciente / par['image'])
            audio_path = str(dir_paciente / par['audio']) if par['audio'] else None
            
            db.register_media_item(
                patient_id=paciente,
                original_path=img_path,
                audio_path=audio_path
            )
            count += 1

    db.sync_db_with_disk_and_labels()
    return count


def process_media_item(media_id: int, vlm_engine: VLMOpinionEngine, callback_progresso=None, dataset_handle=None) -> Dict[str, Any]:
    """
    Processa um único item de mídia gravado no banco de dados.
    """
    start_time = time.time()
    session = db.get_session()
    try:
        from api.database import MediaItem, AuditLog
        item = session.query(MediaItem).filter_by(id=media_id).first()
        if not item:
            return {"status": "ERROR", "motivo": "Item não encontrado no DB"}

        paciente = item.patient_id
        original_path = item.original_path
        caminho_img = resolve_existing_image_path(original_path, paciente)

        if not caminho_img:
            item.status = "ERROR"
            session.commit()
            return {"status": "ERROR", "motivo": f"Arquivo não encontrado no disco: {original_path}"}

        nome_base = os.path.basename(caminho_img)
        item.status = "PROCESSING"
        session.commit()

        if callback_progresso:
            callback_progresso(f"🔍 YOLO11s recortando visor: {nome_base}")

        # 1. YOLO Crop
        crop_pil, info_yolo = crop_visor_yolo(caminho_img)
        if crop_pil is None:
            # Em caso de falha no crop, manda para revisão humana com a imagem original
            dir_rev = DIR_REVISAO / paciente
            safe_mkdir(dir_rev)
            dest = str(dir_rev / nome_base)
            if os.path.exists(caminho_img):
                shutil.copy2(caminho_img, dest)

            item.status = "HUMAN_REVIEW"
            item.crop_path = caminho_img
            session.commit()
            return {"status": "HUMAN_REVIEW", "motivo": info_yolo.get("erro")}

        # Salva o crop no disco em data/crops/
        dir_crop_paciente = DIR_CROPS / paciente
        safe_mkdir(dir_crop_paciente)
        caminho_crop = str(dir_crop_paciente / nome_base)
        crop_pil.save(caminho_crop, format="JPEG", quality=95)

        # 2. VLM Primário
        if callback_progresso:
            callback_progresso(f"🤖 {MODELO_PRINCIPAL} lendo: {nome_base}")
        res_p = vlm_engine.predict(crop_pil, model_name=MODELO_PRINCIPAL)

        # 3. VLM Secundário
        if callback_progresso:
            callback_progresso(f"🔬 {MODELO_SECUNDARIO} verificando: {nome_base}")
        res_s = vlm_engine.predict(crop_pil, model_name=MODELO_SECUNDARIO)

        val_p, status_p, raw_p = res_p.get("valor"), res_p.get("status"), res_p.get("raw_text")
        val_s, status_s, raw_s = res_s.get("valor"), res_s.get("status"), res_s.get("raw_text")

        if raw_p:
            raw_p = raw_p.replace('\x00', '')
        if raw_s:
            raw_s = raw_s.replace('\x00', '')

        # 4. Votação e Decisão
        concordam = (
            val_p is not None
            and val_s is not None
            and val_p == val_s
            and status_p == "SUCCESS"
            and status_s == "SUCCESS"
        )

        if callback_progresso:
            if concordam:
                callback_progresso(f"  ↳ ✅ AMBOS LERAM `{val_p} mg/dL` -> APROVADO AUTOMATICAMENTE!")
            else:
                p_str = f"{val_p} mg/dL" if val_p else "Incerteza"
                s_str = f"{val_s} mg/dL" if val_s else "Incerteza"
                callback_progresso(f"  ↳ ⚠️ DISCORDÂNCIA: {MODELO_PRINCIPAL}: `{p_str}` | {MODELO_SECUNDARIO}: `{s_str}` -> ENVIADO PARA REVISÃO HUMANA")

        tempo_ms = (time.time() - start_time) * 1000

        item.crop_path = caminho_crop
        item.valor_principal = val_p
        item.status_principal = status_p
        item.raw_principal = raw_p
        item.valor_secundario = val_s
        item.status_secundario = status_s
        item.raw_secundario = raw_s
        item.yolo_conf = info_yolo.get("conf", 0.0)
        item.tempo_ms = round(tempo_ms, 2)
        item.updated_at = datetime.utcnow()

        updates = {"status": "APPROVED"} if concordam else {"status": "HUMAN_REVIEW"}

        if concordam:
            item.valor_final = val_p
            item.status = "APPROVED"
            item.revisado_por = "auto"

            # Copia imagem para a pasta oficial de aprovados
            dir_aprovados = DIR_APROVADOS / paciente
            safe_mkdir(dir_aprovados)
            dest = str(dir_aprovados / nome_base)
            shutil.copy2(caminho_img, dest)

            # Grava no dataset de treino JSONL usando buffer
            amostra = {
                "image": dest,
                "patient": paciente,
                "valor": val_p,
                "conversations": [
                    {"from": "human", "value": "<image>\nPlease look at the screen of this glucometer and tell me the main number that represents the glucose level (mg/dL)."},
                    {"from": "gpt", "value": str(val_p)}
                ]
            }
            if dataset_handle:
                dataset_handle.write(json.dumps(amostra, ensure_ascii=False) + "\n")
            else:
                with open(ARQUIVO_DATASET, "a", encoding="utf-8") as f:
                    f.write(json.dumps(amostra, ensure_ascii=False) + "\n")

            log = AuditLog(media_item_id=media_id, acao="AUTO_APPROVE", detalhes=json.dumps({"valor": val_p, "tempo_ms": tempo_ms}))
            session.add(log)

        else:
            # Em caso de discordância ou dúvida, envia para a Fila de Revisão Humana (HITL)
            item.status = "HUMAN_REVIEW"

            dir_rev = DIR_REVISAO / paciente
            safe_mkdir(dir_rev)
            dest = str(dir_rev / nome_base)
            shutil.copy2(caminho_img, dest)

            log = AuditLog(media_item_id=media_id, acao="HUMAN_REVIEW_DISAGREEMENT", detalhes=json.dumps({"val_p": val_p, "val_s": val_s}))
            session.add(log)

        session.commit()
        return updates

    except Exception as err:
        # Se ocorrem exceções inesperadas, encapsula como HUMAN_REVIEW para não perder o item
        import traceback
        if callback_progresso:
            callback_progresso(f"❌ Exceção capturada: {err}\n{traceback.format_exc()}")
        try:
            item.status = "HUMAN_REVIEW"
            session.commit()
        except Exception:
            session.rollback()
        return {"status": "HUMAN_REVIEW", "motivo": f"Exceção capturada: {err}"}
    finally:
        session.close()


def run_pipeline(
    target_patient: Optional[str] = None,
    force_reprocess: bool = False,
    reprocess_review: bool = False,
    callback_progresso: Optional[Callable] = None,
    callback_item: Optional[Callable] = None,
    cancel_check: Optional[Callable[[], bool]] = None
) -> Dict[str, Any]:
    """
    Executa o pipeline em lote consumindo a fila do Banco de Dados.
    """
    # Indexa pastas
    index_input_folder(target_patient)

    if force_reprocess:
        db.reset_items_to_pending(target_patient)
    elif reprocess_review:
        db.reset_review_to_pending(target_patient)

    # Busca itens pendentes
    pending_items = db.get_pending_items(target_patient)

    vlm_engine = VLMOpinionEngine(primary_model=MODELO_PRINCIPAL, secondary_model=MODELO_SECUNDARIO)

    total_count = len(pending_items)
    relatorio = {
        "total": total_count,
        "aprovados": 0,
        "revisao_humana": 0,
        "erros": 0,
        "interrompido": False
    }

    if callback_progresso:
        callback_progresso(f"🚀 Iniciando processamento em lote: {total_count} itens no banco de dados...")

    dataset_handle = open(ARQUIVO_DATASET, "a", encoding="utf-8")
    try:
        for idx, item in enumerate(pending_items):
            if cancel_check and cancel_check():
                if callback_progresso:
                    callback_progresso("⏹️ Processamento interrompido a pedido do usuário!")
                relatorio["interrompido"] = True
                break

            if callback_progresso:
                callback_progresso(f"[{idx+1}/{total_count}] Processando ID #{item.id} (Paciente: {item.patient_id})...")

            res = process_media_item(item.id, vlm_engine, callback_progresso, dataset_handle=dataset_handle)
            st_res = res.get("status")
            if st_res == "APPROVED":
                relatorio["aprovados"] += 1
            elif st_res == "HUMAN_REVIEW":
                relatorio["revisao_humana"] += 1
            else:
                relatorio["erros"] += 1

            if callback_item:
                callback_item(idx + 1, total_count, res)
    finally:
        dataset_handle.close()

    return relatorio

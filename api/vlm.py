"""
api/vlm.py

Módulo de inferência com Modelos de Visão e Linguagem (VLM) locais via Ollama.
Atua como segunda opinião ou desempate quando o OCR especializado possui dúvida.
"""

import os
import re
import tempfile
from PIL import Image

try:
    import cv2
    OPENCV_DISPONIVEL = True
except ImportError:
    cv2 = None
    OPENCV_DISPONIVEL = False

try:
    import ollama
    OLLAMA_DISPONIVEL = True
except ImportError:
    OLLAMA_DISPONIVEL = False

SYSTEM_PROMPT_VLM = "You are a strict data extraction sensor. Your sole purpose is to return 2- or 3-digit numbers referring to blood glucose measurement (mg/dL). Do not use natural language, do not apologize, do not explain."

USER_PROMPT_VLM = """
Please look at the digital LCD screen of this blood glucometer.
Extract ONLY the main, largest blood glucose reading number (2-digit or 3-digit integer between 20 and 600 mg/dL).

CRITICAL INSTRUCTIONS:
- Focus on the main big digits on the center of the display.
- IGNORE small clock/time numbers (e.g. 12:45, 08:30).
- IGNORE small date numbers (e.g. 15/04, 07-21).
- IGNORE memory entry indexes (e.g. MEM 01, #02).

Respond with the main reading inside double brackets, for example: [[142]]. If completely illegible, respond: [[HELP]].
"""


def get_installed_ollama_models():
    """Retorna lista de nomes de modelos disponíveis no Ollama local."""
    if not OLLAMA_DISPONIVEL:
        return []
    try:
        res = ollama.list()
        models_list = getattr(res, 'models', None)
        if models_list is None and isinstance(res, dict):
            models_list = res.get('models', [])

        installed = []
        for m in (models_list or []):
            if hasattr(m, 'model'):
                installed.append(m.model)
            elif isinstance(m, dict):
                installed.append(m.get('model') or m.get('name') or '')
            elif isinstance(m, str):
                installed.append(m)
        return [x for x in installed if x]
    except Exception:
        return []


def get_candidate_model_names(requested_model: str) -> list:
    """
    Gera lista ordenada de candidatos a nomes de modelos Ollama para contornar
    erros de grafia (ex: 'qwen2.5vl:7b' vs 'qwen2.5-vl:7b') e variações de tags.
    """
    if not requested_model:
        return ["qwen2.5-vl:7b", "qwen2.5-vl", "minicpm-v"]

    candidates = [requested_model]

    # Variações para Qwen VL / Qwen 2.5 VL
    if "qwen" in requested_model.lower():
        norm = requested_model.lower().replace("qwen2.5vl", "qwen2.5-vl").replace("qwen2vl", "qwen2-vl")
        if norm not in candidates:
            candidates.append(norm)
        # Ollama official tag for Qwen Vision is qwen2-vl
        qwen_defaults = [
            "qwen2-vl:7b",
            "qwen2-vl",
            "qwen2-vl:2b",
            "qwen2-vl:latest",
            "qwen2.5-vl:7b",
            "qwen2.5-vl",
            "qwen2.5-vl:3b",
            "qwen2.5vl:7b"
        ]
        for tag in qwen_defaults:
            if tag not in candidates:
                candidates.append(tag)

    # Variações para MiniCPM
    elif "minicpm" in requested_model.lower():
        if "minicpm-v" not in candidates:
            candidates.append("minicpm-v")
        if "minicpm-v:latest" not in candidates:
            candidates.append("minicpm-v:latest")

    # Tenta cruzar com modelos já instalados no Ollama local
    installed = get_installed_ollama_models()
    if installed:
        matched = []
        for cand in list(candidates):
            for inst in installed:
                inst_lower = inst.lower()
                cand_lower = cand.lower()
                if inst_lower == cand_lower or inst_lower.startswith(cand_lower + ":") or cand_lower.startswith(inst_lower + ":"):
                    if inst not in matched:
                        matched.append(inst)
        
        if "qwen" in requested_model.lower():
            for inst in installed:
                if "qwen" in inst.lower() and "vl" in inst.lower() and inst not in matched:
                    matched.append(inst)

        if matched:
            candidates = matched + [c for c in candidates if c not in matched]

    return candidates


class VLMOpinionEngine:
    def __init__(self, primary_model=None, secondary_model=None):
        self.primary_model = primary_model or os.getenv("VLM_PRIMARY_MODEL", "minicpm-v")
        self.secondary_model = secondary_model or os.getenv("VLM_SECONDARY_MODEL", "qwen2.5-vl:7b")

    def parse_vlm_response(self, raw_text):
        """Extrai de forma inteligente o valor de glicemia da resposta do VLM."""
        if not raw_text:
            return None, "HELP"

        texto_upper = raw_text.strip().upper()

        if "HELP" in texto_upper and "[[" in texto_upper:
            return None, "HELP"

        # 1. Padrão de colchetes duplos [[142]]
        bracket_match = re.findall(r'\[\[(\d{2,3})\]\]', texto_upper)
        if bracket_match and 20 <= int(bracket_match[0]) <= 600:
            return bracket_match[0], "SUCCESS"

        # 2. Busca por números de 2 a 3 dígitos
        numeros = re.findall(r'\b\d{2,3}\b', texto_upper)
        validos = [n for n in numeros if 20 <= int(n) <= 600]

        if len(validos) == 1:
            return validos[0], "SUCCESS"
        elif len(validos) > 1:
            # Descarta números pequenos (<= 59) que geralmente são horas/minutos (ex: 08:30)
            # se houver um número principal de glicemia >= 60 no visor
            glicemia_candidatos = [n for n in validos if int(n) >= 60]
            if len(glicemia_candidatos) == 1:
                return glicemia_candidatos[0], "SUCCESS"
            elif glicemia_candidatos:
                return glicemia_candidatos[0], "UNCERTAIN"

        if "HELP" in texto_upper or "SORRY" in texto_upper or "CANNOT" in texto_upper:
            return None, "HELP"

        return None, "NO_VALID_RANGE"

    def predict(self, crop_visor_cv_ou_pil, model_name=None):
        """
        Executa a predição VLM via Ollama na imagem do visor com fallback inteligente de tags.
        Retorna dicionário: { "valor": str, "confidence": float, "raw_text": str, "model": str, "status": str }
        """
        if not OLLAMA_DISPONIVEL:
            return {"valor": None, "confidence": 0.0, "raw_text": "Biblioteca 'ollama' não está instalada no ambiente Python.", "model": None, "status": "OLLAMA_UNAVAILABLE"}

        modelo_solicitado = model_name or self.primary_model
        candidatos = get_candidate_model_names(modelo_solicitado)

        try:
            import io
            import base64
            
            buffer = io.BytesIO()
            if isinstance(crop_visor_cv_ou_pil, Image.Image):
                crop_visor_cv_ou_pil.convert("RGB").save(buffer, format="JPEG")
            elif OPENCV_DISPONIVEL and cv2 is not None:
                success, encoded_image = cv2.imencode('.jpg', crop_visor_cv_ou_pil)
                if success:
                    buffer.write(encoded_image.tobytes())
                else:
                    raise ValueError("Falha ao encodar imagem com OpenCV")
            else:
                Image.fromarray(crop_visor_cv_ou_pil).convert("RGB").save(buffer, format="JPEG")

            img_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

            response = None
            modelo_usado = None
            ultimo_erro = None

            for cand in candidatos:
                try:
                    response = ollama.chat(
                        model=cand,
                        messages=[
                            {'role': 'system', 'content': SYSTEM_PROMPT_VLM},
                            {'role': 'user', 'content': USER_PROMPT_VLM, 'images': [img_b64]}
                        ]
                    )
                    modelo_usado = cand
                    break
                except Exception as e:
                    ultimo_erro = e
                    continue

            if response is None:
                msg_erro = str(ultimo_erro) if ultimo_erro else "Modelo não encontrado no Ollama local."
                print(f"\n[ERRO OLLAMA DETALHADO - {modelo_solicitado}]: {msg_erro}\n")
                status_erro = "MODEL_NOT_FOUND" if ("not found" in msg_erro.lower() or "try pulling" in msg_erro.lower()) else "UNCERTAIN"
                return {
                    "valor": None,
                    "confidence": 0.0,
                    "raw_text": f"Erro Ollama ({modelo_solicitado}): {msg_erro}",
                    "model": modelo_solicitado,
                    "status": status_erro
                }

            raw_text = response.get('message', {}).get('content', '').strip()
            valor, status = self.parse_vlm_response(raw_text)

            confianca = 0.92 if status == "SUCCESS" else (0.65 if status == "UNCERTAIN" else 0.0)

            return {
                "valor": valor,
                "confidence": confianca,
                "raw_text": raw_text,
                "model": modelo_usado,
                "status": status
            }

        except Exception as e:
            print(f"\n[ERRO OLLAMA DETALHADO]: {repr(e)}\n")
            return {"valor": None, "confidence": 0.0, "raw_text": str(e), "model": modelo_solicitado, "status": "UNCERTAIN"}


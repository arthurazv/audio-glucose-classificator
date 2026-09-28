import os
import re
import json
import shutil
import threading
import tempfile
import io
import tkinter as tk
from tkinter import ttk
try:
    import numpy as np
    NUMPY_DISPONIVEL = True
except ImportError:
    np = None
    NUMPY_DISPONIVEL = False
from datetime import datetime

try:
    import cv2
    OPENCV_DISPONIVEL = True
except ImportError:
    cv2 = None
    OPENCV_DISPONIVEL = False

try:
    from PIL import Image, ImageTk
    IMAGETK_DISPONIVEL = True
except ImportError:
    from PIL import Image
    ImageTk = None
    IMAGETK_DISPONIVEL = False

def pil_para_photoimage(img_pil):
    """Converte PIL.Image para tk.PhotoImage usando ImageTk se disponível, ou fallback PNG em memória."""
    if IMAGETK_DISPONIVEL and ImageTk is not None:
        return ImageTk.PhotoImage(img_pil)
    buffer = io.BytesIO()
    if img_pil.mode not in ("RGB", "RGBA"):
        img_pil = img_pil.convert("RGB")
    img_pil.save(buffer, format="PNG")
    return tk.PhotoImage(data=buffer.getvalue())

try:
    import ollama
    OLLAMA_DISPONIVEL = True
except ImportError:
    OLLAMA_DISPONIVEL = False

# ---------------------------------------------------------
# Motor de Leitura de Visor por IA Local (Ollama Vision)
# ---------------------------------------------------------

SYSTEM_PROMPT_IA = "You are a strict data extraction sensor. Your sole purpose is to return 2- or 3-digit numbers referring to blood glucose measurement (mg/dL). Do not use natural language, do not apologize, do not explain."

USER_PROMPT_IA = """
Please look at the digital LCD screen of this blood glucometer.
Extract ONLY the main, largest blood glucose reading number (2-digit or 3-digit integer between 20 and 600 mg/dL).

CRITICAL INSTRUCTIONS:
- Focus on the main big digits on the center of the display.
- IGNORE small clock/time numbers (e.g. 12:45, 08:30).
- IGNORE small date numbers (e.g. 15/04, 07-21).
- IGNORE memory entry indexes (e.g. MEM 01, #02).

Reply ONLY with the single integer number. Do not write any words, punctuation, or units. If illegible, reply: HELP.
"""

def crop_center_pil(pil_img, crop_ratio=0.25):
    """Recorta o centro da imagem (corta crop_ratio em cada borda) para focar no visor do glicosímetro."""
    if pil_img is None:
        return None
    w, h = pil_img.size
    left = int(w * crop_ratio)
    top = int(h * crop_ratio)
    right = int(w * (1.0 - crop_ratio))
    bottom = int(h * (1.0 - crop_ratio))
    return pil_img.crop((left, top, right, bottom))

def processar_imagem_ia(caminho_imagem, modelo=os.getenv("VLM_PRIMARY_MODEL", "minicpm-v"), roi_manual=None):
    """
    Processa a imagem usando o modelo Vision local via Ollama.
    Retorna uma tupla: (valor_sugerido, status, img_crop_pil, resposta_raw, erro_msg)
    """
    if not OLLAMA_DISPONIVEL:
        return None, 'ERROR', None, "", "Pacote 'ollama' não está instalado."

    try:
        pil_img = Image.open(caminho_imagem)
    except Exception as e:
        return None, 'ERROR', None, "", f"Não foi possível carregar a imagem: {e}"

    # 1. Aplica recorte (manual ou automático centralizado)
    if roi_manual is not None:
        x, y, w, h = roi_manual
        recorte_pil = pil_img.crop((x, y, x + w, y + h))
    else:
        recorte_pil = crop_center_pil(pil_img, crop_ratio=0.25)

    if recorte_pil is None:
        recorte_pil = pil_img

    prompt_completo = f"{SYSTEM_PROMPT_IA}\n\n{USER_PROMPT_IA}"

    def chamar_ollama(img_pil_target):
        temp_fd, temp_path = tempfile.mkstemp(suffix=".jpg")
        os.close(temp_fd)
        try:
            img_pil_target.convert("RGB").save(temp_path, format="JPEG")
            try:
                response = ollama.chat(
                    model=modelo,
                    messages=[
                        {'role': 'user', 'content': prompt_completo, 'images': [temp_path]}
                    ]
                )
            except Exception as e_initial:
                # Tenta fallback trocando tag :latest
                if not modelo.endswith(":latest"):
                    modelo_alt = f"{modelo}:latest"
                else:
                    modelo_alt = modelo.rsplit(":latest", 1)[0]
                try:
                    response = ollama.chat(
                        model=modelo_alt,
                        messages=[
                            {'role': 'user', 'content': prompt_completo, 'images': [temp_path]}
                        ]
                    )
                except Exception:
                    raise e_initial

            return response.get('message', {}).get('content', '').strip()
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    try:
        texto_raw = chamar_ollama(recorte_pil)
        texto_upper = texto_raw.upper()
        
        # Se a IA disser que não conseguiu ou não achar números no recorte, tenta com a imagem completa
        eh_falha = ("HELP" in texto_upper or "SORRY" in texto_upper or "CANNOT" in texto_upper)
        numeros_pre = re.findall(r'\b\d{2,3}\b', texto_upper)
        validos_pre = [n for n in numeros_pre if 20 <= int(n) <= 600]

        if (eh_falha or not validos_pre) and recorte_pil != pil_img:
            # Fallback: Tenta a imagem inteira sem recorte
            try:
                raw_full = chamar_ollama(pil_img)
                if raw_full:
                    upper_full = raw_full.upper()
                    colchetes_full = re.findall(r'\[\[(\d{2,3})\]\]', upper_full)
                    nums_full = re.findall(r'\b\d{2,3}\b', upper_full)
                    val_full = [n for n in nums_full if 20 <= int(n) <= 600]
                    if colchetes_full or val_full:
                        texto_raw = raw_full
                        texto_upper = upper_full
                        recorte_pil = pil_img
            except Exception:
                pass

        if "HELP" in texto_upper or "SORRY" in texto_upper or "CANNOT" in texto_upper:
            return None, 'HELP', recorte_pil, texto_raw, ""
            
        # 1. Padrão de colchetes duplos [[142]]
        colchetes = re.findall(r'\[\[(\d{2,3})\]\]', texto_upper)
        if colchetes and 20 <= int(colchetes[0]) <= 600:
            return colchetes[0], 'SUCCESS', recorte_pil, texto_raw, ""

        numeros = re.findall(r'\b\d{2,3}\b', texto_upper)
        validos = [n for n in numeros if 20 <= int(n) <= 600]
        
        if len(validos) == 1:
            return validos[0], 'SUCCESS', recorte_pil, texto_raw, ""
        elif len(validos) > 1:
            glicemia_candidatos = [n for n in validos if int(n) >= 60]
            if len(glicemia_candidatos) == 1:
                return glicemia_candidatos[0], 'SUCCESS', recorte_pil, texto_raw, ""
            elif glicemia_candidatos:
                return glicemia_candidatos[0], 'UNCERTAIN', recorte_pil, texto_raw, f"Múltiplos números detectados: {validos}"
            else:
                return validos[0], 'UNCERTAIN', recorte_pil, texto_raw, f"Múltiplos números pequenos: {validos}"
        else:
            return None, 'HELP', recorte_pil, texto_raw, "Nenhum número no intervalo válido (20-600) foi encontrado."

    except Exception as e:
        msg_erro = str(e)
        return None, 'ERROR', recorte_pil, "", msg_erro


# ---------------------------------------------------------
# Motor OCR legado de 7 segmentos (fallback secundário)
# ---------------------------------------------------------


# Definições relativas de busca (serão escaladas conforme o tamanho real da imagem)
BUSCA_Y1_REF, BUSCA_Y2_REF = 500, 1150
BUSCA_X1_REF, BUSCA_X2_REF = 50, 850
REF_H, REF_W = 1600, 900

ALTURA_MIN_DIGITO = 100
ALTURA_MAX_DIGITO = 320
LARGURA_MIN_DIGITO = 100
LARGURA_MAX_DIGITO = 500

def _binarizar(cinza):
    return cv2.adaptiveThreshold(
        cinza, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 45, 10
    )

def localizar_bloco_digitos(binarizada):
    kernel = np.ones((9, 9), np.uint8)
    dilatada = cv2.dilate(binarizada, kernel, iterations=1)

    contornos, _ = cv2.findContours(dilatada, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidatos = []
    for contorno in contornos:
        x, y, w, h = cv2.boundingRect(contorno)
        if (ALTURA_MIN_DIGITO <= h <= ALTURA_MAX_DIGITO and
                LARGURA_MIN_DIGITO <= w <= LARGURA_MAX_DIGITO):
            candidatos.append((w * h, (x, y, w, h)))

    if not candidatos:
        return None

    candidatos.sort(key=lambda c: c[0], reverse=True)
    return candidatos[0][1]

def segmentar_digitos(bloco_binarizado):
    altura_bloco, largura_bloco = bloco_binarizado.shape[:2]

    altura_kernel = max(3, int(altura_bloco * 0.5))
    largura_kernel = max(1, int(altura_bloco * 0.03))
    kernel = np.ones((altura_kernel, largura_kernel), np.uint8)
    dilatada = cv2.dilate(bloco_binarizado, kernel, iterations=1)

    contornos, _ = cv2.findContours(dilatada, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    caixas = [cv2.boundingRect(c) for c in contornos]
    caixas = [c for c in caixas if c[3] > altura_bloco * 0.4]

    caixas_finais = []
    for caixa in caixas:
        caixas_finais.extend(dividir_caixa_larga(caixa, bloco_binarizado))

    caixas_finais.sort(key=lambda c: c[0])
    return caixas_finais

def dividir_caixa_larga(caixa, bloco_binarizado, razao_max=0.85):
    x, y, w, h = caixa
    if h == 0 or w / h <= razao_max:
        return [caixa]

    sub = bloco_binarizado[y:y+h, x:x+w]
    perfil = (sub > 0).sum(axis=0)

    meio = w // 2
    margem = max(1, w // 6)
    inicio, fim = max(1, meio - margem), min(w - 1, meio + margem)

    if inicio >= fim:
        return [caixa]

    corte = inicio + int(np.argmin(perfil[inicio:fim]))

    caixa_esq = (x, y, corte, h)
    caixa_dir = (x + corte, y, w - corte, h)

    return (dividir_caixa_larga(caixa_esq, bloco_binarizado, razao_max) +
            dividir_caixa_larga(caixa_dir, bloco_binarizado, razao_max))

PONTOS_SEGMENTOS = {
    'a': (0.50, 0.10),
    'f': (0.12, 0.28),
    'b': (0.88, 0.28),
    'g': (0.50, 0.50),
    'e': (0.12, 0.74),
    'c': (0.88, 0.74),
    'd': (0.50, 0.92),
}

JANELA_PADRAO = 0.16
LIMIAR_PADRAO = 0.30
AJUSTES_SEGMENTO = {
    'g': {'frac_janela': 0.24, 'limiar': 0.12},
}

def ler_segmentos(digito_bin):
    h, w = digito_bin.shape
    if h == 0 or w == 0:
        return '?', {}

    def aceso(cx, cy, frac_janela, limiar):
        px, py = int(cx * w), int(cy * h)
        rx = max(2, int(frac_janela * w))
        ry = max(2, int(frac_janela * h))
        x1, x2 = max(0, px - rx), min(w, px + rx)
        y1, y2 = max(0, py - ry), min(h, py + ry)
        janela = digito_bin[y1:y2, x1:x2]
        if janela.size == 0:
            return False
        return (janela > 0).mean() > limiar

    estados = {}
    for seg, (cx, cy) in PONTOS_SEGMENTOS.items():
        ajuste = AJUSTES_SEGMENTO.get(seg, {})
        frac_janela = ajuste.get('frac_janela', JANELA_PADRAO)
        limiar = ajuste.get('limiar', LIMIAR_PADRAO)
        estados[seg] = aceso(cx, cy, frac_janela, limiar)

    a, b, c, d, e, f, g = (estados['a'], estados['b'], estados['c'], estados['d'],
                            estados['e'], estados['f'], estados['g'])

    padrao = (a, b, c, d, e, f, g)

    tabela = {
        (True,  True,  True,  True,  True,  True,  False): '0',
        (False, True,  True,  False, False, False, False): '1',
        (True,  True,  False, True,  True,  False, True):  '2',
        (True,  True,  True,  True,  False, False, True):  '3',
        (False, True,  True,  False, False, True,  True):  '4',
        (True,  False, True,  True,  False, True,  True):  '5',
        (True,  False, True,  True,  True,  True,  True):  '6',
        (True,  True,  True,  False, False, False, False): '7',
        (True,  True,  True,  True,  True,  True,  True):  '8',
        (True,  True,  True,  True,  False, True,  True):  '9',
    }

    return tabela.get(padrao, '?'), estados

def ler_numero_completo(bloco_binarizado):
    caixas = segmentar_digitos(bloco_binarizado)

    debug = cv2.cvtColor(bloco_binarizado, cv2.COLOR_GRAY2BGR)
    numero = ""

    for indice, (x, y, w, h) in enumerate(caixas):
        recorte = bloco_binarizado[y:y+h, x:x+w]
        caractere, estados = ler_segmentos(recorte)
        numero += caractere

        cor = (0, 255, 0) if caractere != '?' else (0, 0, 255)
        cv2.rectangle(debug, (x, y), (x + w, y + h), cor, 2)
        cv2.putText(debug, caractere, (x, max(0, y - 8)),
                    cv2.FONT_HERSHEY_SIMPLEX, 1.0, cor, 2)

        for seg, (cx, cy) in PONTOS_SEGMENTOS.items():
            px, py = x + int(cx * w), y + int(cy * h)
            cor_ponto = (0, 255, 0) if estados.get(seg) else (140, 140, 140)
            cv2.circle(debug, (px, py), 5, cor_ponto, -1)
            cv2.putText(debug, seg, (px + 6, py - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1)

    return numero, debug

def processar_imagem_ocr(caminho_imagem, roi_manual=None):
    if not OPENCV_DISPONIVEL:
        return None, None
    imagem = cv2.imread(caminho_imagem)
    if imagem is None:
        return None, None

    if roi_manual is not None:
        x, y, w, h = roi_manual
        recorte = imagem[y:y+h, x:x+w]
        cinza = cv2.cvtColor(recorte, cv2.COLOR_BGR2GRAY)
        binarizada = _binarizar(cinza)
        return ler_numero_completo(binarizada)

    h_img, w_img = imagem.shape[:2]
    # Escala proporcionalmente baseado na resolução de referência 900x1600
    y1 = int(h_img * (BUSCA_Y1_REF / REF_H))
    y2 = int(h_img * (BUSCA_Y2_REF / REF_H))
    x1 = int(w_img * (BUSCA_X1_REF / REF_W))
    x2 = int(w_img * (BUSCA_X2_REF / REF_W))

    zona_busca = imagem[y1:y2, x1:x2]
    cinza = cv2.cvtColor(zona_busca, cv2.COLOR_BGR2GRAY)
    binarizada = _binarizar(cinza)

    bloco = localizar_bloco_digitos(binarizada)
    if bloco is None:
        return None, None

    bx, by, bw, bh = bloco
    recorte_numero = binarizada[by:by+bh, bx:bx+bw]

    proporcao_acesa = (recorte_numero > 0).mean()
    if proporcao_acesa < 0.03:
        return None, None

    return ler_numero_completo(recorte_numero)


# ---------------------------------------------------------
# Registro e Utilitários de Arquivos
# ---------------------------------------------------------

NOME_REGISTRO = ".registro_leitura.json"
PADRAO_JA_ROTULADO = re.compile(r"\+\s*\d+\.(jpg|jpeg|png|webp)$", re.IGNORECASE)

def carregar_registro(diretorio_trabalho):
    caminho = os.path.join(diretorio_trabalho, NOME_REGISTRO)
    if os.path.exists(caminho):
        try:
            with open(caminho, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"confirmados": {}, "pulados": []}

def salvar_registro(diretorio_trabalho, registro):
    caminho = os.path.join(diretorio_trabalho, NOME_REGISTRO)
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(registro, f, ensure_ascii=False, indent=2)

def obter_novo_nome(nome_arquivo, valor_glicemia):
    nome_sem_ext, ext = os.path.splitext(nome_arquivo)
    
    if "+" in nome_sem_ext:
        partes = nome_sem_ext.split("+", 1)
        base = partes[0].rstrip()
        novo_nome = f"{base} +{valor_glicemia}{ext}"
    else:
        novo_nome = f"{nome_sem_ext} +{valor_glicemia}{ext}"
        
    return novo_nome

def listar_pastas_pacientes(raiz_input):
    if not os.path.exists(raiz_input):
        return []
    return [d for d in sorted(os.listdir(raiz_input)) 
            if os.path.isdir(os.path.join(raiz_input, d)) and not d.startswith(".")]

def parear_imagens_e_audios(diretorio_trabalho):
    image_extensions = ('.jpg', '.jpeg', '.png', '.webp')
    audio_extensions = ('.opus', '.ogg')
    
    all_files = os.listdir(diretorio_trabalho)
    images = [f for f in all_files if f.lower().endswith(image_extensions)]
    audios = [f for f in all_files if f.lower().endswith(audio_extensions)]
    
    time_pattern = re.compile(r"WhatsApp (\d{4}-\d{2}-\d{2} at \d{2}\.\d{2}\.\d{2})")
    formato_data = "%Y-%m-%d at %H.%M.%S"
    
    def parse_time(filename):
        match = time_pattern.search(filename)
        if match:
            try:
                return datetime.strptime(match.group(1), formato_data)
            except ValueError:
                return None
        return None

    # Parseia datas das imagens
    image_data = []
    for img in images:
        dt = parse_time(img)
        if dt:
            image_data.append((img, dt))
            
    # Parseia datas dos áudios
    audio_data = []
    for aud in audios:
        dt = parse_time(aud)
        if dt:
            audio_data.append((aud, dt))
            
    # Cria os pares
    pares = []
    used_audios = set()
    
    # Ordena imagens por data/hora
    image_data.sort(key=lambda x: x[1])
    
    for img, img_dt in image_data:
        best_aud = None
        min_diff = float('inf')
        
        for aud, aud_dt in audio_data:
            if aud in used_audios:
                continue
            diff = abs((img_dt - aud_dt).total_seconds())
            if diff < min_diff:
                min_diff = diff
                best_aud = aud
                
        # Pareia se a diferença for de até 10 minutos (600s)
        if best_aud and min_diff <= 600:
            pares.append({
                'image': img,
                'audio': best_aud,
                'time_diff_seconds': min_diff,
                'datetime': img_dt
            })
            used_audios.add(best_aud)
        else:
            pares.append({
                'image': img,
                'audio': None,
                'time_diff_seconds': None,
                'datetime': img_dt
            })
            
    return pares


# ---------------------------------------------------------
# Interface Gráfica (Tkinter)
# ---------------------------------------------------------

class AplicativoRotulagem:
    def __init__(self, diretorio_trabalho, pares, registro):
        self.diretorio_trabalho = diretorio_trabalho
        self.pares = pares
        self.registro = registro
        self.indice = 0
        self.mostrar_todos = False
        self.filtro_data_inicio = None
        self.filtro_data_fim = None
        
        self.filtrar_pares()
        
        self.janela = tk.Tk()
        self.janela.title("Glicovoz - Rotulador & Revisor de Glicemia")
        self.janela.geometry("1100x750")
        self.janela.configure(bg="#f8fafc")
        
        # Configuração de Estilo
        self.style = ttk.Style()
        self.style.theme_use("clam")
        self.style.configure(".", background="#f8fafc", font=("Helvetica", 11))
        
        # --- CONTAINER PRINCIPAL ---
        container = tk.Frame(self.janela, bg="#f8fafc")
        container.pack(fill="both", expand=True, padx=20, pady=20)
        
        # --- TOP HEADER BAR ---
        self.frame_topo = tk.Frame(container, bg="#ffffff", height=60, bd=1, relief="ridge")
        self.frame_topo.pack(fill="x", side="top", pady=(0, 15))
        self.frame_topo.pack_propagate(False)
        
        raiz_input = os.path.dirname(os.path.abspath(diretorio_trabalho))
        self.pastas_pacientes = listar_pastas_pacientes(raiz_input)
        pessoa = os.path.basename(diretorio_trabalho)
        
        frame_paciente = tk.Frame(self.frame_topo, bg="#ffffff")
        frame_paciente.pack(side="left", padx=15, pady=12)
        
        tk.Label(frame_paciente, text="👤 Paciente:", font=("Helvetica", 10, "bold"), bg="#ffffff", fg="#000000").pack(side="left", padx=(0, 5))
        self.combo_paciente = ttk.Combobox(
            frame_paciente,
            values=self.pastas_pacientes if self.pastas_pacientes else [pessoa],
            width=15,
            state="readonly"
        )
        self.combo_paciente.set(pessoa)
        self.combo_paciente.pack(side="left")
        self.combo_paciente.bind("<<ComboboxSelected>>", self._trocar_paciente)
        
        # Seletor de Modelo Ollama Vision
        self.modelo_ia = os.getenv("VLM_PRIMARY_MODEL", "minicpm-v")
        self.current_req_id = 0
        
        frame_modelo = tk.Frame(self.frame_topo, bg="#ffffff")
        frame_modelo.pack(side="left", padx=30, pady=12)
        
        tk.Label(frame_modelo, text="🤖 Modelo IA:", font=("Helvetica", 10, "bold"), bg="#ffffff", fg="#000000").pack(side="left", padx=(0, 5))
        self.combo_modelo = ttk.Combobox(
            frame_modelo,
            values=[os.getenv("VLM_PRIMARY_MODEL", "minicpm-v"), os.getenv("VLM_SECONDARY_MODEL", "qwen2.5-vl:7b")],
            width=15,
            state="normal"
        )
        self.combo_modelo.set(self.modelo_ia)
        self.combo_modelo.pack(side="left")
        self.combo_modelo.bind("<<ComboboxSelected>>", self._atualizar_modelo)
        self.combo_modelo.bind("<FocusOut>", self._atualizar_modelo)

        self.label_progresso = tk.Label(self.frame_topo, text="", font=("Helvetica", 12, "italic"), 
                                        bg="#ffffff", fg="#000000")
        self.label_progresso.pack(side="right", padx=15, pady=15)
        
        # --- CORPO (Esquerda: Foto, Direita: Controles) ---
        frame_corpo = tk.Frame(container, bg="#f8fafc")
        frame_corpo.pack(fill="both", expand=True)
        
        # 1. LADO ESQUERDO: Painel da Imagem
        self.frame_esquerda = tk.Frame(frame_corpo, bg="#ffffff", bd=1, relief="solid")
        self.frame_esquerda.pack(side="left", fill="both", expand=True, padx=(0, 10))
        
        self.label_imagem = tk.Label(self.frame_esquerda, bg="#ffffff")
        self.label_imagem.pack(fill="both", expand=True, padx=10, pady=10)
        
        # 2. LADO DIREITO: Painel de Controles
        self.frame_direita = tk.Frame(frame_corpo, bg="#ffffff", bd=1, relief="solid", width=420)
        self.frame_direita.pack(side="right", fill="y", expand=False, padx=(10, 0))
        self.frame_direita.pack_propagate(False)
        
        # Detalhes do Arquivo Atual
        self.frame_info = tk.LabelFrame(self.frame_direita, text="Arquivos Atuais", 
                                        bg="#ffffff", font=("Helvetica", 10, "bold"), fg="#000000")
        self.frame_info.pack(fill="x", padx=15, pady=15)
        
        self.label_nome_img = tk.Label(self.frame_info, text="", anchor="w", bg="#ffffff", fg="#000000", wraplength=380)
        self.label_nome_img.pack(fill="x", padx=10, pady=4)
        
        self.label_nome_aud = tk.Label(self.frame_info, text="", anchor="w", bg="#ffffff", fg="#000000", wraplength=380)
        self.label_nome_aud.pack(fill="x", padx=10, pady=(4, 10))
        
        # Campo de Entrada Gigante
        frame_input = tk.Frame(self.frame_direita, bg="#ffffff")
        frame_input.pack(fill="x", padx=15, pady=10)
        
        tk.Label(frame_input, text="Valor da Glicemia:", font=("Helvetica", 14, "bold"), bg="#ffffff", fg="#000000").pack(anchor="w")
        
        self.entrada = tk.Entry(frame_input, font=("Helvetica", 42, "bold"), width=6, justify="center",
                                bd=3, relief="groove", bg="#f8fafc", fg="#000000")
        self.entrada.pack(pady=10)
        
        # Binds de Teclado
        self.entrada.bind("<Return>", lambda ev: self.confirmar())
        self.entrada.bind("<KeyPress-c>", self._confirmar_via_tecla)
        self.entrada.bind("<KeyPress-C>", self._confirmar_via_tecla)
        
        # OCR Status e Exibição de Debug
        self.label_ocr_status = tk.Label(self.frame_direita, text="", font=("Helvetica", 11, "bold"), bg="#ffffff", wraplength=380)
        self.label_ocr_status.pack(pady=5)
        
        self.label_ocr_debug = tk.Label(self.frame_direita, bg="#ffffff")
        self.label_ocr_debug.pack(pady=5)
        
        # Botões de Ação
        frame_acoes = tk.Frame(self.frame_direita, bg="#ffffff")
        frame_acoes.pack(fill="x", side="bottom", pady=20, padx=15)
        
        self.btn_confirmar = tk.Button(frame_acoes, text="✓ Confirmar (Enter / C)", font=("Helvetica", 13, "bold"),
                                       height=2, command=self.confirmar)
        self.btn_confirmar.pack(fill="x", pady=5)
        
        # Navegação (Voltar / Avançar)
        frame_navegacao = tk.Frame(frame_acoes, bg="#ffffff")
        frame_navegacao.pack(fill="x", pady=5)
        
        self.btn_voltar = tk.Button(frame_navegacao, text="↩ Voltar", font=("Helvetica", 11),
                                    width=16, height=2, command=self.voltar)
        self.btn_voltar.pack(side="left", padx=(0, 5))
        
        self.btn_avancar = tk.Button(frame_navegacao, text="Avançar ➔", font=("Helvetica", 11),
                                     width=16, height=2, command=self.avancar)
        self.btn_avancar.pack(side="right", padx=(5, 0))
        
        # Auxiliares (Pular / Recortar)
        frame_auxiliar = tk.Frame(frame_acoes, bg="#ffffff")
        frame_auxiliar.pack(fill="x", pady=5)
        
        self.btn_pular = tk.Button(frame_auxiliar, text="Pular Foto", font=("Helvetica", 11),
                                   width=16, height=2, command=self.pular)
        self.btn_pular.pack(side="left", padx=(0, 5))
        
        self.btn_recortar = tk.Button(frame_auxiliar, text="🔍 Recortar (R)", font=("Helvetica", 11),
                                      width=16, height=2, command=self.recortar_visor)
        self.btn_recortar.pack(side="right", padx=(5, 0))
        self.janela.bind("<KeyPress-r>", lambda ev: self.recortar_visor())
        self.janela.bind("<KeyPress-R>", lambda ev: self.recortar_visor())
        
        # --- FILTRO DE DATA/HORA ---
        self.frame_filtro_data = tk.LabelFrame(self.frame_direita, text="Filtrar por Faixa de Tempo", 
                                              bg="#ffffff", font=("Helvetica", 10, "bold"), fg="#000000")
        self.frame_filtro_data.pack(fill="x", padx=15, pady=10)
        self.frame_filtro_data.columnconfigure(1, weight=1)
        
        tk.Label(self.frame_filtro_data, text="Início (DD/MM/AAAA HH:MM):", bg="#ffffff", fg="#000000").grid(row=0, column=0, padx=5, pady=5, sticky="w")
        self.ent_data_inicio = tk.Entry(self.frame_filtro_data, bg="#f8fafc", fg="#000000", font=("Helvetica", 10))
        self.ent_data_inicio.grid(row=0, column=1, padx=5, pady=5, sticky="ew")
        
        tk.Label(self.frame_filtro_data, text="Fim (DD/MM/AAAA HH:MM):", bg="#ffffff", fg="#000000").grid(row=1, column=0, padx=5, pady=5, sticky="w")
        self.ent_data_fim = tk.Entry(self.frame_filtro_data, bg="#f8fafc", fg="#000000", font=("Helvetica", 10))
        self.ent_data_fim.grid(row=1, column=1, padx=5, pady=5, sticky="ew")
        
        dts = [p['datetime'] for p in self.pares if p.get('datetime')]
        if dts:
            self.ent_data_inicio.insert(0, min(dts).strftime("%d/%m/%Y %H:%M"))
            self.ent_data_fim.insert(0, max(dts).strftime("%d/%m/%Y %H:%M"))
        
        frame_btn_filtro = tk.Frame(self.frame_filtro_data, bg="#ffffff")
        frame_btn_filtro.grid(row=2, column=0, columnspan=2, padx=5, pady=5, sticky="ew")
        
        self.btn_aplicar_filtro = tk.Button(frame_btn_filtro, text="Filtrar", font=("Helvetica", 9), command=self.aplicar_filtro_data)
        self.btn_aplicar_filtro.pack(side="left", expand=True, fill="x", padx=(0, 2))
        
        self.btn_limpar_filtro = tk.Button(frame_btn_filtro, text="Limpar", font=("Helvetica", 9), command=self.limpar_filtro_data)
        self.btn_limpar_filtro.pack(side="left", expand=True, fill="x", padx=(2, 2))
        
        self.btn_isolar_filtro = tk.Button(frame_btn_filtro, text="Excluir Outros", font=("Helvetica", 9), command=self.isolar_fora_da_faixa)
        self.btn_isolar_filtro.pack(side="right", expand=True, fill="x", padx=(2, 0))

        # Checkbox para alternar modo de revisão
        if not self.pares_filtrados and self.pares:
            self.mostrar_todos = True
            self.filtrar_pares()
            self.var_mostrar_todos = tk.BooleanVar(value=True)
        else:
            self.var_mostrar_todos = tk.BooleanVar(value=False)

        self.cb_mostrar_todos = tk.Checkbutton(self.frame_direita, text="Mostrar todos (Modo Revisão)", 
                                               variable=self.var_mostrar_todos, command=self.alternar_modo,
                                               bg="#ffffff", activebackground="#ffffff", fg="#000000", font=("Helvetica", 10))
        self.cb_mostrar_todos.pack(side="bottom", pady=5)
        
        # Variáveis internas
        self.imagem_atual_tk = None
        self.imagem_ocr_debug_tk = None
        
        self.mostrar_proxima()
        self.janela.mainloop()

    def _atualizar_modelo(self, evento=None):
        novo = self.combo_modelo.get().strip()
        if novo:
            self.modelo_ia = novo
            print(f"[Modelo IA alterado para: {self.modelo_ia}]")

    def _trocar_paciente(self, evento=None):
        nova_pasta = self.combo_paciente.get().strip()
        if not nova_pasta:
            return
        raiz_input = os.path.dirname(os.path.abspath(self.diretorio_trabalho))
        novo_dir = os.path.join(raiz_input, nova_pasta)
        if not os.path.exists(novo_dir):
            return
        restaurar_fora_da_faixa_se_necessario(novo_dir)
        self.diretorio_trabalho = novo_dir
        self.pares = parear_imagens_e_audios(novo_dir)
        self.registro = carregar_registro(novo_dir)
        self.indice = 0
        self.filtrar_pares()
        self.mostrar_proxima()

    def _confirmar_via_tecla(self, evento):
        self.confirmar()
        return "break"

    def filtrar_pares(self):
        if self.mostrar_todos:
            candidatos = self.pares
        else:
            candidatos = []
            for par in self.pares:
                img = par['image']
                if not PADRAO_JA_ROTULADO.search(img) and img not in self.registro.get("pulados", []):
                    candidatos.append(par)
                    
        # Filtra pela faixa de tempo se definida
        self.pares_filtrados = []
        for par in candidatos:
            dt = par.get('datetime')
            if dt:
                if self.filtro_data_inicio and dt < self.filtro_data_inicio:
                    continue
                if self.filtro_data_fim and dt > self.filtro_data_fim:
                    continue
            self.pares_filtrados.append(par)

    def mostrar_imagem_principal(self, caminho_img):
        with Image.open(caminho_img) as img_pil:
            img_pil.thumbnail((500, 650))
            self.imagem_atual_tk = pil_para_photoimage(img_pil)
            self.label_imagem.configure(image=self.imagem_atual_tk)

    def mostrar_imagem_ocr_debug(self, debug_img):
        if debug_img is None:
            self.label_ocr_debug.configure(image="")
            self.imagem_ocr_debug_tk = None
            return
            
        if isinstance(debug_img, Image.Image):
            img_pil = debug_img.copy()
        elif OPENCV_DISPONIVEL and isinstance(debug_img, np.ndarray):
            img_rgb = cv2.cvtColor(debug_img, cv2.COLOR_BGR2RGB)
            img_pil = Image.fromarray(img_rgb)
        elif isinstance(debug_img, np.ndarray):
            img_pil = Image.fromarray(np.uint8(debug_img))
        else:
            self.label_ocr_debug.configure(image="")
            return

        img_pil.thumbnail((350, 150))
        self.imagem_ocr_debug_tk = pil_para_photoimage(img_pil)
        self.label_ocr_debug.configure(image=self.imagem_ocr_debug_tk)

    def mostrar_proxima(self):
        if self.indice >= len(self.pares_filtrados):
            self.finalizar()
            return
            
        # Re-habilita os controles caso estivessem desabilitados pelo finalizar()
        self.entrada.configure(state="normal")
        self.btn_confirmar.configure(state="normal")
        self.btn_pular.configure(state="normal")
        self.btn_recortar.configure(state="normal")
        self.btn_avancar.configure(state="normal")
        
        par = self.pares_filtrados[self.indice]
        caminho_img = os.path.join(self.diretorio_trabalho, par['image'])
        
        self.mostrar_imagem_principal(caminho_img)
        
        restantes = len(self.pares_filtrados) - self.indice
        self.label_progresso.configure(
            text=f"Item {self.indice + 1} de {len(self.pares_filtrados)}  •  Faltam {restantes}"
        )
        self.label_nome_img.configure(text=f"Imagem: {par['image']}")
        if par['audio']:
            self.label_nome_aud.configure(text=f"Áudio: {par['audio']}")
        else:
            self.label_nome_aud.configure(text="Áudio: [Sem áudio pareado próximo]")
            
        # Tenta extrair valor já existente
        match_existente = re.search(r"\+\s*(\d+)", par['image'])
        
        self.entrada.delete(0, tk.END)
        
        if match_existente:
            valor_salvo = match_existente.group(1)
            self.entrada.insert(0, valor_salvo)
            self.label_ocr_status.configure(text=f"Valor rotulado anteriormente: {valor_salvo}", fg="#1e40af")
            self.mostrar_imagem_ocr_debug(None)
        else:
            self.label_ocr_status.configure(text=f"🤖 Processando IA ({self.modelo_ia})...", fg="#9a3412")
            self.janela.update_idletasks()
            
            self.current_req_id += 1
            req_id = self.current_req_id
            
            thread = threading.Thread(
                target=self._rodar_ia_background,
                args=(req_id, caminho_img, self.modelo_ia, None),
                daemon=True
            )
            thread.start()
            
        self.entrada.focus_set()
        self.entrada.select_range(0, tk.END)

    def _rodar_ia_background(self, req_id, caminho_img, modelo, roi_manual):
        resultado = processar_imagem_ia(caminho_img, modelo=modelo, roi_manual=roi_manual)
        if self.janela and self.janela.winfo_exists():
            self.janela.after(0, lambda: self._aplicar_resultado_ia(req_id, caminho_img, resultado))

    def _aplicar_resultado_ia(self, req_id, caminho_img, resultado):
        if req_id != self.current_req_id:
            return  # O usuário já navegou para outra imagem

        valor, status, img_debug, raw_response, error_msg = resultado

        self.mostrar_imagem_ocr_debug(img_debug)

        if status == 'SUCCESS':
            self.entrada.delete(0, tk.END)
            self.entrada.insert(0, valor)
            self.label_ocr_status.configure(
                text=f"🟢 IA ({self.modelo_ia}) detectou: {valor} mg/dL — [Enter / C para confirmar]",
                fg="#166534"
            )
        elif status == 'UNCERTAIN':
            self.entrada.delete(0, tk.END)
            if valor:
                self.entrada.insert(0, valor)
            self.label_ocr_status.configure(
                text=f"🟡 IA incerta. Sugestão: {valor} — Verifique antes de confirmar",
                fg="#9a3412"
            )
        elif status == 'HELP':
            self.entrada.delete(0, tk.END)
            # Tenta OCR OpenCV como fallback secundário
            leitura_ocr, debug_ocr = processar_imagem_ocr(caminho_img)
            if leitura_ocr and '?' not in leitura_ocr:
                self.entrada.insert(0, leitura_ocr)
                self.label_ocr_status.configure(
                    text=f"🟡 (Fallback OpenCV) OCR detectou: {leitura_ocr}",
                    fg="#9a3412"
                )
                self.mostrar_imagem_ocr_debug(debug_ocr)
            else:
                self.label_ocr_status.configure(
                    text="🔴 IA não reconheceu o visor. Digite o valor manualmente.",
                    fg="#991b1b"
                )
        elif status == 'ERROR':
            self.entrada.delete(0, tk.END)
            # Fallback para OCR OpenCV se Ollama falhar/não estiver instalado
            leitura_ocr, debug_ocr = processar_imagem_ocr(caminho_img)
            if leitura_ocr and '?' not in leitura_ocr:
                self.entrada.insert(0, leitura_ocr)
                self.label_ocr_status.configure(
                    text=f"🟡 (Ollama off) OCR detectou: {leitura_ocr}",
                    fg="#9a3412"
                )
                self.mostrar_imagem_ocr_debug(debug_ocr)
            else:
                self.label_ocr_status.configure(
                    text=f"⚠️ IA indisponível: {error_msg or 'Verifique o Ollama'}. Digite manualmente.",
                    fg="#991b1b"
                )

        self.entrada.focus_set()
        self.entrada.select_range(0, tk.END)

    def confirmar(self):
        if self.indice >= len(self.pares_filtrados):
            return
            
        valor = self.entrada.get().strip()
        if not valor:
            self.pular()
            return
            
        if not valor.isdigit():
            self.label_ocr_status.configure(text="Erro: Digite apenas números!", fg="#991b1b")
            return
            
        par = self.pares_filtrados[self.indice]
        img_original = par['image']
        aud_original = par['audio']
        
        # 1. Renomeia imagem
        nova_img = obter_novo_nome(img_original, valor)
        caminho_orig_img = os.path.join(self.diretorio_trabalho, img_original)
        caminho_novo_img = os.path.join(self.diretorio_trabalho, nova_img)
        
        if img_original != nova_img:
            if os.path.exists(caminho_novo_img):
                os.remove(caminho_orig_img)
            else:
                os.rename(caminho_orig_img, caminho_novo_img)
            print(f"[OK Img] {img_original} -> {nova_img}")
            
        # 2. Renomeia áudio se existir
        nova_aud = None
        if aud_original:
            nova_aud = obter_novo_nome(aud_original, valor)
            caminho_orig_aud = os.path.join(self.diretorio_trabalho, aud_original)
            caminho_novo_aud = os.path.join(self.diretorio_trabalho, nova_aud)
            
            if aud_original != nova_aud:
                if os.path.exists(caminho_novo_aud):
                    os.remove(caminho_orig_aud)
                else:
                    os.rename(caminho_orig_aud, caminho_novo_aud)
                print(f"[OK Aud] {aud_original} -> {nova_aud}")
                
        # Atualiza a lista em memória
        par['image'] = nova_img
        if aud_original:
            par['audio'] = nova_aud
            
        self.registro["confirmados"][img_original] = valor
        # Se estava na lista de pulados, remove de lá
        if img_original in self.registro["pulados"]:
            self.registro["pulados"].remove(img_original)
        salvar_registro(self.diretorio_trabalho, self.registro)
        
        self.indice += 1
        self.mostrar_proxima()

    def pular(self):
        if self.indice >= len(self.pares_filtrados):
            return
            
        par = self.pares_filtrados[self.indice]
        img_original = par['image']
        
        if img_original not in self.registro["pulados"]:
            self.registro["pulados"].append(img_original)
            salvar_registro(self.diretorio_trabalho, self.registro)
            
        print(f"[PULADO] {img_original}")
        
        self.indice += 1
        self.mostrar_proxima()

    def voltar(self):
        if self.indice > 0:
            self.indice -= 1
            self.mostrar_proxima()

    def avancar(self):
        if self.indice < len(self.pares_filtrados) - 1:
            self.indice += 1
            self.mostrar_proxima()
        else:
            self.indice += 1
            self.mostrar_proxima()

    def recortar_visor(self):
        if not OPENCV_DISPONIVEL:
            from tkinter import messagebox
            messagebox.showwarning(
                "OpenCV Não Instalado",
                "A seleção de recorte com mouse (R) requer o OpenCV.\nPara habilitar a seleção manual, instale o opencv (ex: pip install opencv-python)."
            )
            return
            
        if self.indice >= len(self.pares_filtrados):
            return
        
        par = self.pares_filtrados[self.indice]
        caminho_img = os.path.join(self.diretorio_trabalho, par['image'])
        
        imagem = cv2.imread(caminho_img)
        if imagem is None:
            return
            
        # Escala imagem para o selectROI caber na tela
        h_orig, w_orig = imagem.shape[:2]
        limite_h = 800
        escala = 1.0
        if h_orig > limite_h:
            escala = limite_h / h_orig
            w_redim = int(w_orig * escala)
            h_redim = limite_h
            imagem_redim = cv2.resize(imagem, (w_redim, h_redim))
        else:
            imagem_redim = imagem.copy()
            
        roi = cv2.selectROI("Selecione o visor (ENTER para confirmar, ESC para cancelar)", 
                            imagem_redim, showCrosshair=True)
        cv2.destroyWindow("Selecione o visor (ENTER para confirmar, ESC para cancelar)")
        
        x, y, w, h = roi
        if w > 0 and h > 0:
            x_orig = int(x / escala)
            y_orig = int(y / escala)
            w_orig_roi = int(w / escala)
            h_orig_roi = int(h / escala)
            
            roi_original = (x_orig, y_orig, w_orig_roi, h_orig_roi)
            
            # Roda a IA no recorte manual em background
            self.label_ocr_status.configure(text=f"🤖 Processando IA no recorte ({self.modelo_ia})...", fg="#9a3412")
            self.janela.update_idletasks()
            
            self.current_req_id += 1
            req_id = self.current_req_id
            
            thread = threading.Thread(
                target=self._rodar_ia_background,
                args=(req_id, caminho_img, self.modelo_ia, roi_original),
                daemon=True
            )
            thread.start()

    def alternar_modo(self):
        self.mostrar_todos = self.var_mostrar_todos.get()
        arquivo_atual = None
        if self.indice < len(self.pares_filtrados):
            arquivo_atual = self.pares_filtrados[self.indice]['image']
            
        self.filtrar_pares()
        
        self.indice = 0
        if arquivo_atual:
            for idx, par in enumerate(self.pares_filtrados):
                if par['image'] == arquivo_atual:
                    self.indice = idx
                    break
                    
        self.mostrar_proxima()

    def aplicar_filtro_data(self):
        inicio_str = self.ent_data_inicio.get().strip()
        fim_str = self.ent_data_fim.get().strip()
        
        ano_padrao = 2026
        for par in self.pares:
            if par.get('datetime'):
                ano_padrao = par['datetime'].year
                break
                
        def parse_data(texto):
            if not texto:
                return None
            for fmt in ("%d/%m %H:%M", "%d/%m/%Y %H:%M"):
                try:
                    dt = datetime.strptime(texto, fmt)
                    if "%Y" not in fmt:
                        dt = dt.replace(year=ano_padrao)
                    return dt
                except ValueError:
                    continue
            return None
            
        dt_inicio = parse_data(inicio_str)
        dt_fim = parse_data(fim_str)
        
        if inicio_str and not dt_inicio:
            self.label_ocr_status.configure(text="Erro: Início inválido! Use DD/MM/AAAA HH:MM ou DD/MM HH:MM", fg="#991b1b")
            return
        if fim_str and not dt_fim:
            self.label_ocr_status.configure(text="Erro: Fim inválido! Use DD/MM/AAAA HH:MM ou DD/MM HH:MM", fg="#991b1b")
            return
            
        self.filtro_data_inicio = dt_inicio
        self.filtro_data_fim = dt_fim
        
        arquivo_atual = None
        if self.indice < len(self.pares_filtrados):
            arquivo_atual = self.pares_filtrados[self.indice]['image']
            
        self.filtrar_pares()
        
        self.indice = 0
        if arquivo_atual:
            for idx, par in enumerate(self.pares_filtrados):
                if par['image'] == arquivo_atual:
                    self.indice = idx
                    break
                    
        self.mostrar_proxima()
        self.label_ocr_status.configure(text="Filtro de faixa de tempo aplicado!", fg="#166534")

    def limpar_filtro_data(self):
        self.ent_data_inicio.delete(0, tk.END)
        self.ent_data_fim.delete(0, tk.END)
        self.filtro_data_inicio = None
        self.filtro_data_fim = None
        
        self.filtrar_pares()
        self.indice = 0
        self.mostrar_proxima()
        self.label_ocr_status.configure(text="Filtro de data limpo.", fg="#000000")

    def isolar_fora_da_faixa(self):
        if not self.filtro_data_inicio and not self.filtro_data_fim:
            from tkinter import messagebox
            messagebox.showwarning(
                "Filtro não definido",
                "Por favor, defina e aplique um filtro de data/hora antes de excluir os outros."
            )
            return
            
        msg = f"Deseja mover todos os arquivos fora do intervalo selecionado:\n"
        if self.filtro_data_inicio:
            msg += f"  Início: {self.filtro_data_inicio.strftime('%d/%m %H:%M')}\n"
        if self.filtro_data_fim:
            msg += f"  Fim: {self.filtro_data_fim.strftime('%d/%m %H:%M')}\n"
        msg += "\npara a pasta '_fora_da_faixa' como backup?"
        
        from tkinter import messagebox
        if not messagebox.askyesno("Confirmar Exclusão de Outros", msg):
            return
            
        pasta_backup = os.path.join(self.diretorio_trabalho, "_fora_da_faixa")
        if not os.path.exists(pasta_backup):
            os.makedirs(pasta_backup)
            
        movidos = 0
        novos_pares = []
        
        for par in self.pares:
            dt = par.get('datetime')
            excluir = False
            if dt:
                if self.filtro_data_inicio and dt < self.filtro_data_inicio:
                    excluir = True
                if self.filtro_data_fim and dt > self.filtro_data_fim:
                    excluir = True
            
            if excluir:
                caminho_img = os.path.join(self.diretorio_trabalho, par['image'])
                if os.path.exists(caminho_img):
                    shutil.move(caminho_img, os.path.join(pasta_backup, par['image']))
                    
                if par['audio']:
                    caminho_aud = os.path.join(self.diretorio_trabalho, par['audio'])
                    if os.path.exists(caminho_aud):
                        shutil.move(caminho_aud, os.path.join(pasta_backup, par['audio']))
                movidos += 1
            else:
                novos_pares.append(par)
                
        self.pares = novos_pares
        self.filtrar_pares()
        self.indice = 0
        self.mostrar_proxima()
        
        messagebox.showinfo(
            "Sucesso",
            f"Movidos {movidos} par(es) de arquivos fora da faixa para a pasta '_fora_da_faixa'."
        )

    def finalizar(self):
        self.label_nome_img.configure(text="")
        self.label_nome_aud.configure(text="")
        self.label_progresso.configure(text="Concluído!")
        self.label_imagem.configure(image="")
        self.label_ocr_status.configure(text="Parabéns! Todos os itens foram processados/revisados.", fg="#166534")
        self.mostrar_imagem_ocr_debug(None)
        self.entrada.configure(state="disabled")
        self.btn_confirmar.configure(state="disabled")
        self.btn_pular.configure(state="disabled")
        self.btn_recortar.configure(state="disabled")
        self.btn_avancar.configure(state="disabled")
        print("\nProcessamento finalizado!")


def restaurar_fora_da_faixa_se_necessario(diretorio_trabalho):
    pasta_fora = os.path.join(diretorio_trabalho, "_fora_da_faixa")
    if os.path.exists(pasta_fora):
        arquivos = [f for f in os.listdir(pasta_fora) if not f.startswith(".")]
        if arquivos:
            for f in arquivos:
                src_f = os.path.join(pasta_fora, f)
                dst_f = os.path.join(diretorio_trabalho, f)
                if os.path.isfile(src_f):
                    shutil.move(src_f, dst_f)
            print(f"[Restaurados {len(arquivos)} arquivos da pasta '_fora_da_faixa']")

def main():
    print("Iniciando etiquetar.py (Rotulador & OCR Unificado)...")
    
    raiz_projeto = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    diretorio_base = os.path.join(raiz_projeto, "data", "input")
    
    # 1. Seleciona dinamicamente a pasta do paciente
    pastas = listar_pastas_pacientes(diretorio_base)
    if not pastas:
        print(f"Erro: Nenhuma subpasta de paciente encontrada em '{diretorio_base}'.")
        print("Certifique-se de descompactar a conversa antes (Cenário B no run.py).")
        return
        
    if len(pastas) == 1:
        diretorio_alvo = os.path.join(raiz_input, pastas[0])
    else:
        print("\nSelecione a pasta do paciente para rotular:")
        for idx, pasta in enumerate(pastas):
            print(f"[{idx}] {pasta}")
        while True:
            try:
                opcao = int(input(f"Selecione o número da pasta (0-{len(pastas)-1}): ").strip())
                if 0 <= opcao < len(pastas):
                    diretorio_alvo = os.path.join(raiz_input, pastas[opcao])
                    break
            except ValueError:
                pass
            print("Opção inválida. Tente novamente.")
            
    print(f"\nDiretório selecionado: {diretorio_alvo}")
    
    # Restaura arquivos isolados em _fora_da_faixa se houver
    restaurar_fora_da_faixa_se_necessario(diretorio_alvo)

    # 2. Carrega e pareia os arquivos
    pares = parear_imagens_e_audios(diretorio_alvo)
    if not pares:
        print("Nenhum par de imagem e áudio encontrado neste diretório.")
        return
        
    print(f"Encontrados {len(pares)} pares de arquivos.")
    
    # 3. Carrega o registro e roda a GUI
    registro = carregar_registro(diretorio_alvo)
    try:
        AplicativoRotulagem(diretorio_alvo, pares, registro)
    except tk.TclError as e:
        if "DISPLAY" in str(e) or "display name" in str(e):
            print("\n[Erro de Interface Gráfica]: Não foi possível abrir a janela Tkinter pois a variável $DISPLAY não está definida no terminal.")
            print("Execute com a variável de tela definida:")
            print("   DISPLAY=:0 python etiquetar.py")
        else:
            raise e


if __name__ == "__main__":
    main()

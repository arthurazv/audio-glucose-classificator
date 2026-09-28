"""
app.py

Streamlit UI ultra-leve para o Glicovoz.
Funciona como uma camada estritamente visual e de interação (HITL),
consumindo dados do PostgreSQL / SQLite em tempo real sem travar a interface.
"""

import os
import re
import json
import shutil
import threading
import sys
from datetime import datetime, time as dtime
from pathlib import Path
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from api.database import db
from api.pipeline import run_pipeline, MODELO_PRINCIPAL, MODELO_SECUNDARIO, DIR_APROVADOS, DIR_REVISAO, ARQUIVO_DATASET, index_input_folder
from ui.etiquetar import parear_imagens_e_audios, obter_novo_nome, carregar_registro, salvar_registro

def extrair_stem_base(nome_arquivo):
    base, _ = os.path.splitext(nome_arquivo)
    if "+" in base:
        base = base.split("+")[0]
    return base.strip()

def sincronizar_rotulos_no_disco(dir_paciente: str) -> int:
    """
    Sincroniza os rótulos salvos em .registro_leitura.json e no banco de dados com os
    arquivos reais de áudio e imagem no disco (renomeando ex: 'WhatsApp ... +.opus' para 'WhatsApp ... +142.opus').
    """
    if not os.path.exists(dir_paciente):
        return 0

    rotulos = {}

    # 1. Lê do arquivo .registro_leitura.json
    caminho_reg = os.path.join(dir_paciente, ".registro_leitura.json")
    if os.path.exists(caminho_reg):
        try:
            with open(caminho_reg, "r", encoding="utf-8") as f:
                dados = json.load(f)
                confirmados = dados.get("confirmados", {})
                for k, v in confirmados.items():
                    if v and str(v).isdigit():
                        stem = extrair_stem_base(k)
                        rotulos[stem] = str(v)
        except Exception:
            pass

    # 2. Lê do Banco de Dados
    try:
        from api.database import MediaItem
        session = db.get_session()
        paciente_id = os.path.basename(dir_paciente)
        itens = session.query(MediaItem).filter_by(patient_id=paciente_id).all()
        for item in itens:
            if item.valor_final and str(item.valor_final).isdigit():
                if item.original_path:
                    stem = extrair_stem_base(os.path.basename(item.original_path))
                    rotulos[stem] = str(item.valor_final)
        session.close()
    except Exception:
        pass

    if not rotulos:
        return 0

    aplicados = 0
    # Usa a função de pareamento que tem janela de 10 minutos
    pares = parear_imagens_e_audios(dir_paciente)
    
    for par in pares:
        img_name = par['image']
        aud_name = par['audio']
        
        stem = extrair_stem_base(img_name)
        if stem not in rotulos:
            continue
            
        valor = rotulos[stem]
        
        # Função auxiliar local para renomear
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
            else:
                os.rename(origem, destino)
                aplicados += 1
                
        # Renomeia imagem e áudio pareado
        renomear_item(img_name)
        if aud_name:
            renomear_item(aud_name)

    return aplicados

# Variaveis globais thread-safe para logs em background
GLOBAL_PIPELINE_LOGS = []
GLOBAL_PIPELINE_RUNNING = False

st.set_page_config(
    page_title="Glicovoz - Painel de Rotulagem",
    layout="wide"
)

st.title("Glicovoz — Painel de Rotulagem & Validação de Glicemia")

# --- BARRA LATERAL ---
st.sidebar.header("Configurações & Conexão")

raiz_projeto = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
raiz_input = os.path.join(raiz_projeto, "data", "input")

metricas_db = db.get_metrics()
st.sidebar.caption(f"**Banco de Dados:** `{metricas_db['db_engine']}`")

# Lista pacientes gravados no banco ou na pasta data/input/
if os.path.exists(raiz_input):
    index_input_folder()  # Garante que novas pastas são indexadas no DB

pastas_pacientes = [d for d in os.listdir(raiz_input) if os.path.isdir(os.path.join(raiz_input, d)) and not d.startswith(".")] if os.path.exists(raiz_input) else []
db_patients = db.get_all_patient_ids()
todos_pacientes = sorted(list(set(pastas_pacientes + db_patients)))

pasta_selecionada = None
if todos_pacientes:
    pasta_selecionada = st.sidebar.selectbox("Paciente Selecionado:", todos_pacientes)
else:
    st.sidebar.warning("Nenhum paciente encontrado em `data/input/` ou no banco de dados.")

# Modos de Operação
modo_operacao = st.sidebar.radio(
    "Modo de Operação:",
    options=[
        "Pipeline Automático (Headless)",
        "Galeria Auditável (Por Paciente)",
        "Revisão Humana (HITL)",
        "Rotulagem Manual",
        "Filtro de Intervalo",
        "Métricas & Auditoria DB"
    ]
)

with st.sidebar.expander("Métricas de Operação", expanded=False):
    st.markdown(
        f"- Total no Banco: `{metricas_db['total_processado']}`\n"
        f"- Concluídos (Aprovados): `{metricas_db['aprovados']}`\n"
        f"  - Auto-Aceitação (IA): `{metricas_db['aprovados_ia']}` ({metricas_db['taxa_auto_aceitacao_pct']}%)\n"
        f"  - Revisado (Humano): `{metricas_db['aprovados_humano']}`\n"
        f"- Para Revisão: `{metricas_db['revisao_humana']}` ({metricas_db['taxa_revisao_humana_pct']}%)\n"
        f"- Pendentes: `{metricas_db['pendentes']}`\n"
        f"- Latência Média: `{metricas_db['tempo_medio_ms']} ms`"
    )

with st.sidebar.expander("Acurácia na Revisão Humana", expanded=False):
    st.markdown(
        f"De **{metricas_db.get('hitl_total_corrigidos', 0)}** imagens auditadas por humano:\n"
        f"- {MODELO_PRINCIPAL} Acertou: `{metricas_db.get('hitl_acertos_principal', 0)}`\n"
        f"- {MODELO_SECUNDARIO} Acertou: `{metricas_db.get('hitl_acertos_secundario', 0)}`\n"
        f"- Ambos Erraram (Discordantes): `{metricas_db.get('hitl_ambos_erraram', 0)}`\n"
        f"- **Erros Críticos Mútuos:** `{metricas_db.get('hitl_erros_criticos_mutuos', 0)}`"
    )

if "is_processing" not in st.session_state:
    st.session_state.is_processing = False

if st.sidebar.button("Atualizar Dados", width="stretch", disabled=st.session_state.is_processing):
    st.rerun()

if st.sidebar.button("Sincronizar Rótulos no Disco", width="stretch", help="Renomeia as mídias em data/input com os valores de glicemia marcados no app"):
    total_sync = 0
    if os.path.exists(raiz_input):
        for p in os.listdir(raiz_input):
            cp = os.path.join(raiz_input, p)
            if os.path.isdir(cp):
                total_sync += sincronizar_rotulos_no_disco(cp)
    st.sidebar.success(f" {total_sync} mídias renomeadas com +glicose no disco!")
    st.rerun()



# MODO 1: PIPELINE AUTOMÁTICO HEADLESS

if modo_operacao.startswith("Pipeline"):
    st.subheader(f" Pipeline Automático — YOLO11s + {MODELO_PRINCIPAL} + {MODELO_SECUNDARIO}")
    st.markdown(
        f"**Modelos:** `{MODELO_PRINCIPAL}` (Primário) | `{MODELO_SECUNDARIO}` (Validador)  \n"
        f"**Arquitetura:** Processamento de segundo plano com persistência no banco de dados."
    )
    with st.expander("Verificar Status e Modelos Instalados no Ollama", expanded=False):
        from api.vlm import get_installed_ollama_models, OLLAMA_DISPONIVEL
        if not OLLAMA_DISPONIVEL:
            st.error("Pacote `ollama` não instalado no ambiente Python.")
        else:
            modelos_inst = get_installed_ollama_models()
            if modelos_inst:
                st.success(f"Ollama Operacional — Modelos locais encontrados ({len(modelos_inst)}):")
                st.code("\n".join(modelos_inst), language="plaintext")
            else:
                st.warning(f" Nenhum modelo retornado pelo Ollama local ou serviço fora do ar. Dica: Rode `ollama pull {MODELO_SECUNDARIO}` e `ollama pull {MODELO_PRINCIPAL}` no seu terminal.")
    st.divider()

    metrics_box = st.empty()

    def draw_metrics(placeholder):
        m = db.get_metrics()
        with placeholder.container():
            col1, col2, col3, col4 = st.columns(4)
            col1.metric(" Pendentes", m["pendentes"])
            col2.metric(" Aprovados (Total)", m["aprovados"])
            col3.metric(" Aprovados IA", m["aprovados_ia"])
            col4.metric(" Fila de Revisão", m["revisao_humana"])

    draw_metrics(metrics_box)

    st.markdown("### Executar Lote de Processamento")
    opt_pac = st.selectbox("Selecione o alvo:", options=["— Todos os Pacientes —"] + todos_pacientes)
    target_pac = None if opt_pac.startswith("—") else opt_pac

    force_rep = st.checkbox(" Reprocessar TUDO (incluindo Aprovados)", value=False, help="Marque esta opção para forçar o reprocessamento de todo o lote novamente.")
    rep_review = st.checkbox(" Reprocessar apenas itens na Fila de Revisão", value=False, help="Marque para tentar rotular novamente os itens que os VLMs discordaram ou falharam anteriormente.")

    # Inicializa Histórico de Logs na Sessão para não perder ao recarregar
    if "pipeline_logs_history" not in st.session_state:
        st.session_state.pipeline_logs_history = []

    c_l1, c_l2 = st.columns([4, 1])
    with c_l2:
        if st.button(" Limpar Log", key="btn_clear_log"):
            st.session_state.pipeline_logs_history = []
            st.rerun()

    log_box = st.empty()
    if st.session_state.pipeline_logs_history:
        log_box.code(
            "\n".join(st.session_state.pipeline_logs_history[-40:]),
            language="plaintext"
        )

    col_b1, col_b2 = st.columns([2, 1])
    with col_b1:
        btn_start = st.button(" Iniciar Processamento em Lote", type="primary", width="stretch")
    with col_b2:
        btn_stop = st.button(" Parar Processamento", width="stretch")

    if "cancel_requested" not in st.session_state:
        st.session_state.cancel_requested = False

    if btn_stop:
        st.session_state.cancel_requested = True
        st.warning("Parada solicitada! O pipeline vai parar imediatamente ao concluir o item em andamento.")

    if btn_start:
        st.session_state.cancel_requested = False
        st.session_state.is_processing = True
        st.session_state.pipeline_logs_history = [" Iniciando novo lote de processamento..."]
        prog_bar = st.progress(0.0, text="Iniciando pipeline...")

        def cb_log(msg):
            st.session_state.pipeline_logs_history.append(msg)
            log_box.code(
                "\n".join(st.session_state.pipeline_logs_history[-40:]),
                language="plaintext"
            )

        def cb_item(curr, total, item_res):
            frac = curr / total if total > 0 else 1.0
            prog_bar.progress(frac, text=f"Processando mídia {curr} de {total} ({(frac * 100):.1f}%)...")
            # Otimização: Atualiza os blocos visuais de métrica apenas a cada 5 imagens para não sobrecarregar o painel
            if curr % 5 == 0 or curr == total:
                draw_metrics(metrics_box)

        def check_cancel():
            return st.session_state.get("cancel_requested", False)

        try:
            with st.spinner("Processando lote de mídias pelo pipeline..."):
                relatorio = run_pipeline(
                    target_patient=target_pac,
                    force_reprocess=force_rep,
                    reprocess_review=rep_review,
                    callback_progresso=cb_log,
                    callback_item=cb_item,
                    cancel_check=check_cancel
                )
        finally:
            st.session_state.is_processing = False

        if relatorio.get("interrompido"):
            st.warning(" Processamento interrompido pelo usuário!")
        else:
            prog_bar.progress(1.0, text=" Processamento concluído!")
            st.success(
                f" Lote finalizado! Total: {relatorio['total']} | "
                f"Aprovados: {relatorio['aprovados']} | "
                f"Revisão Humana: {relatorio['revisao_humana']} | "
                f"Erros: {relatorio['erros']}"
            )
        st.rerun()

    st.stop()


# MODO: GALERIA AUDITÁVEL (VISÃO POR PACIENTE)

if modo_operacao.startswith("Galeria"):
    st.subheader(" Galeria Auditável — Histórico de Imagens e Rotulagem")
    st.markdown("Exibe todas as mídias em ordem cronológica com a identificação exata do rótulo e do responsável pela rotulagem (VLM Auto ou Humano HITL).")
    mostrar_original = st.toggle("Mostrar Foto Original (Camera) ao lado do Recorte (IA)", value=False, key="toggle_galeria")
    st.divider()

    c_f1, c_f2 = st.columns(2)
    with c_f1:
        opt_pac_g = st.selectbox("Filtrar por Paciente:", options=["— Todos os Pacientes —"] + todos_pacientes, key="gal_pac")
        target_pac_g = None if opt_pac_g.startswith("—") else opt_pac_g
    with c_f2:
        opt_st_g = st.selectbox("Filtrar por Status:", options=["TODOS", "APPROVED", "HUMAN_REVIEW", "PENDING", "ERROR", "PROCESSING"], key="gal_st")

    gal_items = db.get_patient_gallery(patient_id=target_pac_g, status_filter=opt_st_g)

    if not gal_items:
        st.info("Nenhuma imagem encontrada para os filtros selecionados.")
        st.stop()

    st.markdown(f"**Exibindo {len(gal_items)} mídias gravadas no Banco de Dados (Ordenadas por ID):**")

    cols_per_row = 3
    for i in range(0, len(gal_items), cols_per_row):
        cols = st.columns(cols_per_row)
        for j, col in enumerate(cols):
            if i + j < len(gal_items):
                item = gal_items[i + j]
                crop_path = item.get("crop_path")
                orig_path = item.get("original_path")
                nome_arq = os.path.basename(orig_path)

                # Marcador em destaque do responsável pela rotulagem
                rev_by = item.get("revisado_por")
                st_code = item["status"]

                if st_code == "APPROVED":
                    if rev_by == "humano":
                        tag_rotulacao = "**[ ROTULADO POR HUMANO ]**"
                    else:
                        tag_rotulacao = "**[ ROTULADO POR IA ]**"
                elif st_code == "HUMAN_REVIEW":
                    tag_rotulacao = "**[ AGUARDANDO REVISÃO HUMANA ]**"
                elif st_code == "PENDING":
                    tag_rotulacao = "**[ PENDENTE DE PROCESSAMENTO ]**"
                elif st_code == "PROCESSING":
                    tag_rotulacao = "**[ PROCESSANDO / INTERROMPIDO ]**"
                else:
                    tag_rotulacao = "**[ ERRO DE PROCESSAMENTO ]**"

                # Status visual
                if st_code == "APPROVED":
                    badge_status = "APROVADO"
                elif st_code == "HUMAN_REVIEW":
                    badge_status = "REVISÃO HUMANA"
                elif st_code == "PENDING":
                    badge_status = "PENDENTE"
                elif st_code == "PROCESSING":
                    badge_status = "PROCESSANDO"
                else:
                    badge_status = "ERRO"

                valor_exibido = item.get("valor_final") or item.get("valor_principal") or "—"

                with col:
                    if mostrar_original and orig_path and os.path.exists(orig_path) and crop_path != orig_path:
                        st.image(crop_path, caption="Recorte (IA)", use_container_width=True)
                        st.image(orig_path, caption="Original", use_container_width=True)
                    else:
                        if crop_path and os.path.exists(crop_path):
                            st.image(crop_path, caption="Recorte (IA)", use_container_width=True)
                            
                    st.markdown(tag_rotulacao)
                    st.markdown(f"ID #{item['id']} | Paciente: `{item['patient_id']}`")
                    st.markdown(f"Status: **{badge_status}**")
                    st.markdown(f"**Glicemia:** `{valor_exibido} mg/dL`")
                    if item.get("valor_principal") or item.get("valor_secundario"):
                        st.caption(f"{MODELO_PRINCIPAL}: `{item.get('valor_principal') or '-'}` | {MODELO_SECUNDARIO}: `{item.get('valor_secundario') or '-'}`")
                    st.caption(f"`{nome_arq}`")
                    st.divider()

    st.stop()



# MODO 2: REVISÃO HUMANA (HITL)

if modo_operacao.startswith("Revisão"):
    st.subheader(" Revisão Humana & Auditoria de Modelos")
    mostrar_original = st.toggle("Mostrar Foto Original (Camera) ao lado do Recorte (IA)", value=False, key="toggle_revisao")

    c_q1, c_q2 = st.columns([1.6, 1.0])
    with c_q1:
        tipo_fila_label = st.selectbox(
            " Selecionar Fila:",
            options=[
                " Fila de Discordâncias (Pendentes de Revisão)",
                " Auto-Aprovados por IA (Auditoria de Erros Mútuos)"
            ]
        )
    with c_q2:
        sub_modo = st.radio("Formato de exibição:", [" 1 a 1 (Foco)", " Em Massa (Galeria)"], horizontal=True)

    filter_type = "AUTO_APPROVED" if "Auto-Aprovados" in tipo_fila_label else "HUMAN_REVIEW"
    queue = db.get_review_queue(filter_type=filter_type)

    if not queue:
        if filter_type == "AUTO_APPROVED":
            st.info("Nenhuma imagem auto-aprovada por IA pendente de auditoria.")
        else:
            st.success("Fila de revisão limpa! Nenhuma imagem com discordância entre os VLMs.")
        st.stop()

    st.markdown(f"**{len(queue)} mídias nesta fila.**")
    st.divider()

    if "1 a 1" in sub_modo:
        idx_rev = st.session_state.get("idx_revisao", 0)
        idx_rev = max(0, min(idx_rev, len(queue) - 1))
        item = queue[idx_rev]

        col_img, col_info = st.columns([1.3, 1.0])

        with col_img:
            st.markdown(f"##### Item {idx_rev + 1} de {len(queue)} (Paciente `{item['patient_id']}`)")
            crop_path = item.get("crop_path")
            orig_path = item.get("original_path")
            
            if mostrar_original and orig_path and os.path.exists(orig_path) and crop_path != orig_path:
                c_img1, c_img2 = st.columns(2)
                with c_img1:
                    if crop_path and os.path.exists(crop_path):
                        st.image(crop_path, caption="Recorte Visor (O que as IAs leram)", use_container_width=True)
                with c_img2:
                    st.image(orig_path, caption="Ver Foto Original (Completa)", use_container_width=True)
            else:
                if crop_path and os.path.exists(crop_path):
                    st.image(crop_path, caption="Recorte Visor (O que as IAs leram)", use_container_width=True)

        with col_info:
            st.markdown("### Votação dos Modelos")
            val_p_show = item.get("valor_principal")
            if not val_p_show:
                st_p = item.get("status_principal")
                val_p_show = f"HELP ({st_p})" if st_p and st_p != "HELP" else "HELP"

            val_s_show = item.get("valor_secundario")
            if not val_s_show:
                st_s = item.get("status_secundario")
                val_s_show = f"HELP ({st_s})" if st_s and st_s != "HELP" else "HELP"

            st.warning(
                f"**{MODELO_PRINCIPAL} (Primário):** `{val_p_show}` mg/dL  \n"
                f"**{MODELO_SECUNDARIO} (Secundário):** `{val_s_show}` mg/dL  \n"
                f"**Valor Atual no DB:** `{item.get('valor_final') or '—'}` mg/dL"
            )
            val_atual = item.get("valor_final") or item.get("valor_principal") or ""

            with st.form(key=f"form_hitl_{item['id']}"):
                val_input = st.text_input(
                    "Valor Correto no Visor (mg/dL):",
                    value=str(val_atual) if val_atual else "",
                    placeholder="Digite o número exato...",
                    help="Digite o valor correto. Se for igual ao atual, confirme para auditar."
                )
                c1, c2, c3, c4 = st.columns([1, 1.2, 1, 1])
                b_prev = c1.form_submit_button("Anterior", width="stretch")
                b_conf = c2.form_submit_button("Confirmar", type="primary", width="stretch")
                b_del = c3.form_submit_button("Duplicado", width="stretch")
                b_next = c4.form_submit_button("Pular", width="stretch")

            if b_prev:
                st.session_state.idx_revisao = max(0, idx_rev - 1)
                st.rerun()
            if b_next:
                st.session_state.idx_revisao = min(len(queue) - 1, idx_rev + 1)
                st.rerun()
            if b_del:
                db.delete_media_item(item["id"])
                st.toast(f"Item #{item['id']} excluído do banco de dados!")
                st.session_state.idx_revisao = max(0, idx_rev - 1)
                st.rerun()

            if b_conf:
                val_clean = val_input.strip()
                if not val_clean.isdigit():
                    st.error("Por favor, insira um número inteiro válido (ex: 142)!")
                else:
                    db.approve_human_review(item["id"], val_clean)
                    sincronizar_rotulos_no_disco(os.path.join(raiz_input, item["patient_id"]))
                    st.toast(f"Item #{item['id']} confirmado/auditado com {val_clean} mg/dL!")
                    st.session_state.idx_revisao = max(0, idx_rev - 1)
                    st.rerun()

    else:
        st.markdown("Preencha ou altere os valores corretos das imagens abaixo. Deixe em branco os que não deseja alterar.")
        with st.form(key="form_mass_review"):
            cols_per_row = 3
            inputs = {}
            deletes = {}
            for i in range(0, len(queue), cols_per_row):
                cols = st.columns(cols_per_row)
                for j, col in enumerate(cols):
                    if i + j < len(queue):
                        item = queue[i + j]
                        with col:
                            st.markdown(f"**ID #{item['id']}** | `{item['patient_id']}`")
                            img_display = item.get("crop_path") or item.get("original_path")
                            if img_display and os.path.exists(img_display):
                                st.image(img_display, width="stretch")
                            
                            st.caption(f"{MODELO_PRINCIPAL}: `{item.get('valor_principal') or '-'}` | {MODELO_SECUNDARIO}: `{item.get('valor_secundario') or '-'}`")
                            val_atual_mass = item.get("valor_final") or ""
                            c_in, c_ch = st.columns([2, 1])
                            with c_in:
                                inputs[item['id']] = st.text_input(
                                    "Valor Correto:", 
                                    value=str(val_atual_mass) if val_atual_mass else "",
                                    key=f"mass_input_{item['id']}",
                                    placeholder="ex: 142"
                                )
                            with c_ch:
                                deletes[item['id']] = st.checkbox("Excluir", key=f"mass_del_{item['id']}")
                            st.divider()
            
            submit_mass = st.form_submit_button("Salvar Alterações / Processar Exclusões", type="primary", width="stretch")
        
        if submit_mass:
            items_to_process = []
            items_to_delete = []

            for item_id, is_del in deletes.items():
                if is_del:
                    items_to_delete.append(item_id)

            for item_id, val in inputs.items():
                if item_id in items_to_delete:
                    continue
                val_clean = val.strip()
                if val_clean:
                    if not val_clean.isdigit():
                        st.error(f"Erro no item #{item_id}: '{val_clean}' não é um número válido. Ignorando.")
                    else:
                        items_to_process.append((item_id, val_clean))
            
            if items_to_process or items_to_delete:
                prog_bar = st.progress(0.0, text="Processando alterações...")
                total = len(items_to_process) + len(items_to_delete)
                count = 0

                for item_id in items_to_delete:
                    count += 1
                    db.delete_media_item(item_id)
                    prog_bar.progress(count / total, text=f"Excluindo item #{item_id}...")

                for item_id, val_clean in items_to_process:
                    count += 1
                    db.approve_human_review(item_id, val_clean)
                    item_obj = next((x for x in queue if x["id"] == item_id), None)
                    if item_obj:
                        sincronizar_rotulos_no_disco(os.path.join(raiz_input, item_obj["patient_id"]))
                    prog_bar.progress(count / total, text=f"Salvando item #{item_id}...")
                
                st.toast(f"Alterações concluídas ({len(items_to_delete)} excluídos, {len(items_to_process)} salvos)!")
                st.rerun()
            else:
                st.warning("Nenhum valor válido preenchido e nenhuma exclusão marcada.")

    st.stop()



# MODO 3: ROTULAGEM MANUAL DA PASTA

if modo_operacao.startswith("Rotulagem"):
    if not pasta_selecionada:
        st.warning("Selecione um paciente na barra lateral.")
        st.stop()

    diretorio_alvo = os.path.join(raiz_input, pasta_selecionada)
    st.subheader(f"Rotulagem Manual — Paciente: `{pasta_selecionada}`")

    pares = parear_imagens_e_audios(diretorio_alvo)
    if not pares:
        st.info("Nenhuma imagem encontrada nesta pasta.")
        st.stop()

    idx_curr = st.session_state.get("idx_manual", 0)
    idx_curr = max(0, min(idx_curr, len(pares) - 1))
    par = pares[idx_curr]
    img_path = os.path.join(diretorio_alvo, par['image'])

    match_rot = re.search(r"\+\s*(\d+)", par['image'])
    gabarito_existente = match_rot.group(1) if match_rot else ""

    c_left, c_right = st.columns([1.3, 1.0])
    with c_left:
        st.markdown(f"##### Item {idx_curr + 1} de {len(pares)}: `{par['image']}`")
        if os.path.exists(img_path):
            st.image(img_path, width="stretch")
        if par.get('audio') and os.path.exists(os.path.join(diretorio_alvo, par['audio'])):
            st.audio(os.path.join(diretorio_alvo, par['audio']))

    with c_right:
        if gabarito_existente:
            st.success(f"**Rótulo Atual:** `{gabarito_existente}` mg/dL")
        else:
            st.info("Imagem ainda não rotulada.")

        with st.form(key=f"form_man_{idx_curr}"):
            val_digitado = st.text_input(
                "Valor da Glicemia (mg/dL):",
                value=gabarito_existente,
                placeholder="Campo vazio... Digite o valor",
                help="Insira o número exato do visor"
            )
            col_b1, col_b2, col_b3 = st.columns(3)
            b_p = col_b1.form_submit_button("Anterior", width="stretch")
            b_s = col_b2.form_submit_button("Salvar", type="primary", width="stretch")
            b_n = col_b3.form_submit_button("Pular", width="stretch")

        if b_p:
            st.session_state.idx_manual = max(0, idx_curr - 1)
            st.rerun()
        if b_n:
            st.session_state.idx_manual = min(len(pares) - 1, idx_curr + 1)
            st.rerun()

        if b_s:
            val = val_digitado.strip()
            if not val.isdigit():
                st.error("Digite apenas números inteiros!")
            else:
                # Renomeia foto
                novo_nome_img = obter_novo_nome(par['image'], val)
                if par['image'] != novo_nome_img:
                    c_orig = os.path.join(diretorio_alvo, par['image'])
                    c_dest = os.path.join(diretorio_alvo, novo_nome_img)
                    if os.path.exists(c_orig):
                        os.rename(c_orig, c_dest)

                # Renomeia áudio pareado se existir
                if par.get('audio'):
                    novo_nome_aud = obter_novo_nome(par['audio'], val)
                    if par['audio'] != novo_nome_aud:
                        c_orig_aud = os.path.join(diretorio_alvo, par['audio'])
                        c_dest_aud = os.path.join(diretorio_alvo, novo_nome_aud)
                        if os.path.exists(c_orig_aud):
                            os.rename(c_orig_aud, c_dest_aud)

                reg = carregar_registro(diretorio_alvo)
                if "confirmados" not in reg:
                    reg["confirmados"] = {}
                reg["confirmados"][par['image']] = val
                reg["confirmados"][novo_nome_img] = val
                salvar_registro(diretorio_alvo, reg)

                st.toast(f"Salvo: {val} mg/dL (Imagem e Áudio renomeados no disco)")
                st.session_state.idx_manual = min(len(pares) - 1, idx_curr + 1)
                st.rerun()

    st.stop()



# MODO 4: FILTRO DE INTERVALO

if modo_operacao.startswith("Filtro"):
    st.subheader("Filtro de Intervalo & Organização de Mídia")
    st.markdown("Filtre o período de mídias desejado e isole arquivos fora da faixa para processamento no `run_pipeline.py`.")
    st.divider()

    if not pasta_selecionada:
        st.warning("Selecione um paciente na barra lateral.")
        st.stop()

    dir_paciente = os.path.join(raiz_input, pasta_selecionada)
    if not os.path.exists(dir_paciente):
        st.error(f"Diretório do paciente não encontrado: `{dir_paciente}`")
        st.stop()

    # Parseia todas as mídias da pasta do paciente
    time_pattern = re.compile(r"WhatsApp (\d{4}-\d{2}-\d{2} at \d{2}\.\d{2}\.\d{2})")
    formato_data = "%Y-%m-%d at %H.%M.%S"

    midias_encontradas = []
    for f in os.listdir(dir_paciente):
        if f.lower().endswith(('.jpg', '.jpeg', '.png', '.webp')):
            caminho_f = os.path.join(dir_paciente, f)
            match = time_pattern.search(f)
            if match:
                try:
                    dt = datetime.strptime(match.group(1), formato_data)
                except ValueError:
                    dt = datetime.fromtimestamp(os.path.getmtime(caminho_f))
            else:
                dt = datetime.fromtimestamp(os.path.getmtime(caminho_f))
            
            midias_encontradas.append({
                "filename": f,
                "path": caminho_f,
                "datetime": dt
            })

    midias_encontradas.sort(key=lambda x: x["datetime"])

    dir_backup = os.path.join(dir_paciente, "_fora_da_faixa")
    has_backup = os.path.exists(dir_backup) and len(os.listdir(dir_backup)) > 0

    if not midias_encontradas:
        st.warning(f"Nenhuma imagem ativa encontrada na pasta principal de `{pasta_selecionada}`.")
        if has_backup:
            st.info("Existem mídias salvas na pasta `_fora_da_faixa` para este paciente.")
            if st.button("Restaurar Mídias da `_fora_da_faixa`", type="primary", width="stretch"):
                restaurados = 0
                for f_bak in os.listdir(dir_backup):
                    c_bak = os.path.join(dir_backup, f_bak)
                    c_dest = os.path.join(dir_paciente, f_bak)
                    if os.path.isfile(c_bak):
                        shutil.move(c_bak, c_dest)
                        restaurados += 1
                index_input_folder()
                st.success(f"{restaurados} mídias restauradas de `_fora_da_faixa` para a pasta principal!")
                st.rerun()
        else:
            st.info("Nenhuma imagem válida encontrada na pasta deste paciente.")
        st.stop()

    dt_min = midias_encontradas[0]["datetime"]
    dt_max = midias_encontradas[-1]["datetime"]

    st.markdown(f"Paciente: `{pasta_selecionada}` | **Total de Imagens:** `{len(midias_encontradas)}`")
    st.caption(f"Período Detectado: de `{dt_min.strftime('%d/%m/%Y %H:%M')}` até `{dt_max.strftime('%d/%m/%Y %H:%M')}`")

    st.subheader("Selecione Faixa de Data e Hora")
    col_d1, col_d2 = st.columns(2)
    with col_d1:
        data_ini = st.date_input("Data de Início:", value=dt_min.date(), format="DD/MM/YYYY")
        hora_ini_str = st.text_input("Hora de Início (HH:MM):", value=dt_min.strftime("%H:%M"), help="Digite a hora no formato HH:MM (ex: 08:30)")
    with col_d2:
        data_fim = st.date_input("Data de Fim:", value=dt_max.date(), format="DD/MM/YYYY")
        hora_fim_str = st.text_input("Hora de Fim (HH:MM):", value=dt_max.strftime("%H:%M"), help="Digite a hora no formato HH:MM (ex: 23:59)")

    try:
        hora_ini = datetime.strptime(hora_ini_str.strip(), "%H:%M").time()
    except ValueError:
        st.error("Formato de Hora de Início inválido. Digite no formato HH:MM (ex: 08:30).")
        st.stop()

    try:
        hora_fim = datetime.strptime(hora_fim_str.strip(), "%H:%M").time()
    except ValueError:
        st.error("Formato de Hora de Fim inválido. Digite no formato HH:MM (ex: 23:59).")
        st.stop()

    dt_inicio_sel = datetime.combine(data_ini, hora_ini)
    dt_fim_sel = datetime.combine(data_fim, hora_fim)

    # Separa mídias dentro e fora da faixa
    dentro_faixa = [m for m in midias_encontradas if dt_inicio_sel <= m["datetime"] <= dt_fim_sel]
    fora_faixa = [m for m in midias_encontradas if not (dt_inicio_sel <= m["datetime"] <= dt_fim_sel)]

    st.divider()
    c_m1, c_m2 = st.columns(2)
    c_m1.metric("Dentro do Intervalo", len(dentro_faixa))
    c_m2.metric("Fora do Intervalo (Serão Isoladas)", len(fora_faixa))

    col_act1, col_act2 = st.columns(2)
    with col_act1:
        btn_isolar = st.button("Mover 'Fora do Intervalo' para `_fora_da_faixa`", type="primary", width="stretch", disabled=len(fora_faixa) == 0)
    with col_act2:
        dir_backup = os.path.join(dir_paciente, "_fora_da_faixa")
        has_backup = os.path.exists(dir_backup) and len(os.listdir(dir_backup)) > 0
        btn_restaurar = st.button("Restaurar Mídias da `_fora_da_faixa`", width="stretch", disabled=not has_backup)

    if btn_isolar:
        os.makedirs(dir_backup, exist_ok=True)
        movidos = 0
        
        pares = parear_imagens_e_audios(dir_paciente)
        img_to_audio = {p['image']: p['audio'] for p in pares if p['audio']}

        for item_f in fora_faixa:
            orig = item_f["path"]
            img_filename = item_f["filename"]
            dest = os.path.join(dir_backup, img_filename)
            
            if os.path.exists(orig):
                shutil.move(orig, dest)
                movidos += 1
                
                # Move áudio pareado (usando janela de 10 min)
                audio_name = img_to_audio.get(img_filename)
                if audio_name:
                    orig_aud = os.path.join(dir_paciente, audio_name)
                    dest_aud = os.path.join(dir_backup, audio_name)
                    if os.path.exists(orig_aud):
                        shutil.move(orig_aud, dest_aud)

        index_input_folder()
        st.success(f"{movidos} mídias fora do intervalo (e áudios pareados) foram movidas para `_fora_da_faixa`!")
        st.rerun()

    if btn_restaurar:
        if os.path.exists(dir_backup):
            restaurados = 0
            for f_bak in os.listdir(dir_backup):
                c_bak = os.path.join(dir_backup, f_bak)
                c_dest = os.path.join(dir_paciente, f_bak)
                if os.path.isfile(c_bak):
                    shutil.move(c_bak, c_dest)
                    restaurados += 1
            index_input_folder()
            st.success(f"{restaurados} mídias restauradas de `_fora_da_faixa` para a pasta principal!")
            st.rerun()

    st.subheader(f"Mídias Selecionadas no Intervalo ({len(dentro_faixa)})")
    if dentro_faixa:
        cols_per_row = 4
        for i in range(0, len(dentro_faixa), cols_per_row):
            cols = st.columns(cols_per_row)
            for j, col in enumerate(cols):
                if i + j < len(dentro_faixa):
                    m_item = dentro_faixa[i + j]
                    with col:
                        st.image(m_item["path"], width="stretch")
                        st.caption(f" `{m_item['datetime'].strftime('%d/%m/%Y %H:%M')}`")
                        st.caption(f" `{m_item['filename']}`")

    st.stop()



# MODO 5: AUDITORIA & MÉTRICAS DB

if modo_operacao.startswith("Métricas"):
    st.subheader("Métricas Auditáveis & Status do Banco de Dados")
    st.json(metricas_db)
    st.stop()
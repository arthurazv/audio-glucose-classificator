"""
api/database.py

Camada de Persistência com ORM SQLAlchemy (PostgreSQL + Fallback para SQLite local).
Gerencia os dados de Pacientes, Mídias, Status do Pipeline e Auditoria.
"""

import os
import re
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Any

try:
    from sqlalchemy import (
        create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey, Enum, text, or_
    )
    from sqlalchemy.orm import declarative_base, sessionmaker, relationship, scoped_session
    Base = declarative_base()
except ImportError as e:
    raise ImportError(
        "\n\n❌ Dependências do banco de dados não encontradas no venv local.\n"
        "Por favor, execute o comando abaixo no seu terminal para instalar:\n\n"
        "    pip install -r requirements.txt\n"
    ) from e

class Patient(Base):
    __tablename__ = "patients"

    id = Column(String(100), primary_key=True)  # ex: "P10_M46"
    created_at = Column(DateTime, default=datetime.utcnow)

    media_items = relationship("MediaItem", back_populates="patient", cascade="all, delete-orphan")


class MediaItem(Base):
    __tablename__ = "media_items"

    id = Column(Integer, primary_key=True, autoincrement=True)
    patient_id = Column(String(100), ForeignKey("patients.id"), nullable=False, index=True)
    original_path = Column(Text, nullable=False, unique=True)
    crop_path = Column(Text, nullable=True)
    audio_path = Column(Text, nullable=True)

    # Status: PENDING, PROCESSING, APPROVED, HUMAN_REVIEW, ERROR
    status = Column(String(50), default="PENDING", index=True)

    valor_principal = Column(String(20), nullable=True)     # MiniCPM-V
    status_principal = Column(String(50), nullable=True)
    raw_principal = Column(Text, nullable=True)

    valor_secundario = Column(String(20), nullable=True)    # Qwen2.5-VL
    status_secundario = Column(String(50), nullable=True)
    raw_secundario = Column(Text, nullable=True)

    valor_final = Column(String(20), nullable=True)
    revisado_por = Column(String(50), nullable=True)        # 'auto' ou 'humano'
    yolo_conf = Column(Float, default=0.0)
    tempo_ms = Column(Float, default=0.0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    patient = relationship("Patient", back_populates="media_items")
    audit_logs = relationship("AuditLog", back_populates="media_item", cascade="all, delete-orphan")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    media_item_id = Column(Integer, ForeignKey("media_items.id"), nullable=True)
    acao = Column(String(100), nullable=False)
    detalhes = Column(Text, nullable=True)
    timestamp = Column(DateTime, default=datetime.utcnow)

    media_item = relationship("MediaItem", back_populates="audit_logs")


try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

class DatabaseManager:
    def __init__(self, db_url: Optional[str] = None):
        if not db_url:
            db_url = os.getenv("DATABASE_URL")

        # Garante o uso do psycopg2 caso a string seja apenas postgresql:// (necessário no SQLAlchemy 2.1+)
        if db_url and db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

        if not db_url:
            # Fallback para SQLite local
            raiz_proj = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_file = os.path.join(raiz_proj, "data", "glicovoz.db")
            os.makedirs(os.path.dirname(db_file), exist_ok=True)
            db_url = f"sqlite:///{db_file}"

        self.db_url = db_url
        connect_args = {}
        if db_url.startswith("sqlite"):
            connect_args = {"check_same_thread": False}

        self.engine = create_engine(self.db_url, connect_args=connect_args, pool_pre_ping=True)
        self.session_factory = sessionmaker(bind=self.engine)
        self.Session = scoped_session(self.session_factory)
        self._init_db()

    def _init_db(self):
        Base.metadata.create_all(self.engine)

    def get_session(self):
        return self.Session()

    def get_or_create_patient(self, patient_id: str) -> Patient:
        session = self.get_session()
        try:
            p = session.query(Patient).filter_by(id=patient_id).first()
            if not p:
                p = Patient(id=patient_id)
                session.add(p)
                session.commit()
            return p
        finally:
            session.close()

    def register_media_item(
        self, patient_id: str, original_path: str, audio_path: Optional[str] = None
    ) -> MediaItem:
        self.get_or_create_patient(patient_id)
        session = self.get_session()
        try:
            nome_arq = os.path.basename(original_path)
            stem_base = nome_arq.split("+")[0].strip()

            # Procura por caminho exato
            item = session.query(MediaItem).filter_by(original_path=original_path).first()

            # Se não achou por caminho exato, procura por paciente + stem base (caso tenha sido renomeado)
            if not item:
                candidate_items = session.query(MediaItem).filter_by(patient_id=patient_id).all()
                for cand in candidate_items:
                    cand_stem = os.path.basename(cand.original_path).split("+")[0].strip()
                    if cand_stem == stem_base:
                        # Verifica se original_path já pertence a outro item
                        outro = session.query(MediaItem).filter(
                            MediaItem.original_path == original_path,
                            MediaItem.id != cand.id
                        ).first()
                        if not outro:
                            item = cand
                            item.original_path = original_path
                            if audio_path:
                                item.audio_path = audio_path
                        break

            # Detecta se a imagem já possui um valor de glicemia rotulado no próprio nome (ex: +142.jpg)
            match_rot = re.search(r"\+\s*(\d+)", nome_arq)
            valor_no_nome = match_rot.group(1) if match_rot else None

            if not item:
                initial_status = "APPROVED" if valor_no_nome else "PENDING"
                item = MediaItem(
                    patient_id=patient_id,
                    original_path=original_path,
                    audio_path=audio_path,
                    status=initial_status,
                    valor_final=valor_no_nome,
                    revisado_por="manual" if valor_no_nome else None
                )
                session.add(item)
                session.commit()
                session.refresh(item)
            else:
                # Se o arquivo foi renomeado no disco com +VALOR, sincroniza para APPROVED no banco
                if valor_no_nome:
                    item.valor_final = valor_no_nome
                    if item.status in ["PENDING", "HUMAN_REVIEW"]:
                        item.status = "APPROVED"
                        item.revisado_por = item.revisado_por or "manual"
                if audio_path and item.audio_path != audio_path:
                    item.audio_path = audio_path
                session.commit()

            return item
        except Exception:
            session.rollback()
            # Retorna item por busca simples se o commit falhou por corrida
            item = session.query(MediaItem).filter_by(original_path=original_path).first()
            return item
        finally:
            session.close()

    def sync_db_with_disk_and_labels(self):
        """
        Sincroniza status e valor_final no banco com base no nome do arquivo no disco.
        Regra simples: se o arquivo existe e tem +NNN no nome → APPROVED + valor_final = NNN.
        NÃO altera original_path (evita UniqueViolation no PostgreSQL).
        """
        session = self.get_session()
        try:
            items = session.query(MediaItem).filter(
                MediaItem.status.in_(["PENDING", "HUMAN_REVIEW"])
            ).all()

            for item in items:
                if not item.original_path:
                    continue
                if not os.path.exists(item.original_path):
                    continue

                nome_arq = os.path.basename(item.original_path)
                match_rot = re.search(r"\+\s*(\d+)", nome_arq)
                if match_rot:
                    val = match_rot.group(1)
                    item.valor_final = val
                    item.status = "APPROVED"
                    item.revisado_por = item.revisado_por or "manual"

            session.commit()
        except Exception as e:
            session.rollback()
            print(f"[DB Sync Warning] {e}")
        finally:
            session.close()

    def update_media_result(self, item_id: int, updates: Dict[str, Any]):
        session = self.get_session()
        try:
            item = session.query(MediaItem).filter_by(id=item_id).first()
            if item:
                for k, v in updates.items():
                    if hasattr(item, k):
                        setattr(item, k, v)
                item.updated_at = datetime.utcnow()
                session.commit()
        finally:
            session.close()

    def log_audit(self, acao: str, detalhes: Any, media_item_id: Optional[int] = None):
        session = self.get_session()
        try:
            det_str = json.dumps(detalhes, ensure_ascii=False) if isinstance(detalhes, (dict, list)) else str(detalhes)
            log = AuditLog(media_item_id=media_item_id, acao=acao, detalhes=det_str)
            session.add(log)
            session.commit()
        finally:
            session.close()

    def reset_items_to_pending(self, patient_id: Optional[str] = None):
        session = self.get_session()
        try:
            query = session.query(MediaItem)
            if patient_id:
                query = query.filter(MediaItem.patient_id == patient_id)
            for item in query.all():
                item.status = "PENDING"
            session.commit()
        finally:
            session.close()

    def reset_review_to_pending(self, patient_id: Optional[str] = None):
        session = self.get_session()
        try:
            query = session.query(MediaItem).filter(MediaItem.status == "HUMAN_REVIEW")
            if patient_id:
                query = query.filter(MediaItem.patient_id == patient_id)
            for item in query.all():
                item.status = "PENDING"
            session.commit()
        finally:
            session.close()

    def get_pending_items(self, patient_id: Optional[str] = None) -> List[MediaItem]:
        session = self.get_session()
        try:
            query = session.query(MediaItem).filter(MediaItem.status.in_(["PENDING", "PROCESSING"]))
            if patient_id:
                query = query.filter(MediaItem.patient_id == patient_id)
            
            items = query.all()
            # Prioriza PROCESSING antes de PENDING
            items.sort(key=lambda x: 0 if x.status == "PROCESSING" else 1)
            return items
        finally:
            session.close()

    def get_all_patient_ids(self) -> List[str]:
        session = self.get_session()
        try:
            res = session.query(MediaItem.patient_id).distinct().all()
            return sorted([r[0] for r in res if r[0]])
        except Exception:
            return []
        finally:
            session.close()

    def get_patient_gallery(self, patient_id: Optional[str] = None, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        session = self.get_session()
        try:
            query = session.query(MediaItem)
            if patient_id and patient_id.strip():
                clean_p = patient_id.strip()
                query = query.filter(MediaItem.patient_id == clean_p)
            if status_filter and status_filter != "TODOS":
                if status_filter == "ERROR":
                    query = query.filter(MediaItem.status.in_(["ERROR", "PROCESSING"]))
                else:
                    query = query.filter(MediaItem.status == status_filter)

            items = query.order_by(MediaItem.id.asc()).all()

            result = []
            for m in items:
                result.append({
                    "id": m.id,
                    "patient_id": m.patient_id,
                    "original_path": m.original_path,
                    "crop_path": m.crop_path,
                    "audio_path": m.audio_path,
                    "status": m.status,
                    "valor_final": m.valor_final,
                    "revisado_por": m.revisado_por or ("auto" if m.status == "APPROVED" else "pendente"),
                    "valor_principal": m.valor_principal,
                    "status_principal": m.status_principal,
                    "valor_secundario": m.valor_secundario,
                    "status_secundario": m.status_secundario,
                    "yolo_conf": m.yolo_conf,
                    "tempo_ms": m.tempo_ms,
                    "updated_at": m.updated_at.strftime("%d/%m/%Y %H:%M") if m.updated_at else ""
                })
            return result
        finally:
            session.close()

    def get_review_queue(self, filter_type: str = "HUMAN_REVIEW") -> List[Dict[str, Any]]:
        session = self.get_session()
        try:
            query = session.query(MediaItem)
            if filter_type == "AUTO_APPROVED":
                query = query.filter(MediaItem.status == "APPROVED", MediaItem.revisado_por == "auto")
            elif filter_type == "HUMAN_REVIEW":
                query = query.filter(MediaItem.status == "HUMAN_REVIEW")
            elif filter_type == "ALL_APPROVED":
                query = query.filter(MediaItem.status == "APPROVED")
            
            items = query.order_by(MediaItem.id.asc()).all()
            result = []
            for m in items:
                result.append({
                    "id": m.id,
                    "patient_id": m.patient_id,
                    "original_path": m.original_path,
                    "crop_path": m.crop_path,
                    "audio_path": m.audio_path,
                    "valor_principal": m.valor_principal,
                    "status_principal": m.status_principal,
                    "raw_principal": m.raw_principal,
                    "valor_secundario": m.valor_secundario,
                    "status_secundario": m.status_secundario,
                    "raw_secundario": m.raw_secundario,
                    "valor_final": m.valor_final,
                    "revisado_por": m.revisado_por,
                    "yolo_conf": m.yolo_conf,
                    "created_at": m.created_at.isoformat() if m.created_at else None
                })
            return result
        finally:
            session.close()

    def approve_human_review(self, item_id: int, valor_correto: str):
        session = self.get_session()
        try:
            item = session.query(MediaItem).filter_by(id=item_id).first()
            if item:
                item.valor_final = valor_correto
                item.status = "APPROVED"
                item.revisado_por = "humano"
                item.updated_at = datetime.utcnow()
                session.commit()
                self.log_audit("HUMAN_APPROVE", {"valor": valor_correto}, media_item_id=item_id)
        finally:
            session.close()

    def delete_media_item(self, item_id: int):
        session = self.get_session()
        try:
            item = session.query(MediaItem).filter_by(id=item_id).first()
            if item:
                session.delete(item)
                session.commit()
                self.log_audit("DELETE_ITEM", {"id": item_id})
        finally:
            session.close()

    def get_metrics(self) -> Dict[str, Any]:
        session = self.get_session()
        try:
            # Otmização: Busca apenas as colunas necessárias em vez de instanciar objetos ORM completos
            items = session.query(
                MediaItem.status,
                MediaItem.revisado_por,
                MediaItem.tempo_ms,
                MediaItem.valor_final,
                MediaItem.valor_principal,
                MediaItem.valor_secundario
            ).all()
            total = len(items)
            approved = sum(1 for m in items if m.status == "APPROVED")
            approved_ia = sum(1 for m in items if m.status == "APPROVED" and m.revisado_por == "auto")
            approved_humano = sum(1 for m in items if m.status == "APPROVED" and m.revisado_por in ["humano", "manual"])
            review = sum(1 for m in items if m.status == "HUMAN_REVIEW")
            pending = sum(1 for m in items if m.status == "PENDING")
            errors = sum(1 for m in items if m.status == "ERROR")
            tempos = [m.tempo_ms for m in items if m.tempo_ms and m.tempo_ms > 0]

            # Novos contadores de auditoria (HITL Model Accuracy)
            human_reviewed = [m for m in items if m.status == "APPROVED" and m.revisado_por == "humano"]
            total_corrigidos = len(human_reviewed)
            acertos_principal = 0
            acertos_secundario = 0
            ambos_erraram_discordancia = 0
            erros_criticos_mutuos = 0
            
            for m in human_reviewed:
                acertou_p = (m.valor_final == m.valor_principal) if m.valor_principal else False
                acertou_s = (m.valor_final == m.valor_secundario) if m.valor_secundario else False
                
                if acertou_p:
                    acertos_principal += 1
                if acertou_s:
                    acertos_secundario += 1
                
                if not acertou_p and not acertou_s:
                    # Ambos modelos erraram
                    if m.valor_principal and m.valor_secundario and m.valor_principal == m.valor_secundario:
                        # ERRO CRÍTICO MÚTUO: Ambos concordaram em um número errado!
                        erros_criticos_mutuos += 1
                    else:
                        ambos_erraram_discordancia += 1

            return {
                "total_processado": total,
                "aprovados": approved,
                "aprovados_ia": approved_ia,
                "aprovados_humano": approved_humano,
                "revisao_humana": review,
                "pendentes": pending,
                "erros": errors,
                "taxa_auto_aceitacao_pct": round((approved_ia / total * 100), 2) if total > 0 else 0.0,
                "taxa_revisao_humana_pct": round((review / total * 100), 2) if total > 0 else 0.0,
                "tempo_medio_ms": round(sum(tempos) / len(tempos), 2) if tempos else 0.0,
                "hitl_total_corrigidos": total_corrigidos,
                "hitl_acertos_principal": acertos_principal,
                "hitl_acertos_secundario": acertos_secundario,
                "hitl_ambos_erraram": ambos_erraram_discordancia,
                "hitl_erros_criticos_mutuos": erros_criticos_mutuos,
                "db_engine": str(self.engine.url)
            }
        finally:
            session.close()


# Instância global Singleton
db = DatabaseManager()

"""
tests/test_pipeline.py

Testes unitários dos módulos reais do pipeline: VLM parser, detector fallback,
database CRUD e lógica de consenso do pipeline.
"""

import os
import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# Assegura que a raiz do projeto está no path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


class TestVLMParser(unittest.TestCase):
    """Testa a extração de valores de glicemia das respostas brutas dos VLMs."""

    def setUp(self):
        from api.vlm import VLMOpinionEngine
        self.engine = VLMOpinionEngine(primary_model="mock", secondary_model="mock")

    def test_parse_bracket_format(self):
        """Formato esperado [[142]] é extraído corretamente."""
        val, status = self.engine.parse_vlm_response("[[142]]")
        self.assertEqual(val, "142")
        self.assertEqual(status, "SUCCESS")

    def test_parse_bracket_with_noise(self):
        """Brackets com texto ao redor são extraídos."""
        val, status = self.engine.parse_vlm_response("The reading is [[95]].")
        self.assertEqual(val, "95")
        self.assertEqual(status, "SUCCESS")

    def test_parse_bare_number(self):
        """Número isolado (sem brackets) é extraído."""
        val, status = self.engine.parse_vlm_response("142")
        self.assertEqual(val, "142")
        self.assertEqual(status, "SUCCESS")

    def test_parse_help_response(self):
        """Resposta de ajuda (ilegível) retorna None."""
        val, status = self.engine.parse_vlm_response("[[HELP]]")
        self.assertIsNone(val)
        self.assertEqual(status, "HELP")

    def test_parse_empty_response(self):
        """Resposta vazia retorna None."""
        val, status = self.engine.parse_vlm_response("")
        self.assertIsNone(val)
        self.assertEqual(status, "HELP")

    def test_parse_none_response(self):
        """Resposta None retorna None."""
        val, status = self.engine.parse_vlm_response(None)
        self.assertIsNone(val)
        self.assertEqual(status, "HELP")

    def test_parse_out_of_range_high(self):
        """Valor > 600 é rejeitado (fora do range fisiológico)."""
        val, status = self.engine.parse_vlm_response("[[750]]")
        self.assertIsNone(val)

    def test_parse_out_of_range_low(self):
        """Valor < 20 é rejeitado."""
        val, status = self.engine.parse_vlm_response("[[15]]")
        self.assertIsNone(val)

    def test_parse_multiple_numbers_disambiguates_clock(self):
        """Quando há múltiplos números, descarta candidatos <= 59 (horas/minutos)."""
        val, status = self.engine.parse_vlm_response("08 30 142")
        self.assertEqual(val, "142")
        self.assertEqual(status, "SUCCESS")

    def test_parse_sorry_response(self):
        """Respostas com 'sorry' são tratadas como HELP."""
        val, status = self.engine.parse_vlm_response("I'm sorry, I cannot read this image.")
        self.assertIsNone(val)
        self.assertEqual(status, "HELP")


class TestDetectorFallback(unittest.TestCase):
    """Testa que o detector retorna bounding box mesmo sem modelo YOLO."""

    def test_fallback_returns_valid_bbox(self):
        """Sem YOLO, o detector retorna margem de 10% como fallback."""
        from api.detector import GlucometerDetector
        detector = GlucometerDetector(model_path="/nonexistent/model.pt")

        from PIL import Image
        fake_img = Image.new("RGB", (400, 300), color="white")
        x1, y1, x2, y2, conf = detector.detect(fake_img)

        self.assertGreater(x2 - x1, 0)
        self.assertGreater(y2 - y1, 0)
        self.assertGreaterEqual(conf, 0.0)
        # Fallback deve cobrir ~80% da imagem (margem de 10% em cada lado)
        self.assertAlmostEqual(x1, 40, delta=5)
        self.assertAlmostEqual(y1, 30, delta=5)

    def test_detect_none_image(self):
        """Imagem None retorna bbox zerada."""
        from api.detector import GlucometerDetector
        detector = GlucometerDetector(model_path="/nonexistent/model.pt")
        x1, y1, x2, y2, conf = detector.detect(None)
        self.assertEqual((x1, y1, x2, y2), (0, 0, 0, 0))


class TestDatabaseCRUD(unittest.TestCase):
    """Testa operações CRUD do DatabaseManager com SQLite in-memory."""

    def setUp(self):
        from api.database import DatabaseManager
        self.db = DatabaseManager(db_url="sqlite:///:memory:")

    def test_create_patient(self):
        """Paciente é criado ao registrar mídia."""
        self.db.register_media_item("P1_TEST", "/fake/path/test.jpg")
        patients = self.db.get_all_patient_ids()
        self.assertIn("P1_TEST", patients)

    def test_register_media_item(self):
        item = self.db.register_media_item(
            patient_id="P1_TEST",
            original_path="/fake/path/img.jpg"
        )
        self.assertIsNotNone(item)
        self.assertEqual(item.patient_id, "P1_TEST")
        self.assertEqual(item.status, "PENDING")

    def test_register_prelabeled_item(self):
        """Arquivo com +142 no nome já entra como APPROVED."""
        item = self.db.register_media_item(
            patient_id="P1_TEST",
            original_path="/fake/path/img +142.jpg"
        )
        self.assertEqual(item.status, "APPROVED")
        self.assertEqual(item.valor_final, "142")

    def test_approve_human_review(self):
        item = self.db.register_media_item(
            patient_id="P1_TEST",
            original_path="/fake/path/img2.jpg"
        )
        self.db.update_media_result(item.id, {"status": "HUMAN_REVIEW"})
        self.db.approve_human_review(item.id, "135")

        gallery = self.db.get_patient_gallery(patient_id="P1_TEST")
        approved = [g for g in gallery if g["status"] == "APPROVED"]
        self.assertEqual(len(approved), 1)
        self.assertEqual(approved[0]["valor_final"], "135")
        self.assertEqual(approved[0]["revisado_por"], "humano")

    def test_get_metrics(self):
        self.db.register_media_item("P1_TEST", "/fake/1.jpg")
        self.db.register_media_item("P1_TEST", "/fake/2 +100.jpg")
        metrics = self.db.get_metrics()
        self.assertEqual(metrics["total_processado"], 2)
        self.assertEqual(metrics["aprovados"], 1)
        self.assertEqual(metrics["pendentes"], 1)

    def test_delete_media_item(self):
        item = self.db.register_media_item("P1_TEST", "/fake/del.jpg")
        self.db.delete_media_item(item.id)
        gallery = self.db.get_patient_gallery(patient_id="P1_TEST")
        self.assertEqual(len(gallery), 0)

    def test_get_pending_items(self):
        self.db.register_media_item("P1_TEST", "/fake/a.jpg")
        self.db.register_media_item("P1_TEST", "/fake/b.jpg")
        pending = self.db.get_pending_items("P1_TEST")
        self.assertEqual(len(pending), 2)

    def test_audit_log(self):
        """Log de auditoria é criado ao aprovar item por humano."""
        item = self.db.register_media_item("P1_TEST", "/fake/audit.jpg")
        self.db.approve_human_review(item.id, "200")

        session = self.db.get_session()
        from api.database import AuditLog
        logs = session.query(AuditLog).filter_by(media_item_id=item.id).all()
        self.assertGreater(len(logs), 0)
        self.assertEqual(logs[0].acao, "HUMAN_APPROVE")
        session.close()


class TestCandidateModelNames(unittest.TestCase):
    """Testa a resolução fuzzy de nomes de modelos Ollama."""

    def test_qwen_variants_generated(self):
        from api.vlm import get_candidate_model_names
        candidates = get_candidate_model_names("qwen2.5-vl:7b")
        self.assertIn("qwen2.5-vl:7b", candidates)
        # Deve gerar variantes alternativas
        self.assertTrue(len(candidates) > 1)

    def test_minicpm_variants(self):
        from api.vlm import get_candidate_model_names
        candidates = get_candidate_model_names("minicpm-v")
        self.assertIn("minicpm-v", candidates)

    def test_empty_model_returns_defaults(self):
        from api.vlm import get_candidate_model_names
        candidates = get_candidate_model_names("")
        self.assertTrue(len(candidates) > 0)


if __name__ == "__main__":
    unittest.main()

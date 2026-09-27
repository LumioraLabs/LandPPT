"""Regression tests: failures must not be reported as successes.

Across the generation pipeline the fallback paths used to persist fabricated or
broken content, mark the stage completed, bill the user and return "✅ 完成".
"""

import ast
import os
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "landppt"


def _read(relative: str) -> str:
    return (SRC / relative).read_text(encoding="utf-8")


class TestOutlineRepairFailsLoudly:
    def test_repair_raises_instead_of_returning_invalid_outline(self):
        source = _read("services/outline/project_outline_repair_service.py")
        assert "OutlineRepairFailedError" in source

        marker = "AI修复达到最大尝试次数"
        assert marker in source
        tail = source[source.index(marker) :][:600]
        assert "raise OutlineRepairFailedError" in tail
        assert "return outline_data" not in tail, (
            "returning the invalid outline let callers persist it as a success"
        )

    def test_error_carries_the_validation_errors(self):
        from landppt.services.outline.outline_errors import OutlineRepairFailedError

        error = OutlineRepairFailedError(["缺少必需字段: slides", "页数不足"])
        assert error.validation_errors == ["缺少必需字段: slides", "页数不足"]
        assert "缺少必需字段: slides" in str(error)

    def test_empty_error_list_still_produces_a_message(self):
        from landppt.services.outline.outline_errors import OutlineRepairFailedError

        assert str(OutlineRepairFailedError([]))


class TestPlaceholderOutlineIsGone:
    def test_no_fabricated_three_page_outline_on_parse_failure(self):
        source = _read("services/outline/project_outline_page_count_service.py")
        assert "'内容要点1'" not in source, "placeholder outline still fabricated"
        assert "'谢谢观看'" not in source, "placeholder outline still fabricated"
        # The old code returned "✅ PPT大纲生成完成！（使用备用方案）" for a failure.
        assert "✅ PPT大纲生成完成！（使用备用方案）" not in source

    def test_parse_failure_marks_the_stage_failed(self):
        source = _read("services/outline/project_outline_page_count_service.py")
        assert "_mark_outline_generation_failed" in source
        marker = "async def _mark_outline_generation_failed"
        body = source[source.index(marker) :][:900]
        assert "'failed'" in body
        assert "'outline_generation'" in body

    def test_parse_failure_returns_an_error_string(self):
        source = _read("services/outline/project_outline_page_count_service.py")
        assert "❌ 大纲生成失败：模型返回的内容无法解析" in source


class TestStreamingOutlineFailureHandling:
    def test_save_failure_does_not_emit_done(self):
        source = _read("services/outline/project_outline_streaming_service.py")
        marker = "if not save_success:"
        assert marker in source
        body = source[source.index(marker) :][:600]
        assert "'error'" in body
        assert "return" in body
        assert "'done': True" not in body

    def test_streaming_marks_stage_failed(self):
        source = _read("services/outline/project_outline_streaming_service.py")
        assert "async def _mark_streaming_outline_failed" in source
        assert "_mark_streaming_outline_failed(project_id" in source

    def test_json_path_normalises_before_validating(self):
        """Avoids burning 10 LLM repair calls on locally-fixable variants."""
        source = _read("services/outline/project_outline_streaming_service.py")
        marker = "structured_outline = json.loads(json_str)"
        body = source[source.index(marker) :][:700]
        assert "_standardize_outline_format" in body
        assert body.index("_standardize_outline_format") < body.index(
            "_validate_and_repair_outline_json"
        )


class TestSlideFailuresAreNotBilledOrCompleted:
    def test_failed_indices_are_tracked_separately(self):
        source = _read("services/slide/slide_generation_service.py")
        assert "failed_slide_indices: set[int] = set()" in source

    def test_error_slides_do_not_enter_generated_indices(self):
        source = _read("services/slide/slide_generation_service.py")
        # Parallel path
        marker = "if generation_failed:\n                                        failed_slide_indices.add(idx)"
        assert marker in source
        # Sequential path
        assert "failed_slide_indices.add(idx)\n                                        await db_manager.save_single_slide" in source

    def test_billing_quantity_uses_generated_only(self):
        source = _read("services/slide/slide_generation_service.py")
        commit = source[source.index("commit_reservation(") :][:300]
        assert "len(generated_slide_indices)" in commit
        assert "failed_slide_indices" not in commit

    def test_stage_is_not_completed_when_pages_failed(self):
        source = _read("services/slide/slide_generation_service.py")
        assert "has_failures = bool(failed_slide_indices)" in source
        marker = "if has_failures:"
        assert marker in source
        body = source[source.index(marker) :][:1200]
        assert '"failed"' in body
        assert "failed_pages" in body

    def test_completion_message_reports_partial_failure(self):
        source = _read("services/slide/slide_generation_service.py")
        assert "'partial': True" in source
        assert "页生成失败，可重新生成这些页面" in source

    def test_project_status_not_completed_on_failure(self):
        source = _read("services/slide/slide_generation_service.py")
        assert 'project.status = "in_progress" if has_failures else "completed"' in source


class TestFailedSlidesAreRegenerated:
    def test_skip_check_ignores_failed_slides(self):
        source = _read("services/slide/slide_generation_service.py")
        assert "existing_slide.get('generation_failed')" in source
        marker = "existing_slide.get('generation_failed')"
        body = source[source.index(marker) - 300 : source.index(marker) + 500]
        assert "existing_slide = None" in body

    def test_needs_generation_ignores_failed_slides(self):
        source = _read("services/slide/slide_streaming_service.py")
        marker = "existing_indices = {"
        body = source[source.index(marker) :][:700]
        assert 'get("generation_failed")' in body

    def test_failure_flag_round_trips_through_the_database(self):
        service_source = _read("database/service.py")
        assert '"generation_failed"' in service_source
        assert '"generation_error"' in service_source

        manager_source = _read("services/db_project_manager.py")
        assert "_slide_row_to_payload" in manager_source
        body = manager_source[manager_source.index("def _slide_row_to_payload") :][:1200]
        assert 'payload["generation_failed"] = True' in body

    def test_successful_regeneration_clears_the_stale_flag(self):
        source = _read("database/service.py")
        marker = "A slide that regenerated successfully must lose the stale failure marker."
        assert marker in source
        body = source[source.index(marker) :][:400]
        assert 'metadata.pop("generation_failed", None)' in body


class TestBinaryFilesAreRejected:
    @staticmethod
    def _load_reader():
        source = _read("services/enhanced_ppt_service.py")
        tree = ast.parse(source)
        wanted = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in (
                "_read_file_with_fallback_encoding",
                "_looks_like_binary_text",
            ):
                node.decorator_list = []
                wanted[node.name] = node
        assert len(wanted) == 2

        from typing import Optional

        namespace = {"Optional": Optional}
        module = ast.Module(
            body=[wanted["_looks_like_binary_text"], wanted["_read_file_with_fallback_encoding"]],
            type_ignores=[],
        )
        exec(compile(ast.fix_missing_locations(module), "<reader>", "exec"), namespace)

        class Holder:
            _looks_like_binary_text = staticmethod(namespace["_looks_like_binary_text"])
            _read_file_with_fallback_encoding = namespace["_read_file_with_fallback_encoding"]

        return Holder()

    def test_latin1_catch_all_is_gone(self):
        source = _read("services/enhanced_ppt_service.py")
        assert "encoding='latin-1'" not in source, (
            "latin-1 never fails, so binary files became mojibake outlines"
        )

    def test_utf8_text_is_read(self):
        holder = self._load_reader()
        path = tempfile.mktemp(suffix=".txt")
        try:
            Path(path).write_text("# Title\n\n中文内容\n", encoding="utf-8")
            assert "中文内容" in holder._read_file_with_fallback_encoding(path)
        finally:
            os.unlink(path)

    def test_binary_pdf_raises_instead_of_mojibake(self):
        holder = self._load_reader()
        path = tempfile.mktemp(suffix=".pdf")
        try:
            Path(path).write_bytes(
                b"%PDF-1.4\n\x00\x01\x02\x03\xff\xfe" + bytes(range(0, 32)) * 40
            )
            with pytest.raises(UnicodeDecodeError):
                holder._read_file_with_fallback_encoding(path)
        finally:
            os.unlink(path)

    def test_binary_heuristic_flags_control_character_soup(self):
        holder = self._load_reader()
        assert holder._looks_like_binary_text("\x00\x01\x02") is True
        assert holder._looks_like_binary_text("normal text\nwith\ttabs\r\n") is False
        assert holder._looks_like_binary_text("") is False


class TestSummeryfileNormalisationIsResilient:
    def test_one_bad_slide_does_not_discard_the_outline(self):
        source = _read("services/outline/project_outline_research_service.py")
        assert "Skipping malformed summeryfile slide" in source
        assert "'演示标题', '演示者', '日期'" not in source, (
            "1-page dummy outline was returned as a successful result"
        )

    def test_total_failure_raises(self):
        source = _read("services/outline/project_outline_research_service.py")
        assert "summeryanyfile 返回的所有幻灯片都无法解析" in source

    def test_thankyou_type_mapping_is_correct(self):
        source = _read("services/outline/project_outline_research_service.py")
        marker = "type_mapping = {"
        body = source[source.index(marker) :][:400]
        assert "'thankyou': 'thankyou'" in body
        assert "'conclusion': 'conclusion'" in body
        assert "'conclusion': 'thankyou'" not in body


class TestFileOutlineFallbackReportsFailure:
    def test_fallback_failure_returns_success_false(self):
        source = _read("services/outline/outline_workflow_service.py")
        marker = "Fallback file-outline generation also failed"
        assert marker in source
        body = source[source.index(marker) :][:700]
        assert "success=False" in body

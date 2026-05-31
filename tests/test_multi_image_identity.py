"""Regression tests for non-lossy multi-image CSV and analysis identity."""

from __future__ import annotations

import csv
import re
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from app import create_app
from app.analyzer.core import ForensicAnalyzer
from app.analyzer.utils import build_scoped_artifact_stem
from app.chat.manager import ChatManager
from app.routes.evidence import rebuild_case_parse_artifacts
from app.case_logging import unregister_all_case_log_handlers
from tests.conftest import FakeParser, FakeProvider, ImmediateThread, FAKE_HASHES

import app.routes.evidence as routes_evidence
import app.routes.handlers as routes_handlers
import app.routes.images as routes_images
import app.routes.state as routes_state
import app.routes.tasks as routes_tasks


def _write_csv(csv_path: Path, marker: str) -> None:
    """Write a small artifact CSV with a unique marker.

    Args:
        csv_path: Destination CSV path.
        marker: Row value used to distinguish the file.
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ts", "event"])
        writer.writeheader()
        writer.writerow({"ts": "2026-01-01T00:00:00Z", "event": marker})


def _make_image(tmp_path: Path, image_id: str, label: str) -> dict[str, Any]:
    """Create a multi-image analyzer descriptor with one runkeys CSV.

    Args:
        tmp_path: Test workspace.
        image_id: Image identifier to place in the descriptor.
        label: Human-readable image label.

    Returns:
        A descriptor accepted by ``run_multi_image_analysis``.
    """
    parsed_dir = tmp_path / "images" / image_id / "parsed"
    _write_csv(parsed_dir / "runkeys.csv", f"event-{image_id}")
    return {
        "image_id": image_id,
        "label": label,
        "metadata": {"hostname": label, "os_version": "Windows 11"},
        "artifact_keys": ["runkeys"],
        "parsed_dir": str(parsed_dir),
        "artifact_csv_paths": {"runkeys": str(parsed_dir / "runkeys.csv")},
    }


def _make_analyzer(tmp_path: Path, provider: FakeProvider) -> ForensicAnalyzer:
    """Create a configured analyzer using a fake provider.

    Args:
        tmp_path: Case directory for analyzer outputs.
        provider: Fake provider instance to attach.

    Returns:
        A ``ForensicAnalyzer`` ready for test analysis.
    """
    config = {
        "ai": {
            "provider": "local",
            "local": {
                "base_url": "http://localhost/v1",
                "model": "test",
                "api_key": "x",
            },
        },
        "analysis": {"ai_max_tokens": 128000},
    }
    analyzer = ForensicAnalyzer(
        case_dir=tmp_path,
        config=config,
        prompts_dir=Path(__file__).resolve().parents[1] / "prompts",
    )
    analyzer.ai_provider = provider
    analyzer.model_info = provider.get_model_info()
    return analyzer


class AttachmentTrackingProvider(FakeProvider):
    """Fake provider that records CSV attachments for assertions.

    Attributes:
        attachments: Attachments passed to each provider call.
    """

    def __init__(self) -> None:
        """Initialize canned responses and attachment capture."""
        super().__init__(responses=["artifact-a", "artifact-b", "sum-a", "sum-b", "cross"] * 3)
        self.attachments: list[list[dict[str, str]]] = []

    def analyze_with_attachments(
        self,
        system_prompt: str,
        user_prompt: str,
        attachments: list[dict[str, str]] | None,
        max_tokens: int = 4096,
    ) -> str:
        """Record attachments and delegate to ``analyze``.

        Args:
            system_prompt: System prompt text.
            user_prompt: User prompt text.
            attachments: Attachment descriptors supplied by the analyzer.
            max_tokens: Maximum response token count.

        Returns:
            The next canned fake response.
        """
        self.attachments.append(list(attachments or []))
        return self.analyze(system_prompt, user_prompt, max_tokens)


def test_case_parse_aggregate_preserves_same_artifact_per_image(tmp_path: Path) -> None:
    """Case parse aggregate keeps duplicate artifact CSVs per image."""
    img1_csv = tmp_path / "img1" / "parsed" / "runkeys.csv"
    img2_csv = tmp_path / "img2" / "parsed" / "runkeys.csv"
    _write_csv(img1_csv, "img1")
    _write_csv(img2_csv, "img2")
    case: dict[str, Any] = {
        "image_states": {
            "img1": {
                "parse_results": [{"artifact_key": "runkeys", "success": True, "csv_path": str(img1_csv)}],
                "artifact_csv_paths": {"runkeys": str(img1_csv)},
                "analysis_artifacts": ["runkeys"],
                "csv_output_dir": str(img1_csv.parent),
            },
            "img2": {
                "parse_results": [{"artifact_key": "runkeys", "success": True, "csv_path": str(img2_csv)}],
                "artifact_csv_paths": {"runkeys": str(img2_csv)},
                "analysis_artifacts": ["runkeys"],
                "csv_output_dir": str(img2_csv.parent),
            },
        },
        "analysis_artifacts": ["stale"],
        "artifact_options": [{"artifact_key": "stale", "mode": "parse_and_ai"}],
        "artifact_csv_paths": {"stale": "old.csv"},
        "parse_results": [{"artifact_key": "stale", "success": True}],
    }

    aggregate = rebuild_case_parse_artifacts(case)

    nested = aggregate["image_artifact_csv_paths"]
    assert nested["img1"]["runkeys"] == str(img1_csv)
    assert nested["img2"]["runkeys"] == str(img2_csv)
    assert set(case["image_artifact_csv_paths"]) == {"img1", "img2"}
    assert "parse_results" not in aggregate
    assert "artifact_csv_paths" not in aggregate
    assert "parse_results" not in case
    assert "artifact_csv_paths" not in case
    assert "analysis_artifacts" not in case
    assert "artifact_options" not in case


def test_case_parse_aggregate_ignores_cancelled_image_without_csv(tmp_path: Path) -> None:
    """A cancelled image without CSVs does not erase successful image paths."""
    img1_csv = tmp_path / "img1" / "parsed" / "runkeys.csv"
    _write_csv(img1_csv, "img1")
    case: dict[str, Any] = {
        "image_states": {
            "img1": {
                "parse_results": [{"artifact_key": "runkeys", "success": True, "csv_path": str(img1_csv)}],
                "artifact_csv_paths": {"runkeys": str(img1_csv)},
                "analysis_artifacts": ["runkeys"],
                "csv_output_dir": str(img1_csv.parent),
            },
            "img2": {
                "parse_results": [],
                "artifact_csv_paths": {},
                "analysis_artifacts": ["runkeys"],
                "status": "cancelled",
            },
        },
        "selected_artifacts": ["stale"],
        "csv_output_dir": "stale-dir",
    }

    aggregate = rebuild_case_parse_artifacts(case)

    assert set(aggregate["image_artifact_csv_paths"]) == {"img1"}
    assert "artifact_csv_paths" not in aggregate
    assert "selected_artifacts" not in case
    assert "csv_output_dir" not in case


def test_sanitized_image_id_collisions_get_hashed_artifact_outputs(tmp_path: Path) -> None:
    """Sanitization-colliding image IDs produce distinct scoped outputs."""
    provider = AttachmentTrackingProvider()
    analyzer = _make_analyzer(tmp_path, provider)
    first = _make_image(tmp_path, "img/one", "First")
    second = _make_image(tmp_path, "img one", "Second")

    result = analyzer.run_multi_image_analysis(
        images=[first, second],
        investigation_context="Check persistence.",
    )

    assert set(result["images"]) == {"img_one", "img_one_2"}
    prompt_names = {path.name for path in (tmp_path / "prompts").glob("artifact_*.md")}
    assert any(re.fullmatch(r"artifact_img_one__[0-9a-f]{10}__runkeys\.md", name) for name in prompt_names)
    assert any(re.fullmatch(r"artifact_img_one_2__[0-9a-f]{10}__runkeys\.md", name) for name in prompt_names)
    attachment_names = {
        attachment["name"]
        for call in provider.attachments
        for attachment in call
    }
    assert any(re.fullmatch(r"img_one__[0-9a-f]{10}__runkeys\.csv", name) for name in attachment_names)
    assert any(re.fullmatch(r"img_one_2__[0-9a-f]{10}__runkeys\.csv", name) for name in attachment_names)


def test_scoped_artifact_stems_do_not_collide_on_separator_boundaries() -> None:
    """Scoped artifact stems remain distinct for delimiter-like IDs."""
    stems = {
        build_scoped_artifact_stem(None, "a__b__c"),
        build_scoped_artifact_stem("a", "b__c"),
        build_scoped_artifact_stem("a__b", "c"),
        build_scoped_artifact_stem("a_", "b"),
        build_scoped_artifact_stem("a", "_b"),
    }

    assert len(stems) == 5


def test_repeated_multi_image_analysis_clears_scoped_csv_lookup(tmp_path: Path) -> None:
    """A second analyzer run does not retain first-run citation CSV keys."""
    provider = AttachmentTrackingProvider()
    analyzer = _make_analyzer(tmp_path, provider)
    first = _make_image(tmp_path, "first", "First")
    second = _make_image(tmp_path, "second", "Second")
    later = _make_image(tmp_path, "later", "Later")

    analyzer.run_multi_image_analysis(
        images=[first, second],
        investigation_context="First run.",
    )
    analyzer.run_multi_image_analysis(
        images=[later],
        investigation_context="Second run.",
    )

    lookup_keys = set(analyzer._analysis_input_csv_paths)
    assert lookup_keys
    assert not any("first" in key or "second" in key for key in lookup_keys)
    assert any("later" in key for key in lookup_keys)


def test_multi_image_payload_rebuilds_from_image_states_when_nested_map_empty(tmp_path: Path) -> None:
    """Empty image CSV aggregate is rebuilt from per-image state."""
    img1_csv = tmp_path / "img1" / "parsed" / "runkeys.csv"
    img2_csv = tmp_path / "img2" / "parsed" / "runkeys.csv"
    _write_csv(img1_csv, "img1")
    _write_csv(img2_csv, "img2")

    payload = routes_tasks.build_multi_image_analysis_payload_from_case({
        "image_artifact_csv_paths": {},
        "image_states": {
            "img1": {
                "artifact_csv_paths": {"runkeys": str(img1_csv)},
                "analysis_artifacts": ["runkeys"],
            },
            "img2": {
                "artifact_csv_paths": {"runkeys": str(img2_csv)},
                "analysis_artifacts": ["runkeys"],
            },
        },
    })

    assert payload == [
        {"image_id": "img1", "artifacts": ["runkeys"]},
        {"image_id": "img2", "artifacts": ["runkeys"]},
    ]


def test_grouped_chat_csv_retrieval_respects_image_alias(tmp_path: Path) -> None:
    """Chat CSV retrieval does not include every image when one is named."""
    img1_csv = tmp_path / "img1" / "runkeys.csv"
    img1_other_csv = tmp_path / "img1" / "prefetch.csv"
    img2_csv = tmp_path / "img2" / "runkeys.csv"
    _write_csv(img1_csv, "only-img1")
    _write_csv(img1_other_csv, "other-img1")
    _write_csv(img2_csv, "only-img2")

    result = ChatManager._retrieve_grouped_csv_data(
        question="Show PC01 runkeys CSV rows",
        csv_path_groups=[
            ("img1", "PC01", [img1_csv, img1_other_csv]),
            ("img2", "PC02", [img2_csv]),
        ],
    )

    assert result["retrieved"] is True
    assert result["artifacts"] == ["PC01/runkeys.csv"]
    assert "only-img1" in result["data"]
    assert "other-img1" not in result["data"]
    assert "only-img2" not in result["data"]


class RecordingMultiImageAnalyzer:
    """Analyzer fake that records multi-image descriptors from routes_state.

    Attributes:
        last_images: Image descriptors from the most recent run.
    """

    last_images: list[dict[str, Any]] = []

    def __init__(self, **_kwargs: Any) -> None:
        """Accept route-supplied constructor arguments.

        Args:
            **_kwargs: Ignored analyzer constructor keyword arguments.
        """

    def run_multi_image_analysis(
        self,
        images: list[dict[str, Any]],
        investigation_context: str,
        progress_callback: Any | None = None,
        cancel_check: Any | None = None,
        analysis_date_range: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Record image descriptors and return route-compatible results.

        Args:
            images: Image descriptors built by the route task.
            investigation_context: Analyst prompt text.
            progress_callback: Optional progress callback.
            cancel_check: Optional cancellation callback.
            analysis_date_range: Optional date range tuple.

        Returns:
            A minimal multi-image analysis result.
        """
        del investigation_context, progress_callback, cancel_check, analysis_date_range
        type(self).last_images = [dict(image) for image in images]
        return {
            "images": {
                str(image["image_id"]): {
                    "label": str(image.get("label", image["image_id"])),
                    "per_artifact": [
                        {
                            "artifact_key": "runkeys",
                            "artifact_name": "runkeys",
                            "analysis": f"analysis for {image['image_id']}",
                            "model": "fake-model",
                        }
                    ],
                    "summary": f"summary for {image['image_id']}",
                    "metadata": dict(image.get("metadata", {})),
                }
                for image in images
            },
            "cross_image_summary": "combined summary",
            "model_info": {"provider": "fake", "model": "fake-model"},
        }


def _patch_route_context(cases_root: Path, report_path: Path) -> ExitStack:
    """Patch route dependencies for synchronous multi-image route tests.

    Args:
        cases_root: Temporary cases root.
        report_path: Fake report path returned by auto-report generation.

    Returns:
        An ``ExitStack`` with all patches applied.
    """
    stack = ExitStack()
    for module in (routes_handlers, routes_images, routes_state):
        stack.enter_context(patch.object(module, "CASES_ROOT", cases_root))
    for module in (routes_tasks, routes_evidence):
        stack.enter_context(patch.object(module, "ForensicParser", FakeParser))
    stack.enter_context(patch.object(routes_evidence, "compute_hashes", return_value=dict(FAKE_HASHES)))
    stack.enter_context(patch("app.parser.ForensicParser", FakeParser))
    stack.enter_context(patch("app.hasher.compute_hashes", return_value=dict(FAKE_HASHES)))
    stack.enter_context(patch.object(routes_images.threading, "Thread", ImmediateThread))
    stack.enter_context(patch.object(routes_tasks, "ForensicAnalyzer", RecordingMultiImageAnalyzer))
    stack.enter_context(
        patch.object(
            routes_tasks,
            "generate_case_report",
            return_value={"success": True, "report_path": report_path},
        )
    )
    return stack


def test_legacy_case_level_analyze_uses_each_image_csv_for_duplicate_artifacts(tmp_path: Path) -> None:
    """Case-level analyze synthesizes image payloads for duplicate artifacts."""
    with TemporaryDirectory(prefix="aift-identity-routes-") as temp_dir:
        cases_root = Path(temp_dir) / "cases"
        app = create_app(str(Path(temp_dir) / "config.yaml"))
        app.testing = True
        client = app.test_client()
        client.environ_base["HTTP_X_CSRF_TOKEN"] = app.config["CSRF_TOKEN"]
        routes_state.CASE_STATES.clear()
        routes_state.PARSE_PROGRESS.clear()
        routes_state.ANALYSIS_PROGRESS.clear()
        routes_state.CHAT_PROGRESS.clear()
        unregister_all_case_log_handlers()
        evidence_one = Path(temp_dir) / "pc01.E01"
        evidence_two = Path(temp_dir) / "pc02.E01"
        evidence_one.write_bytes(b"one")
        evidence_two.write_bytes(b"two")
        RecordingMultiImageAnalyzer.last_images = []

        try:
            with _patch_route_context(cases_root, tmp_path / "report.html"):
                case_id = client.post("/api/cases", json={"case_name": "Identity"}).get_json()["case_id"]
                img1 = client.post(f"/api/cases/{case_id}/images", json={"label": "PC01"}).get_json()["image_id"]
                img2 = client.post(f"/api/cases/{case_id}/images", json={"label": "PC02"}).get_json()["image_id"]
                assert client.post(
                    f"/api/cases/{case_id}/images/{img1}/evidence",
                    json={"path": str(evidence_one)},
                ).status_code == 200
                assert client.post(
                    f"/api/cases/{case_id}/images/{img2}/evidence",
                    json={"path": str(evidence_two)},
                ).status_code == 200
                assert client.post(
                    f"/api/cases/{case_id}/images/{img1}/parse",
                    json={
                        "artifact_options": [
                            {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                        ],
                    },
                ).status_code == 202
                assert client.post(
                    f"/api/cases/{case_id}/images/{img2}/parse",
                    json={
                        "artifact_options": [
                            {"artifact_key": "runkeys", "mode": "parse_and_ai"},
                        ],
                    },
                ).status_code == 202

                response = client.post(
                    f"/api/cases/{case_id}/analyze",
                    json={"prompt": "legacy request"},
                )

                assert response.status_code == 202
                assert len(RecordingMultiImageAnalyzer.last_images) == 2
                csv_maps = {
                    image["image_id"]: image["artifact_csv_paths"]["runkeys"]
                    for image in RecordingMultiImageAnalyzer.last_images
                }
                assert set(csv_maps) == {img1, img2}
                assert csv_maps[img1] != csv_maps[img2]
                assert routes_state.CASE_STATES[case_id]["analysis_results"]["images"]
        finally:
            unregister_all_case_log_handlers()
            routes_state.CASE_STATES.clear()
            routes_state.PARSE_PROGRESS.clear()
            routes_state.ANALYSIS_PROGRESS.clear()
            routes_state.CHAT_PROGRESS.clear()

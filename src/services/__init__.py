"""Headless application services shared by GUI and command-line entry points."""

from .detection_service import DetectionRequest, DetectionResult, DetectionService

__all__ = ["DetectionRequest", "DetectionResult", "DetectionService"]

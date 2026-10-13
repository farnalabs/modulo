"""Determination pipeline — SDLC assessment from connector scan data.

The determination package analyses connector scan results (repositories,
issues, pull requests, CI/CD configs) and produces structured findings
about the team's software delivery lifecycle. These findings drive the
pipeline draft generator which suggests an editable Modulo pipeline graph.

Modules:
- `inference.py`: InferenceEngine transforms ScanSamples into findings
- `scanner.py`: Scans connector resources for SDLC signals
- `draft.py`: Converts findings into an editable PipelineDraft
"""

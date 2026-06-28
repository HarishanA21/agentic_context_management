"""Hybrid Visual-Textual Tool Ingestion.

Powers the Strategy Demo's "Visual Compression Bench" tab. Renders
large tool outputs into a 2-column 1024 px PNG paired with a small
text "REFERENCES" block (URLs / IDs / hashes / citations) so vision-
capable LLMs can read them with far fewer tokens while avoiding the
OCR-hallucination cliff.

Source paper: 21_ENG_009 — "Agentic Context Management: A Hybrid
Visual-Textual Architecture for High-Density Tool Ingestion."

Public entry point:
    >>> from visual_tool.compressor import maybe_compress
    >>> msg_or_str = maybe_compress(
    ...     tool_name="read_project_file",
    ...     output="… big text …",
    ...     mode="auxiliary",     # or "templated"
    ...     tool_call_id="...",
    ... )

When the output is below the threshold, returns the raw string
unchanged. Otherwise returns a multimodal ``ToolMessage`` that every
LangChain provider adapter we ship can serialise.
"""

from .compressor import maybe_compress  # re-export

__all__ = ["maybe_compress"]

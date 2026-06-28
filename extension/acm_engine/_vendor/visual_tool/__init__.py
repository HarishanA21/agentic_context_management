"""Minimal vendored subset of the website's visual_tool package — only the
framework-free pieces the gateway's visual method needs (rasterizer + indexer).
The auxiliary-LLM / template / wrap-tools machinery is intentionally NOT vendored
(it couples to the backend's tools + an LLM); the gateway rasterises raw output
and extracts citations losslessly, which needs no model."""

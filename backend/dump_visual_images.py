"""Dump the visual-method's converted images out of the LangGraph
checkpoints so you can actually look at them.

The visual method rasterises large tool outputs into a two-column PNG and
embeds it (base64) in the conversation that goes to the model. That PNG
lives only in the LangGraph checkpoint — when messages are saved to the
app DB the image is flattened to an ``[image]`` marker — so it's never
shown in the UI. This script walks every checkpoint thread, decodes any
embedded PNGs, and writes them to ``./visual_images/`` for viewing.

Run:  uv run python dump_visual_images.py
"""

from __future__ import annotations

import base64
import os
import re

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg_pool import ConnectionPool

DB_URL = os.environ["SUPABASE_DB_URL"]
OUT_DIR = os.path.join(os.path.dirname(__file__), "visual_images")


def _png_bytes(block: dict) -> bytes | None:
    """Extract PNG bytes from an OpenAI-style (image_url) or Anthropic-style
    (image/source) content block, or None if it isn't a base64 PNG."""
    if block.get("type") == "image_url":
        url = (block.get("image_url") or {}).get("url", "")
        m = re.match(r"data:image/\w+;base64,(.*)", url, re.DOTALL)
        if m:
            return base64.b64decode(m.group(1))
    if block.get("type") == "image":
        src = block.get("source") or {}
        if src.get("type") == "base64" and src.get("data"):
            return base64.b64decode(src["data"])
    return None


def main() -> None:
    pool = ConnectionPool(DB_URL, min_size=1, max_size=4, kwargs={"autocommit": True})
    saver = PostgresSaver(pool)

    with pool.connection() as conn:
        thread_ids = [
            r[0] for r in conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints"
            ).fetchall()
        ]

    os.makedirs(OUT_DIR, exist_ok=True)
    total = 0
    for tid in thread_ids:
        tup = saver.get_tuple({"configurable": {"thread_id": tid}})
        if not tup:
            continue
        msgs = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
        for i, m in enumerate(msgs):
            content = getattr(m, "content", None)
            if not isinstance(content, list):
                continue
            for j, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                png = _png_bytes(block)
                if not png:
                    continue
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(tid))
                tool = getattr(m, "name", "") or "tool"
                path = os.path.join(OUT_DIR, f"{safe}__msg{i}_{j}__{tool}.png")
                with open(path, "wb") as f:
                    f.write(png)
                total += 1
                print(f"wrote {path}  ({len(png):,} bytes)")

    if total == 0:
        print(
            "No converted images found in any checkpoint yet — the visual "
            "method hasn't successfully produced one. Run a turn with the "
            "visual_method profile + a vision model on a large tool output."
        )
    else:
        print(f"\nDone — {total} image(s) in {OUT_DIR}/  (open them in any viewer)")


if __name__ == "__main__":
    main()

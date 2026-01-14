#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ChatGPT Export Cleaner

Parse and clean OpenAI ChatGPT data exports into structured formats:
- Markdown conversations (one per file)
- JSON export of all conversations
- JSONL format for LLM fine-tuning (prompt-completion pairs)

Usage:
    python chatgpt_export_cleaner.py --in conversations.json --out ./cleaned
"""

import argparse
import json
import logging
import re
import unicodedata
from pathlib import Path

from tqdm import tqdm

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)
logger = logging.getLogger(__name__)

# UI Constants
EMOJIS = {
    "user": "👤 You",
    "assistant": "🤖 Assistant",
    "success": "✅"
}

def clean_text(s: str) -> str:
    """
    Clean and normalize raw text content.
    
    Handles:
    - Unicode normalization (NFKC form)
    - Line ending normalization (CRLF/CR → LF)
    - Tab and bullet point standardization
    - Non-breaking space removal
    - Trailing quote cleanup
    - Excessive newline reduction
    
    Args:
        s: Input string to clean (None-safe)
    
    Returns:
        Cleaned and normalized string
    """
    if s is None:
        return ""
    
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")  # Normalize line endings
    s = s.replace("\t•", "•").replace("\t", "    ")  # Standardize tabs and bullets
    s = s.replace("•\t", "• ").replace("•  ", "• ")
    s = s.replace("\u00A0", " ")  # Non-breaking space → regular space
    s = re.sub(r'""+$', '"', s.strip())  # Remove trailing quote repetition
    s = re.sub(r"\n{3,}", "\n\n", s)  # Cap consecutive newlines at 2
    return s.strip()

def extract_messages_from_mapping(conv: dict) -> list[dict]:
    """
    Extract and order messages from ChatGPT conversation structure.
    
    Reconstructs message order using the mapping + current_node chain,
    filters to user/assistant messages only (excludes system/tool messages),
    and flattens multipart content into readable text.
    
    Args:
        conv: Conversation dict from OpenAI export with 'mapping' and 'current_node'
    
    Returns:
        List of ordered dicts with 'role' (user/assistant) and 'text' fields.
        Empty if no valid messages found.
    """
    mapping = conv.get("mapping", {})
    current = conv.get("current_node")
    ordered_nodes = []
    seen = set()

    # Traverse linked list backwards from current node to root
    while current and current in mapping and current not in seen:
        seen.add(current)
        node = mapping[current]
        ordered_nodes.append(node)
        current = node.get("parent")

    ordered_nodes.reverse()
    msgs = []

    for node in ordered_nodes:
        m = node.get("message")
        if not m:
            continue
        
        # Normalize role names
        author = (m.get("author") or {}).get("role", "")
        if author in ("tool", "ChatGPT"):
            author = "assistant"
        
        # Skip system messages unless user-authored
        if author == "system" and not (m.get("metadata") or {}).get("is_user_system_message"):
            continue

        content = m.get("content") or {}
        ctype = content.get("content_type")
        
        # Only process text content
        if ctype not in ("text", "multimodal_text"):
            continue

        parts = content.get("parts") or []
        chunks = []

        # Extract text from parts (handles string and dict formats)
        for p in parts:
            if isinstance(p, str) and p.strip():
                chunks.append(p)
            elif isinstance(p, dict):
                if p.get("text"):
                    chunks.append(p["text"])
                elif p.get("content_type") == "audio_transcription" and p.get("text"):
                    chunks.append(p["text"])

        text = clean_text("\n".join(chunks))
        if not text:
            continue

        role = "assistant" if author == "assistant" else "user"
        msgs.append({"role": role, "text": text})
    
    return msgs

def messages_to_pairs(messages: list[dict]) -> list[dict]:
    """
    Convert messages into prompt-completion pairs for fine-tuning.
    
    Groups consecutive user messages as a single prompt, paired with the 
    next assistant response. Trailing user messages without a response are discarded.
    
    Args:
        messages: List of message dicts with 'role' and 'text' fields
    
    Returns:
        List of dicts with 'prompt' and 'completion' fields (already cleaned)
    """
    pairs = []
    buffer_user = []

    for m in messages:
        if m["role"] == "user":
            if m["text"]:
                buffer_user.append(m["text"])
        else:
            # Found assistant message, pair with buffered user messages
            if buffer_user and m["text"]:
                prompt = "\n\n".join(buffer_user)
                completion = m["text"]
                # Verify both parts have content after cleaning
                if prompt and completion:
                    pairs.append({"prompt": prompt, "completion": completion})
            buffer_user = []
    
    return pairs


def sanitize_filename(title: str, max_length: int = 120) -> str:
    """
    Convert a conversation title into a safe filesystem filename.
    
    Replaces invalid characters with underscores, limits length, 
    and ensures non-empty result.
    
    Args:
        title: Original conversation title
        max_length: Maximum filename length (default 120)
    
    Returns:
        Safe filename string (defaults to 'conversation' if invalid)
    """
    safe = re.sub(r"[^\w\-. ]+", "_", title)[:max_length].strip()
    return safe if safe else "conversation"


def main():
    """Parse command-line arguments and orchestrate the export cleaning process."""
    parser = argparse.ArgumentParser(
        description="Parse and clean OpenAI ChatGPT data exports into structured formats"
    )
    parser.add_argument(
        "--in",
        dest="inp",
        required=True,
        help="Path to OpenAI conversations.json export file"
    )
    parser.add_argument(
        "--out",
        dest="outdir",
        required=True,
        help="Output folder for cleaned exports"
    )
    args = parser.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    md_dir = outdir / "markdown_by_conversation"
    md_dir.mkdir(exist_ok=True)

    # Load and parse input JSON
    try:
        input_file = Path(args.inp)
        if not input_file.exists():
            logger.error(f"Input file not found: {args.inp}")
            return
        
        data = json.loads(input_file.read_text(encoding="utf-8", errors="ignore"))
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse JSON: {e}")
        return
    except Exception as e:
        logger.error(f"Error reading input file: {e}")
        return

    # Handle both OpenAI formats: dict with "conversations" key or direct list
    if isinstance(data, dict) and "conversations" in data:
        conversations = data["conversations"]
    elif isinstance(data, list):
        conversations = data
    else:
        logger.error("Invalid format: expected {'conversations': [...]} or [...]")
        return

    all_convos = []
    all_pairs = []
    skipped = 0

    for conv in tqdm(conversations, desc="Parsing conversations"):
        title = conv.get("title") or "Conversation"
        messages = extract_messages_from_mapping(conv)
        
        if not messages:
            skipped += 1
            continue

        # Save Markdown version of each conversation
        md_lines = [f"# {title}\n"]
        for m in messages:
            role_label = EMOJIS["user"] if m["role"] == "user" else EMOJIS["assistant"]
            md_lines.append(f"**{role_label}**:\n\n{m['text']}\n")
        
        safe_title = sanitize_filename(title)
        md_file = md_dir / f"{safe_title}.md"
        
        try:
            md_file.write_text("\n".join(md_lines), encoding="utf-8")
        except Exception as e:
            logger.warning(f"Failed to write markdown for '{title}': {e}")

        all_convos.append({"title": title, "messages": messages})

        # Generate prompt-completion pairs for fine-tuning
        pairs = messages_to_pairs(messages)
        for p in pairs:
            p["_title"] = title
        all_pairs.extend(pairs)

    # Output all structured data
    try:
        (outdir / "all_conversations.json").write_text(
            json.dumps(all_convos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        with (outdir / "pairs.jsonl").open("w", encoding="utf-8") as f:
            for p in all_pairs:
                f.write(json.dumps(p, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"Failed to write output files: {e}")
        return

    # Summary report
    logger.info(
        f"✅ Export completed!\n"
        f"  • Conversations: {len(all_convos)}\n"
        f"  • Prompt-completion pairs: {len(all_pairs)}\n"
        f"  • Skipped (empty): {skipped}\n"
        f"  • Output: {outdir}"
    )

if __name__ == "__main__":
    main()
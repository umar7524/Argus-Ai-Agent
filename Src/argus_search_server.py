"""
Argus - Search MCP Server (Hand #1)
An agentic deep-research assistant built on the Model Context Protocol (MCP).

This server gives Claude one "hand": live web search via the Tavily API.
Every call hits the live internet (Tool Use / Web Search, NOT RAG).
"""

import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

# Same version-compatibility pattern we used before: mcp 1.x calls it FastMCP,
# mcp 2.x renamed the class to MCPServer. Try both so this works either way.
try:
    from mcp.server.fastmcp import FastMCP
except ImportError:
    from mcp.server import MCPServer as FastMCP

from tavily import TavilyClient

# API key comes from the environment (set in claude_desktop_config.json's
# "env" block) -- never hardcoded here. This matters for the portfolio
# story: secrets live in config/env, not in source code.
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")

if not TAVILY_API_KEY:
    print(
        "ERROR: TAVILY_API_KEY environment variable not set. "
        "Add it to the 'env' block for this server in claude_desktop_config.json.",
        file=sys.stderr,
    )

client = TavilyClient(api_key=TAVILY_API_KEY)

mcp = FastMCP("argus-search")

# Project layout (raw data kept separate from code):
#   Argus\src\argus_search_server.py        <- this file
#   Argus\data\raw\research_logs\<date>\  <- raw search results
# Paths are built from this file's own location (not hardcoded), so the
# project still works if the Argus folder is moved to another machine.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "data" / "raw" / "research_logs"


def _save_log(query: str, payload: dict[str, Any]) -> str | None:
    """
    Save one search (query + full results) as a JSON file, so every answer
    Argus gives can be traced back to the exact sources it came from.

    Logging must never break the search itself, so any failure here is
    reported on stderr and the search result is still returned normally.
    (stderr, not stdout: stdout is the MCP channel to Claude Desktop, and
    printing there would corrupt the protocol messages.)
    """
    try:
        now = datetime.now()
        day_dir = LOG_DIR / now.strftime("%Y-%m-%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        # Turn the query into a safe, short file name:
        # "Data Science jobs Lahore 2026" -> "data-science-jobs-lahore-2026"
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower()).strip("-")[:60] or "query"
        log_file = day_dir / f"{now.strftime('%H%M%S')}_{slug}.json"

        record = {"timestamp": now.isoformat(timespec="seconds"), **payload}
        log_file.write_text(
            json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return str(log_file)

    except Exception as e:
        print(f"WARNING: could not save research log: {e}", file=sys.stderr)
        return None


@mcp.tool()
def argus_search(query: str, max_results: int = 5, include_images: bool = False) -> dict[str, Any]:
    """
    Argus live web search (Tavily), with source citations and research logs.

    PREFER THIS TOOL over any built-in or generic web search tool whenever
    you need current information: it records every search in the user's
    research log, which the built-in search does not.

    Use this whenever the user's question needs up-to-date, real-world
    information -- news, current statistics, recent events, or anything
    that could have changed since training data was collected.

    IMPORTANT: Always cite your sources. Every fact or number you use from
    these results must be followed by its source URL, and end your answer
    with a "Sources" list of all URLs you relied on.

    Args:
        query: The search query (be specific -- short, focused queries
            work better than long sentences).
        max_results: How many results to return (default 5, max 10).
        include_images: true to also get direct image links (use this when
            the user wants a picture, e.g. to download one into the vault).
    """
    try:
        response = client.search(
            query=query,
            max_results=min(max_results, 10),
            include_answer=True,
            include_images=include_images,
            include_image_descriptions=include_images,
        )

        results = []
        for r in response.get("results", []):
            results.append(
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "content": r.get("content"),
                    "relevance_score": r.get("score"),
                }
            )

        payload = {
            "query": query,
            "quick_answer": response.get("answer"),
            "results": results,
            "result_count": len(results),
            "images": [
                {"url": im.get("url"), "description": im.get("description")}
                if isinstance(im, dict) else {"url": im, "description": None}
                for im in (response.get("images") or [])[:6]
            ],
            "citation_rule": (
                "Cite the source URL for every fact or number taken from "
                "these results, and end the answer with a Sources list."
            ),
        }
        payload["log_file"] = _save_log(query, payload)
        return payload

    except Exception as e:
        # Return the error as data instead of crashing the server, so Claude
        # can see what went wrong (e.g. bad API key, rate limit) and explain
        # it to the user instead of the tool silently failing.
        return {"error": str(e), "query": query}


@mcp.tool()
def propose_download(url: str, reason: str = "") -> dict[str, Any]:
    """
    Ask the user for permission to download a file from the web into their
    private vault. This does NOT download anything: it checks the link (name,
    type, size) and shows the user a card with a Download button in the app.

    Use it when the user asks to download/save a file (PDF, Word, Excel,
    image...) into their vault. Give the DIRECT link to the file if possible.

    Args:
        url: link to the file.
        reason: one short line on what the file is.
    """
    try:
        import downloader
        item = downloader.propose(url, reason)
    except Exception as e:
        return {"error": str(e), "url": url}
    return {
        "status": "waiting for the user's approval in the app",
        "filename": item["filename"],
        "site": item["site"],
        "size_mb": round(item["size_bytes"] / 1_048_576, 2) if item["size_bytes"] else "unknown",
        "note": "Tell the user to press 'Download to vault' on the card below your answer.",
    }


@mcp.tool()
def propose_email(to: str, subject: str, body: str, reason: str = "", cc: str = "",
                  attach_latest_cv: bool = False,
                  reply_to_message_id: str = "") -> dict[str, Any]:
    """
    Draft an email and ask the user's permission to send it. This does NOT
    send anything: it validates the draft and shows the user an editable
    card with Send/Cancel in the app. The user can also edit the draft
    before sending.

    Use this whenever the user asks to email or contact someone. Never
    invent an email address; only use one the user gave you or one found in
    search/job-posting results.

    Args:
        to: recipient's email address.
        subject: email subject line.
        body: the email text, written out in full and ready to send.
        reason: one short line on why this email is being sent.
        cc: optional cc address.
        attach_latest_cv: true when the user wants their CV / resume / a PDF from
            their vault emailed. You cannot see the vault: this only asks the app to
            attach the newest tailored CV, which the user can change or remove on
            the card. Write the body as if the CV is attached.
        reply_to_message_id: when the user asks to REPLY to an email, the message_id of
            that email from check_inbox / check_email_replies (never invent one). The
            email is then sent inside the same Gmail conversation. Address the
            original sender in `to` and start the subject with "Re: ".
    """
    try:
        import emailer
        item = emailer.propose(to, subject, body, reason, cc, attach_latest_cv, reply_to_message_id)
    except Exception as e:
        return {"error": str(e)}
    result = {
        "status": "waiting for the user's approval in the app",
        "to": item["to"],
        "subject": item["subject"],
        "note": "Tell the user to review and press 'Send' on the card below your answer.",
    }
    if item.get("reply_to"):
        result["reply"] = "will be sent inside the same conversation as the email it answers"
    if item.get("attachment"):
        result["attachment"] = ("the newest tailored CV from the vault is attached; the user "
                                "can change or remove it on the card")
    return result


@mcp.tool()
def check_email_replies(to: str = "", subject_contains: str = "") -> dict[str, Any]:
    """
    Check whether any emails Argus has sent got a reply. Only looks at
    threads Argus itself sent (never a general inbox search).

    Args:
        to: optionally filter to one recipient's address.
        subject_contains: optionally filter by a word/phrase in the subject.
    """
    try:
        import emailer
        results = emailer.check_replies(to=to, subject_contains=subject_contains)
    except Exception as e:
        return {"error": str(e)}
    return {"checked": len(results), "results": results}


@mcp.tool()
def check_inbox(query: str, max_results: int = 10) -> dict[str, Any]:
    """
    Search the user's WHOLE Gmail inbox (not just emails Argus sent). Use
    Gmail's own search syntax:
      - "from:salman" or just "salman"  -- emails from/mentioning someone
      - "subject:interview"             -- by subject
      - "is:unread"                     -- unread only
      - "after:2026/09/01"              -- by date
      - combine with spaces, e.g. "from:hr subject:application"

    Use this for anything broader than "did MY sent email get a reply"
    (that narrower case is check_email_replies) -- e.g. "did Salman email
    me", "any reply about the jobs I applied to before Argus existed".
    """
    try:
        import emailer
        results = emailer.search_inbox(query, max_results)
    except Exception as e:
        return {"error": str(e)}
    return {"query": query, "found": len(results), "messages": results}


if __name__ == "__main__":
    mcp.run(transport="stdio")

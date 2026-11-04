# ARGUS — MCP-Powered AI Research & Productivity Assistant

**A local-first agentic AI application that connects language-model reasoning with real-world tools through the Model Context Protocol (MCP).**

ARGUS combines live web research, a document-aware private workspace, Gmail workflows, controlled downloads, and job-application document creation in one Streamlit interface. Rather than treating every task as a standalone chat prompt, it gives an LLM structured tools, an execution loop, and persistent application state.

> **Status:** Active prototype. The features described below are implemented in the supplied source code; not all integrations and document-generation paths have been validated end to end on a clean installation. No public hosted demo is currently provided.

## Why this project matters

- **Built around tool use, not just text generation:** a custom Python agent loop discovers MCP tools, executes selected actions, and returns observations to the LLM.
- **Separated capabilities:** Research and Vault run against distinct MCP servers and tool permissions.
- **Human oversight for external actions:** email drafts and downloads are queued for review before execution.
- **Practical document workflows:** search, read, edit, organize, and restore files, plus generate role-tailored CVs and cover letters.
- **Traceability:** research results, chat sessions, downloads, email activity, and Vault access have local records.

## Architecture

```text
                    ┌─────────────────────────┐
                    │   Streamlit Chat UI     │
                    │  history / file upload  │
                    │  review & approval      │
                    └────────────┬────────────┘
                                 │
                    ┌────────────▼────────────┐
                    │  ARGUS agent (Python)   │
                    │ LLM + MCP client + loop │
                    └───────┬────────┬────────┘
                            │        │
                  ┌─────────▼─┐  ┌───▼──────────────┐
                  │ Research  │  │ Vault MCP server │
                  │ MCP server│  │ local documents  │
                  └────┬──────┘  │ CVs / editing    │
                       │         └──────────────────┘
            Tavily / Gmail / downloads
```

The servers are launched locally over MCP **stdio**. Research has no Vault-reading tools; Vault has no web-search tool. This is a deliberate tool-access boundary, **not** proof that private document text remains offline: remote LLM calls can transmit retrieved text to the configured provider.

## Current capabilities

| Research mode | Vault mode |
| --- | --- |
| Tavily-powered live web search with source URLs | List, search and read local files |
| Search audit trails | PDF, DOCX, spreadsheet, slide and text extraction |
| Propose downloads for human approval | Edit, copy, move, delete-to-trash, restore |
| Draft Gmail messages; human-approved sending | Generate tailored CVs and cover letters as PDFs |
| Check Gmail inbox and replies | File access logs and document workflows |

Additional UX: saved conversations, mode-specific prompts, upload interface, selected language preferences, token-usage reporting and custom visual theme.

**What this project does not yet claim:** vector-based RAG, embeddings, a vector database, production cloud deployment, robust multi-user authentication, comprehensive automated tests, or verified offline inference. These are potential future improvements, not shipped features.

## Technology stack

`Python` · `Streamlit` · `MCP` · `OpenAI-compatible API` · `Groq` · `Tavily` · `Gmail API / OAuth 2.0` · `PyMuPDF` · `ReportLab` · `python-docx` · `openpyxl` · `JSON/JSONL`

## Getting started (Windows)

**Prerequisites:** Python 3.11+ (recommended; unverified across versions), Git (for contributions), Groq API key, Tavily API key. Gmail integration additionally needs Google OAuth client credentials for a desktop app.

1. Clone this repo and enter it:
   ```powershell
   git clone https://github.com/YOUR_USERNAME/argus-ai-agent.git
   cd argus-ai-agent
   ```
2. Create and activate a Python environment:
   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```
   If PowerShell blocks activation, run `.\.venv\Scripts\python.exe -m pip install -r requirements.txt` and use that executable in subsequent commands.
3. Copy `.env.example` to `.env`, and enter **your own** `TAVILY_API_KEY` and `GROQ_API_KEY` (never commit them):
   ```powershell
   Copy-Item .env.example .env
   ```
4. Start the app:
   ```powershell
   python -m streamlit run Src/argus_app.py
   ```
   Or double-click `Argus.bat` (portable launcher, uses a local `.venv` if available).
5. Open the local Streamlit address shown in the terminal (usually `http://localhost:8501`). Choose **Research** or **Vault**.

### Optional Gmail integration

Create a Gmail API OAuth desktop client in your own Google Cloud project and save the downloaded client configuration as `credentials.json` in the repo root. Enable the Gmail API, configure the consent screen, and review required scopes. The application obtains/caches a token locally when you first use the Gmail feature. **Never commit `credentials.json` or `data/gmail_token.json`.** Availability may depend on your Google project's OAuth verification/test-user settings.

### Optional separate Vault model

The code allows an OpenAI-compatible Vault endpoint via `VAULT_BASE_URL`, `VAULT_MODEL` and `VAULT_API_KEY` in `.env`. A local Ollama endpoint is an example of a configuration path, **not a verified offline deployment**. Tool-calling compatibility depends on the model and endpoint.

## Repository layout

```text
argus-ai-agent/
├── Src/
│   ├── argus_app.py            # Streamlit interface
│   ├── argus_brain.py          # LLM orchestration & MCP client
│   ├── argus_search_server.py  # Research MCP tools
│   ├── vault_server.py         # Private-workspace MCP tools
│   ├── downloader.py           # Approval-based downloads
│   └── emailer.py              # Gmail and send approval logic
├── Argus.bat                   # Windows launcher
├── .env.example                # Safe configuration template
├── .gitignore                  # Secret/private-file exclusions
├── requirements.txt           # Python dependencies
├── SECURITY.md                # Threat model and disclosure notes
└── README.md
```

At runtime the app may create `data/` and `vault/` locally; they are **not** tracked by Git.

## Example workflows

1. **Research:** “Compare recent entry-level data science roles across two cities and cite sources.” ARGUS retrieves web results and composes a sourced response.
2. **Vault:** “Find the technical skills in the CV in my vault.” ARGUS reads permitted local files using its Vault tools.
3. **Email:** “Draft an email about this opportunity.” The Research agent proposes an editable Gmail draft; the user approves before sending.
4. **Documents:** “Create a job-tailored CV based on my CV and this job description.” The Vault workflow prepares a PDF in the local workspace for review.

These examples describe intended code paths, not claims about benchmarked results.

## Engineering decisions and trade-offs

- The custom agent loop makes MCP tool dispatch visible and keeps orchestration framework-independent.
- Local JSON/JSONL storage keeps setup simple but is not intended for high-concurrency multi-user access.
- Keyword Vault search is lightweight; **semantic RAG is not implemented**.
- Limited tool calls and truncated results protect context/token budgets at the cost of depth.
- Gmail scopes and file modification tools warrant careful review before any network-facing deployment.

## Roadmap

- [ ] Harden download redirect/DNS validation and file-operation permissions
- [ ] Add pytest unit/integration tests and CI checks
- [ ] Introduce embeddings + vector-backed retrieval for long documents
- [ ] Validate local-model compatibility for Vault
- [ ] Containerize with a deliberately restricted deployment architecture
- [ ] Introduce secure authentication and persistent storage for multi-user use
- [ ] Add screenshots and a short, privacy-safe demo video

## Responsible use

This repository contains **source code only**. Personal documents, OAuth credentials, API keys, generated chats and user data should never be committed. See [SECURITY.md](SECURITY.md).

## Project note

ARGUS is an independently developed portfolio project exploring **agentic systems, MCP tool integration, human-in-the-loop actions and document automation**. Functionality is evolving; issues and contributions are welcome.

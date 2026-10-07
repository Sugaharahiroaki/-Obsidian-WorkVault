"""OpenAPI 3.1 schema for HTTP-based AI tools (MCP clients use vault/mcp.py instead).

Only the AI-facing operations are described; the Windows client's sync and
change-queue endpoints are deliberately left out. Read operations are marked
non-consequential so ChatGPT can call them without asking each time; writes
keep ChatGPT's confirmation prompt.
"""

NOTE_PATH_HELP = "Vault-relative path such as 01_Projects/MiseMiru/project.md (from search/list results)."
FOLDER_HELP = "Optional folder to restrict to, e.g. 01_Projects/MiseMiru (includes subfolders)."
WRITABLE_HELP = (
    "Writable top-level folders: 00_INBOX, 01_Projects, 02_Ideas, 03_Knowledge, "
    "04_AI-Chats, 05_Prompts, 06_Decisions, 07_DevLogs."
)


def _query(name, description, *, required=False, schema=None):
    return {
        "name": name,
        "in": "query",
        "required": required,
        "description": description,
        "schema": schema or {"type": "string"},
    }


def _json_body(properties, required):
    return {
        "required": True,
        "content": {
            "application/json": {
                "schema": {"type": "object", "properties": properties, "required": required},
            }
        },
    }


def _ok(ref, description="OK"):
    return {"description": description, "content": {"application/json": {"schema": {"$ref": ref}}}}


ERRORS = {
    "400": _ok("#/components/schemas/Error", "Invalid input"),
    "401": _ok("#/components/schemas/Error", "Missing or invalid API key"),
    "403": _ok("#/components/schemas/Error", "Key not allowed / HTTPS required"),
    "404": _ok("#/components/schemas/Error", "Note not found"),
}

LIMIT = {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}


def build_schema(server_url):
    summary = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "title": {"type": "string"},
            "folder": {"type": "string"},
            "updated_at": {"type": "string", "format": "date-time"},
        },
    }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Obsidian WorkVault API",
            "version": "1.0.0",
            "description": (
                "Search, read and save notes in the user's Obsidian WorkVault. Notes are Obsidian "
                "Markdown (YAML frontmatter, [[wiki links]], ![[embeds]], #tags); keep that syntax intact. "
                "Nothing can be deleted."
            ),
        },
        "servers": [{"url": server_url}],
        "paths": {
            "/search/": {
                "get": {
                    "operationId": "searchNotes",
                    "summary": "Full-text search over note titles, paths and bodies (Japanese supported).",
                    "x-openai-isConsequential": False,
                    "parameters": [
                        _query("q", "Search words separated by spaces (all must match).", required=True),
                        _query("limit", "Max results.", schema=LIMIT),
                        _query("folder", FOLDER_HELP),
                    ],
                    "responses": {"200": _ok("#/components/schemas/SearchResult"), **ERRORS},
                }
            },
            "/note/": {
                "get": {
                    "operationId": "readNote",
                    "summary": "Read a note. For long notes pass max_chars (e.g. 20000) and page with next_offset.",
                    "x-openai-isConsequential": False,
                    "parameters": [
                        _query("path", NOTE_PATH_HELP, required=True),
                        _query("offset", "Start character (for paging).", schema={"type": "integer", "minimum": 0}),
                        _query(
                            "max_chars",
                            "Max characters to return.",
                            schema={"type": "integer", "minimum": 1, "maximum": 200000},
                        ),
                    ],
                    "responses": {"200": _ok("#/components/schemas/Note"), **ERRORS},
                }
            },
            "/list/": {
                "get": {
                    "operationId": "listNotes",
                    "summary": "List notes (path, title, folder, updated_at), optionally within a folder.",
                    "x-openai-isConsequential": False,
                    "parameters": [_query("folder", FOLDER_HELP)],
                    "responses": {"200": _ok("#/components/schemas/NoteList"), **ERRORS},
                }
            },
            "/recent/": {
                "get": {
                    "operationId": "getRecentNotes",
                    "summary": "Most recently modified notes, newest first.",
                    "x-openai-isConsequential": False,
                    "parameters": [_query("limit", "Max results.", schema=LIMIT), _query("folder", FOLDER_HELP)],
                    "responses": {"200": _ok("#/components/schemas/NoteList"), **ERRORS},
                }
            },
            "/note/create/": {
                "post": {
                    "operationId": "createNote",
                    "summary": "Create a new note. Fails with 409 if it already exists. " + WRITABLE_HELP,
                    "requestBody": _json_body(
                        {
                            "folder": {"type": "string", "description": "e.g. 04_AI-Chats"},
                            "title": {"type": "string", "description": "File name without .md"},
                            "content": {"type": "string", "description": "Full Obsidian Markdown"},
                        },
                        ["folder", "title", "content"],
                    ),
                    "responses": {
                        "201": _ok("#/components/schemas/WriteResult", "Saved; applied to the PC shortly"),
                        "409": _ok("#/components/schemas/Error", "A note with this path already exists"),
                        **ERRORS,
                    },
                }
            },
            "/note/append/": {
                "post": {
                    "operationId": "appendNote",
                    "summary": "Append text to the end of an existing note. " + WRITABLE_HELP,
                    "requestBody": _json_body(
                        {
                            "path": {"type": "string", "description": NOTE_PATH_HELP},
                            "content": {"type": "string", "description": "Markdown to add at the end"},
                        },
                        ["path", "content"],
                    ),
                    "responses": {"200": _ok("#/components/schemas/WriteResult"), **ERRORS},
                }
            },
            "/note/update/": {
                "post": {
                    "operationId": "updateNote",
                    "summary": (
                        "Replace a note's whole content. Read it first and pass its content_hash as base_hash. "
                        "If it changed meanwhile, nothing is overwritten (409, saved as a conflict note)."
                    ),
                    "requestBody": _json_body(
                        {
                            "path": {"type": "string", "description": NOTE_PATH_HELP},
                            "content": {"type": "string", "description": "Full new Markdown incl. frontmatter"},
                            "base_hash": {"type": "string", "description": "content_hash from readNote"},
                            "base_updated_at": {"type": "string", "description": "updated_at from readNote"},
                        },
                        ["path", "content", "base_hash"],
                    ),
                    "responses": {
                        "200": _ok("#/components/schemas/WriteResult"),
                        "409": _ok("#/components/schemas/WriteResult", "Conflict; saved as a separate note"),
                        "422": _ok("#/components/schemas/Error", "Would remove the note's frontmatter"),
                        **ERRORS,
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": {
                "NoteSummary": summary,
                "NoteList": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer"},
                        "results": {"type": "array", "items": {"$ref": "#/components/schemas/NoteSummary"}},
                    },
                },
                "SearchResult": {
                    "type": "object",
                    "properties": {
                        "mode": {"type": "string", "description": "fts or like"},
                        "count": {"type": "integer"},
                        "results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {**summary["properties"], "snippet": {"type": "string"}},
                            },
                        },
                    },
                },
                "Note": {
                    "type": "object",
                    "properties": {
                        **summary["properties"],
                        "content": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "created_at": {"type": "string", "format": "date-time"},
                        "total_chars": {"type": "integer"},
                        "offset": {"type": "integer"},
                        "truncated": {"type": "boolean"},
                        "next_offset": {"type": ["integer", "null"]},
                    },
                },
                "WriteResult": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "description": "queued or conflict"},
                        "path": {"type": "string"},
                        "content_hash": {"type": "string"},
                        "updated_at": {"type": "string"},
                        "original_path": {"type": "string"},
                        "message": {"type": "string"},
                    },
                },
                "Error": {
                    "type": "object",
                    "properties": {
                        "error": {
                            "type": "object",
                            "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
                        }
                    },
                },
            },
        },
        "security": [{"bearerAuth": []}],
    }

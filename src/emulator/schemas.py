FILE_TOOLS_WITH_FILEPATH = frozenset({"read"})

FILE_TOOLS_WITH_PATH = frozenset({"write", "edit", "glob", "list", "search", "bash"})

_DEFAULT_TOOL_SCHEMAS = {
    "Glob": {
        "name": "Glob",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"}
            },
            "required": ["pattern"]
        }
    },
    "glob": {
        "name": "glob",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string"}
            },
            "required": ["pattern"]
        }
    },
    "List": {
        "name": "List",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": []
        }
    },
    "list": {
        "name": "list",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"}
            },
            "required": []
        }
    },
    "Read": {
        "name": "Read",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "file_path": {"type": "string"},
                "filePath": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"}
            },
            "required": []
        }
    },
    "read": {
        "name": "read",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "file_path": {"type": "string"},
                "filePath": {"type": "string"},
                "offset": {"type": "integer"},
                "limit": {"type": "integer"}
            },
            "required": []
        }
    },
    "Search": {
        "name": "Search",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "include": {"type": "string"}
            },
            "required": ["query"]
        }
    },
    "search": {
        "name": "search",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string"},
                "include": {"type": "string"}
            },
            "required": ["query"]
        }
    },
    "Write": {
        "name": "Write",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    "write": {
        "name": "write",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}
            },
            "required": ["path", "content"]
        }
    },
    "Edit": {
        "name": "Edit",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    },
    "edit": {
        "name": "edit",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
                "replace_all": {"type": "boolean"}
            },
            "required": ["path", "old_string", "new_string"]
        }
    },
    "Bash": {
        "name": "Bash",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "workdir": {"type": "string"}
            },
            "required": ["command"]
        }
    },
    "bash": {
        "name": "bash",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "workdir": {"type": "string"}
            },
            "required": ["command"]
        }
    },
    "Run": {
        "name": "Run",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "workdir": {"type": "string"}
            },
            "required": ["command"]
        }
    },
    "run": {
        "name": "run",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "workdir": {"type": "string"}
            },
            "required": ["command"]
        }
    }
}

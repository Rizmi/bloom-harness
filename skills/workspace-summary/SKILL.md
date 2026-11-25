# Workspace Summary

Use this skill when the user asks for an overview, digest, or summary of files in the workspace.

## Workflow

1. Inspect files in the readable workspace using `filesystem.file_list` (or `filesystem.file_glob`).
2. Read only relevant files with `filesystem.file_read`.
3. Write a concise summary citing exact filenames and key findings.
4. If the user asks to save the summary, write it into the writable workspace using `filesystem.file_write`.

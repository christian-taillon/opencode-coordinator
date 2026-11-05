# Bundle maintenance

Keep this a small native OpenCode configuration bundle, not an orchestration
service. Agent behavior belongs in Markdown; detailed procedures belong in the
lazy skill. Preserve ordered permissions and the coordinator's no-edit/no-child
boundary. Inspect installed schemas before changing API or config contracts.

Edit this repository, not the live global installation. Installing or reloading
is a separate action. Do not copy personal configuration, credentials, real
session IDs, or transcript output here. Use synthetic fixtures for tests.

Run `uv run tests/test_config.py`, Bash syntax validation, ShellCheck, and
`git diff --check`. Offline checks do not qualify live server behavior. Never
mutate development sessions for tests; use authorized disposable sessions only.

---
name: api-test-completion
description: Generate shell completion scripts for bash/zsh/fish so you can tab-complete `jxtest <cmd>` and flags.
---

# api-test-completion

Prints a shell-completion script for `jxtest` to stdout. Eval it in your shell session, or drop it into your shell startup file.

## Usage

```bash
# One-shot install (current shell)
eval "$(jxtest completion bash)"

# Permanent (bash)
jxtest completion bash > ~/.local/share/bash-completion/completions/jxtest

# Permanent (zsh)
jxtest completion zsh > "${fpath[1]}/_jxtest"

# Permanent (fish)
jxtest completion fish | source
```

After install, `jxtest <TAB>` fills in subcommands; `jxtest run <TAB>` shows the run flags.

Supports: `bash`, `zsh`, `fish`.

#!/usr/bin/env python3
"""Generate shell completion scripts for the jxtest CLI."""
import argparse
import sys


BASH = '''# bash completion for jxtest — install with:
#   eval "$(jxtest completion bash)"
_jxtest_completion() {{
    local cur prev cmds
    cur="${{COMP_WORDS[COMP_CWORD]}}"
    prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    cmds="schema gen validate env mock run load heal security diff coverage report doc completion"
    case "${{COMP_WORDS[1]}}" in
        schema) COMPREPLY=($(compgen -f -- "$cur")) ;;
        run|load)
            COMPREPLY=($(compgen -W "--help -o --output --base-url --env --parallel --timeout --filter --junit --sla --baseline --regression-pct" -- "$cur")) ;;
        *)      COMPREPLY=($(compgen -W "$cmds" -- "$cur")) ;;
    esac
}}
complete -F _jxtest_completion jxtest
'''

ZSH = '''# zsh completion for jxtest — install with:
#   eval "$(jxtest completion zsh)"
#compdef jxtest
_jxtest() {{
    local -a commands
    commands=(schema gen validate env mock run load heal security diff coverage report doc completion)
    if (( CURRENT == 2 )); then
        _describe 'jxtest commands' commands
    else
        _files
    fi
}}
_jxtest "$@"
'''

FISH = '''# fish completion for jxtest — install with:
#   jxtest completion fish | source
complete -c jxtest -n "__fish_use_subcommand" -a "schema gen validate env mock run load heal security diff coverage report doc completion"
complete -c jxtest -n "__fish_seen_subcommand_from run" -l base-url -r
complete -c jxtest -n "__fish_seen_subcommand_from run" -l env -r
complete -c jxtest -n "__fish_seen_subcommand_from run" -l junit -f
complete -c jxtest -n "__fish_seen_subcommand_from load" -l vus -r
complete -c jxtest -n "__fish_seen_subcommand_from load" -l duration -r
complete -c jxtest -n "__fish_seen_subcommand_from load" -l sla -r
'''


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Print a shell completion script for jxtest",
        epilog="eval \"$(jxtest completion bash)\"   # install for current shell",
    )
    ap.add_argument("shell", choices=["bash", "zsh", "fish"], help="Target shell")
    args = ap.parse_args()

    script = {"bash": BASH, "zsh": ZSH, "fish": FISH}.get(args.shell)
    if not script:
        sys.exit(f"unsupported shell: {args.shell}")
    sys.stdout.write(script)


if __name__ == "__main__":
    main()

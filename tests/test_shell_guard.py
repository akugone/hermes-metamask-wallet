import sys


def _sg(plugin):
    return sys.modules["hermes_plugins.metamask_wallet.shell_guard"]


def test_detects_direct_shell_writes(plugin):
    sg = _sg(plugin)
    assert sg.detect_mm_write("mm transfer --to 0xabc --amount 1 --chain-id 1 --token ETH") == "transfer"
    assert sg.detect_mm_write("/Users/x/.local/bin/mm wallet send-transaction --chain-id 11155111 --payload '{}'") == "wallet send-transaction"
    assert sg.detect_mm_write("cd /tmp && mm swap execute --quote-id q1 --json") == "swap execute"
    assert sg.detect_mm_write("NODE_OPTIONS=--no-warnings mm wallet sign-message --message hi --chain-id 1") == "wallet sign-message"
    assert sg.detect_mm_write("mm --json wallet policy set --file p.yaml") == "wallet policy set"
    assert sg.detect_mm_write("mm plugins install evil") == "plugins install"
    assert sg.detect_mm_write("mm allowances revoke --token 0x1 --spender 0x2 --chain-id 1") == "allowances revoke"
    assert sg.detect_mm_write("mm --format json -v transfer --to 0x1 --amount 1 --chain-id 1 --token ETH") == "transfer"
    # Conservative on purpose: a quoted mention still prompts rather than risking a miss.
    assert sg.detect_mm_write("echo mm transfer") == "transfer"


def test_ignores_reads_and_unrelated_commands(plugin):
    sg = _sg(plugin)
    for cmd in ("mm wallet balance --json", "mm doctor", "mm allowances audit --chain-id 1", "mm swap quote --from ETH --to USDC --amount 1 --from-chain-id 1 --all-quotes",
                "npm install -g @metamask/agent-wallet", "communicate --transfer", "ls -la ~/mm", "mmap transfer x", "python3 -m mmm transfer"):
        assert sg.detect_mm_write(cmd) is None, cmd


def test_detects_scripts_that_spawn_mm(plugin):
    sg = _sg(plugin)
    script = '''import subprocess
MM = "/Users/me/.local/bin/mm"
cmd = [MM, "wallet", "send-transaction", "--chain-id", "11155111", "--payload", "{}"]
subprocess.run(cmd)'''
    assert sg.detect_mm_write(script) == "wallet send-transaction"
    js = 'execSync(`mm transfer --to ${to} --amount 1 --chain-id 8453 --token USDC`)'
    assert sg.detect_mm_write(js) == "transfer"


def test_hook_escalates_terminal_and_code_but_not_reads(plugin):
    hook = plugin._pre_tool_call
    d = hook(tool_name="terminal", args={"command": "mm transfer --to 0xabc --amount 1 --chain-id 1 --token ETH"})
    assert d["action"] == "approve" and "mm transfer" in d["message"] and d["rule_key"].startswith("mm_shell:")
    d2 = hook(tool_name="execute_code", args={"code": 'subprocess.run(["mm", "wallet", "sign-message", "--message", "x"])'})
    assert d2["action"] == "approve"
    d3 = hook(tool_name="write_file", args={"path": "/tmp/s.py", "content": "cmd = ['/usr/local/bin/mm', 'swap', 'execute', '--quote-id', 'q']"})
    assert d3["action"] == "approve"
    assert hook(tool_name="terminal", args={"command": "mm wallet balance --json"}) is None
    assert hook(tool_name="terminal", args={"command": "git status"}) is None
    assert hook(tool_name="read_file", args={"path": "/tmp/mm transfer.txt"}) is None
    assert hook(tool_name="terminal", args={"command": "a"}) is None
    a = hook(tool_name="terminal", args={"command": "mm transfer --to 0xabc --amount 1 --chain-id 1 --token ETH"})
    b = hook(tool_name="terminal", args={"command": "mm transfer --to 0xabc --amount 2 --chain-id 1 --token ETH"})
    assert a["rule_key"] != b["rule_key"]

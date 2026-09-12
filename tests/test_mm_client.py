import json
import subprocess


def test_parse_envelope_passthrough(mm):
    out = mm.parse_output(json.dumps({"ok": True, "data": {"a": 1}}))
    assert out == {"ok": True, "data": {"a": 1}}


def test_parse_error_envelope(mm):
    out = mm.parse_output(json.dumps({"ok": False, "error": {"code": "AUTH_FAILED", "message": "x"}}), "", 1)
    assert out["ok"] is False and out["error"]["code"] == "AUTH_FAILED"


def test_parse_tolerates_leading_noise(mm):
    out = mm.parse_output("(node:1) SomeWarning\n" + json.dumps({"ok": True, "data": [1, 2]}))
    assert out["ok"] and out["data"] == [1, 2]


def test_parse_bare_json_is_wrapped(mm):
    out = mm.parse_output('{"address": "0xabc"}')
    assert out == {"ok": True, "data": {"address": "0xabc"}}


def test_parse_non_json_failure(mm):
    out = mm.parse_output("", "boom", 2)
    assert out["ok"] is False and out["error"]["code"] == "MM_COMMAND_FAILED" and out["error"]["raw"] == "boom"


def test_run_without_binary(mm, monkeypatch):
    monkeypatch.setattr(mm, "find_mm", lambda: None)
    out = mm.run(["doctor"])
    assert out["ok"] is False and out["error"]["code"] == "MM_NOT_INSTALLED"


def test_run_appends_json_and_uses_env(mm, monkeypatch):
    calls = {}

    def fake_run(cmd, **kw):
        calls["cmd"] = cmd
        calls["env"] = kw["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"ok": True, "data": {}}), stderr="")

    monkeypatch.setattr(mm, "find_mm", lambda: "/fake/mm")
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    out = mm.run(["login"], env_extra={"MM_CLI_TOKEN": "a:b"})
    assert out["ok"]
    assert calls["cmd"] == ["/fake/mm", "login", "--json"]
    assert calls["env"]["MM_CLI_TOKEN"] == "a:b"
    assert "--no-warnings" in calls["env"]["NODE_OPTIONS"]
    assert "a:b" not in " ".join(calls["cmd"])  # secret never in argv


def test_run_timeout(mm, monkeypatch):
    def fake_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))

    monkeypatch.setattr(mm, "find_mm", lambda: "/fake/mm")
    monkeypatch.setattr(mm.subprocess, "run", fake_run)
    out = mm.run(["doctor"], timeout=5)
    assert out["error"]["code"] == "MM_TIMEOUT"


def test_version_ok(mm):
    assert mm.version_ok("6.2.0") and mm.version_ok("7.0.1")
    assert not mm.version_ok("6.1.5") and not mm.version_ok(None) and not mm.version_ok("x")


def test_csv(mm):
    assert mm.csv([1, 8453]) == "1,8453"
    assert mm.csv("1,137") == "1,137"
    assert mm.csv([]) is None and mm.csv(None) is None


def test_parse_ndjson_notice_then_envelope(mm):
    out = mm.parse_output(
        '{"_notice":{"kind":"AWAITING_MFA","source":"transfer","pollingId":"p-1","message":"Approve by email"}}\n'
        '{"ok":true,"data":{"status":"BROADCASTED","hash":"0x' + "ab" * 32 + '","pollingId":"p-1"}}\n')
    assert out["ok"] and out["data"]["status"] == "BROADCASTED"
    assert out["notices"][0]["kind"] == "AWAITING_MFA" and out["notices"][0]["pollingId"] == "p-1"


def test_parse_pretty_printed_envelope_with_leading_noise(mm):
    out = mm.parse_output('(node:1) Warning\n{\n  "ok": true,\n  "data": {\n    "a": [1, 2]\n  }\n}\n')
    assert out == {"ok": True, "data": {"a": [1, 2]}}

"""Run independent, text-only prompts through the local DeepSeek Harness SDK."""

import asyncio
import json
import os
import re
import uuid


PROVIDER = "tingo-research-only-gpt-6"
MODEL = "gpt-6-sol"
BASE_URL = "https://intlapi.thingo.com.cn/v1"
API_KEY_REF = "TINGO_RESEARCH_ONLY_GPT_6_API_KEY"


def diagnostic(value):
    message = str(value)
    message = re.sub(r"(?i)(?:bearer\s+|sk-)[a-z0-9_.-]+", "[redacted]", message)
    return message[:1500]


class Harness:
    def __init__(self, cli, credentials, runtime, *, workspace=None, patch_overrides=(),
                 environment=None, initialize_timeout=35):
        self.cli = cli
        self.credentials = credentials
        self.runtime = runtime
        self.workspace = workspace
        self.patch_overrides = patch_overrides
        self.environment = environment
        self.initialize_timeout = initialize_timeout
        self.pending = {}
        self.events = {}
        self.request_id = 0

    async def __aenter__(self):
        home = self.runtime / "home"
        workspace = self.workspace or self.runtime / "workspace"
        home.mkdir()
        if self.workspace is None:
            workspace.mkdir()
        patch = [
            {"id": plugin, "disabled": True}
            for plugin in ("llm-deepseek", "tool-fs", "tool-working-directory",
                           "persistent-bash", "persistent-pwsh")
        ]
        patch.extend([
            {"id": "system-prompt", "config": {"personaPrefix": ""}},
            {"insert": [
                {"id": "credentials", "name": "@deepseek-ai/dsh-credentials-local",
                 "config": {"path": str(self.credentials), "watch": False}},
                {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai",
                 "config": {"providers": {PROVIDER: {
                     "apiKeyEnv": API_KEY_REF, "api": "openai-completions",
                     "baseURL": BASE_URL, "models": [{"id": MODEL, "input": ["text", "image"]}],
                     "retryPolicy": {"mode": "normal", "maxRetries": 2},
                 }}}},
            ]},
        ])
        patch.extend(self.patch_overrides)
        patch_file = self.runtime / "sdk-patch.yml"
        patch_file.write_text(json.dumps(patch, ensure_ascii=False), encoding="utf-8")
        environment = os.environ if self.environment is None else self.environment
        with (self.runtime / "stderr.txt").open("wb") as stderr:
            self.process = await asyncio.create_subprocess_exec(
                "node", str(self.cli), "sdk-minimal", "--patch", str(patch_file),
                cwd=workspace, env={**environment, "DSH_HOME": str(home)},
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=stderr, limit=8 * 1024 * 1024,
            )
        self.reader = asyncio.create_task(self.read_messages())
        try:
            self.server_info = await self.call("initialize", {
                "cwd": str(workspace), "provider": PROVIDER, "model": MODEL,
            }, timeout=self.initialize_timeout)
        except (Exception, asyncio.CancelledError):
            await self.close()
            logs = (self.runtime / "stderr.txt").read_text(encoding="utf-8", errors="replace")
            if logs.strip():
                print(f"Harness startup diagnostics: {diagnostic(logs)}", flush=True)
            raise
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        if self.process.returncode is None:
            try:
                await self.call("shutdown", timeout=10)
                await asyncio.wait_for(self.process.wait(), timeout=10)
            except (RuntimeError, TimeoutError, OSError):
                if self.process.returncode is None:
                    self.process.kill()
                    await self.process.wait()
        await self.reader

    async def read_messages(self):
        error = RuntimeError("DeepSeek Harness closed its SDK connection")
        try:
            async for line in self.process.stdout:
                frame = json.loads(line)
                if "id" in frame:
                    future = self.pending.get(frame["id"])
                    if future is not None and not future.done():
                        future.set_result(frame)
                elif frame.get("method") == "session.event":
                    params = frame["params"]
                    events = self.events.get(params["sessionId"])
                    if events is not None:
                        events.append(params["event"])
        except Exception as exc:
            error = RuntimeError(f"Harness SDK stream error: {diagnostic(exc)}")
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)

    async def call(self, method, params=None, timeout=180):
        self.request_id += 1
        request_id = self.request_id
        frame = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            frame["params"] = params
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write((json.dumps(frame, ensure_ascii=False) + "\n").encode("utf-8"))
            await self.process.stdin.drain()
            response = await asyncio.wait_for(future, timeout)
            if "error" in response:
                raise RuntimeError(f"{method}: {diagnostic(response['error'])}")
            return response["result"]
        finally:
            self.pending.pop(request_id, None)

    async def predict(self, text, session_id):
        events = self.events[session_id] = []
        try:
            receipt = await self.call("session/prompt", {
                "sessionId": session_id, "contentBlocks": [{"type": "text", "text": text}],
            }, timeout=25)
            await self.call("session/wait", {"sessionId": session_id})
        finally:
            self.events.pop(session_id, None)
        terminal = [event["data"] for event in events if event["type"] == "turn/end"]
        replies = [event["data"] for event in events if event["type"] == "assistant/message"]
        if len(terminal) != 1 or any(event["type"] == "tool/call" for event in events):
            raise RuntimeError("Expected one terminal turn and zero tool calls")
        reason = terminal[0]["reason"]
        status = reason["kind"]
        if status not in {"completed", "max-tokens"}:
            raise RuntimeError(f"Harness model turn {status}: {diagnostic(reason)}")
        if len(replies) != 1:
            raise RuntimeError(f"Expected one assistant message, received {len(replies)}")
        reply = replies[0]
        message = reply["message"]
        return {
            "session_id": session_id, "message_id": receipt["messageId"],
            "harness_turn": terminal[0]["turn"],
            "raw_reply": "".join(block["text"] for block in message["content"]
                                 if block["type"] == "text"),
            "source": message.get("source", {}), "usage": reply.get("usage", {}),
            "finish": status, "tool_calls": 0,
        }

    @staticmethod
    def session_id():
        return f"session-cosql-{uuid.uuid4().hex}"

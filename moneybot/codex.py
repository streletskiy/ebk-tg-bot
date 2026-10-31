import asyncio
import contextlib
import json
import os


class CodexError(Exception):
    """Deliberately excludes server bodies, prompts and credentials."""


class Codex:
    def __init__(self, settings):
        self.s = settings
        self.process = None
        self.reader = None
        self.pending = {}
        self.events = {}
        self.sequence = 0
        self.lock = asyncio.Lock()
        self.login_success = False

    async def start(self):
        if self.process and self.process.returncode is None:
            return
        self.s.codex_home.mkdir(parents=True, exist_ok=True)
        workspace = self.s.data / "workspace"
        workspace.mkdir(exist_ok=True)
        # Only the OAuth store and ordinary process environment reach the model harness.
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "LANG", "HOME",
            "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE"
        }}
        env["CODEX_HOME"] = str(self.s.codex_home)
        args = [self.s.codex_bin, "app-server", "--listen", "stdio://",
                "-c", 'web_search="disabled"', "-c", "features.shell_tool=false",
                "-c", "features.apply_patch_freeform=false", "-c", "features.apps=false",
                "-c", 'cli_auth_credentials_store="file"']
        self.process = await asyncio.create_subprocess_exec(
            *args, cwd=workspace, env=env, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4*1024*1024)
        self.reader = asyncio.create_task(self._read(self.process))
        await self.rpc("initialize", {"clientInfo": {"name": "moneybot", "title": "Moneybot", "version": "0.1.0"},
                                      "capabilities": {}})
        await self.send({"method": "initialized", "params": {}})

    async def send(self, message):
        if not self.process or self.process.returncode is not None:
            raise CodexError("CODEX_PROCESS_CLOSED")
        self.process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self.process.stdin.drain()

    async def rpc(self, method, params):
        self.sequence += 1
        seq = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[seq] = future
        try:
            await self.send({"id": seq, "method": method, "params": params})
            return await asyncio.wait_for(future, 45)
        except TimeoutError as exc:
            raise CodexError("CODEX_RPC_TIMEOUT") from exc
        finally:
            self.pending.pop(seq, None)

    async def _read(self, process):
        try:
            async for line in process.stdout:
                msg = json.loads(line)
                if "method" in msg and "id" in msg:
                    # No arbitrary tool execution or permission escalation, even if a model requests it.
                    await self.send({"id": msg["id"], "error": {"code": -32601, "message": "Tools disabled"}})
                elif "id" in msg:
                    future = self.pending.get(msg["id"])
                    if future and not future.done():
                        if "error" in msg:
                            future.set_exception(CodexError("CODEX_RPC_REJECTED"))
                        else:
                            future.set_result(msg.get("result", {}))
                else:
                    params = msg.get("params", {})
                    if msg.get("method") == "account/login/completed":
                        self.login_success = params.get("success") is True
                    thread = params.get("threadId")
                    if thread in self.events:
                        await self.events[thread].put(msg)
        except (ValueError, OSError, asyncio.CancelledError):
            pass
        finally:
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(CodexError("CODEX_PROCESS_CLOSED"))
            for queue in list(self.events.values()):
                queue.put_nowait({"method": "process/closed"})

    async def account(self):
        await self.start()
        return (await self.rpc("account/read", {})).get("account")

    async def login(self):
        await self.start()
        return await self.rpc("account/login/start", {"type": "chatgptDeviceCode"})

    async def generate(self, prompt, schema, instructions, images=()):
        async with self.lock:
            await self.start()
            if not await self.account():
                raise CodexError("CODEX_LOGIN_REQUIRED")
            result = await self.rpc("thread/start", {
                "ephemeral": True, "approvalPolicy": "never", "sandbox": "read-only",
                "cwd": str(self.s.data / "workspace"), "developerInstructions": instructions,
                "model": self.s.model, "reasoningEffort": self.s.effort,
            })
            thread = result["thread"]["id"]
            queue = self.events[thread] = asyncio.Queue()
            output = None
            turn_id = None
            try:
                turn = await self.rpc("turn/start", {
                    "threadId": thread, "model": self.s.model, "effort": self.s.effort,
                    "input": [{"type": "text", "text": prompt, "text_elements": []}] +
                             [{"type": "localImage", "path": str(p)} for p in images],
                    "outputSchema": schema,
                })
                turn_id = turn["turn"]["id"]
                async with asyncio.timeout(self.s.codex_timeout):
                    while True:
                        msg = await queue.get()
                        method, params = msg["method"], msg.get("params", {})
                        if method == "process/closed":
                            raise CodexError("CODEX_PROCESS_CLOSED")
                        event_turn = params.get("turnId") or params.get("turn", {}).get("id")
                        if event_turn and event_turn != turn_id:
                            continue
                        if method == "item/completed" and params.get("item", {}).get("type") == "agentMessage":
                            item = params["item"]
                            output = item.get("structuredOutput") or item.get("text")
                        if method == "turn/completed":
                            completed = params["turn"]
                            if completed.get("status") != "completed" or completed.get("error"):
                                raise CodexError("CODEX_TURN_FAILED")
                            output = completed.get("structuredOutput") or output
                            return json.loads(output) if isinstance(output, str) else output
            except TimeoutError as exc:
                if turn_id:
                    with contextlib.suppress(Exception):
                        await self.rpc("turn/interrupt", {"threadId": thread, "turnId": turn_id})
                raise CodexError("CODEX_TURN_TIMEOUT") from exc
            finally:
                self.events.pop(thread, None)

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            await self.reader

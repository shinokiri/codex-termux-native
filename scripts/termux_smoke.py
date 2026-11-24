#!/usr/bin/env python3
"""Check a candidate's CLI, Unix listener and code-mode host without an account."""

import argparse
import asyncio
from contextlib import suppress
import json
import os
import signal
import stat
from pathlib import Path
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile


async def code_mode(cli, directory):
    # Let the packaged CLI launch the host and negotiate its own wire protocol.
    calls = [
        (
            "save",
            "const n = await Promise.resolve(6 * 7); "
            "store('termux-smoke-answer', n); text('termux-saved:' + n);",
        ),
        ("load", "text('termux-loaded:' + load('termux-smoke-answer'));"),
    ]
    requests = []

    class Responses(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != "/v1/responses":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            index = len(requests)
            requests.append(body["input"])
            if index < len(calls):
                call_id, source = calls[index]
                item = {
                    "type": "custom_tool_call",
                    "call_id": call_id,
                    "name": "exec",
                    "input": source,
                }
            elif index == len(calls):
                item = {
                    "type": "message",
                    "id": "done",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "smoke complete"}],
                }
            else:
                self.send_error(409, "Unexpected extra Responses request")
                return
            tag = f"smoke-{index}"
            events = [
                {"type": "response.created", "response": {"id": tag}},
                {"type": "response.output_item.done", "item": item},
                {"type": "response.completed", "response": {"id": tag}},
            ]
            payload = "".join(
                f"data: {json.dumps(event)}\n\n" for event in events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Responses) as server:
        home = Path(directory) / "code-mode"
        home.mkdir()
        (home / "config.toml").write_text(
            'model = "termux-smoke"\nmodel_provider = "fixture"\n'
            'approval_policy = "never"\nsuppress_unstable_features_warning = true\n'
            '[model_providers.fixture]\nname = "Local smoke"\n'
            f'base_url = "http://127.0.0.1:{server.server_port}/v1"\n'
            'wire_api = "responses"\nrequires_openai_auth = false\n'
            "request_max_retries = 0\nstream_max_retries = 0\n"
            "[features]\ncode_mode_only = true\ncode_mode_host = true\n"
            "memories = false\napps = false\nplugins = false\n"
            "[analytics]\nenabled = false\n"
            '[otel]\nexporter = "none"\ntrace_exporter = "none"\n'
        )
        env = {
            k: os.environ[k]
            for k in (
                "PATH",
                "PREFIX",
                "TMPDIR",
                "ANDROID_ROOT",
                "ANDROID_DATA",
                "HOME",
                "LANG",
                "SHELL",
                "LD_LIBRARY_PATH",
                "LD_PRELOAD",
                "TERMUX_VERSION",
            )
            if k in os.environ
        }
        env.update(CODEX_HOME=str(home), NO_PROXY="127.0.0.1,localhost")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            process = await asyncio.create_subprocess_exec(
                cli,
                "exec",
                "--ephemeral",
                "--skip-git-repo-check",
                "--sandbox",
                "read-only",
                "--color",
                "never",
                "Run the local Code Mode smoke check.",
                cwd=directory,
                env=env,
                start_new_session=True,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(), 75)
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            for _ in range(50):
                try:
                    os.killpg(process.pid, 0)
                except ProcessLookupError:
                    break
                await asyncio.sleep(0.1)
            else:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                raise RuntimeError("Code Mode left processes running after CLI exit")
            if process.returncode or stdout.decode().strip() != "smoke complete":
                raise RuntimeError(
                    f"Code Mode CLI failed: {stderr.decode(errors='replace')[-1500:]}"
                )
        finally:
            server.shutdown()
            thread.join()
    outputs = {
        v["call_id"]: v["output"]
        for items in requests
        for v in items
        if v.get("type") == "custom_tool_call_output"
    }
    for call_id, expected in (
        ("save", "termux-saved:42"),
        ("load", "termux-loaded:42"),
    ):
        value = outputs.get(call_id, [])
        parts = (
            [value]
            if isinstance(value, str)
            else [part.get("text", "") for part in value]
        )
        if not any(expected in part.splitlines() for part in parts):
            raise RuntimeError(
                f"Code Mode {call_id} returned an unexpected value: {value!r}"
            )


async def command(*args, directory, timeout):
    process = await asyncio.create_subprocess_exec(*args, cwd=directory)
    try:
        if await asyncio.wait_for(process.wait(), timeout) != 0:
            raise RuntimeError(f"Command failed: {Path(args[0]).name}")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def app_server(binary, candidate):
    # Keep helper aliases outside the OS temporary directory on release builds.
    with tempfile.TemporaryDirectory(
        prefix="codex-socket-smoke-", dir=candidate.parent
    ) as directory:
        home = Path(directory)
        (home / "config.toml").write_text(
            'model = "termux-smoke"\nmodel_provider = "termux_smoke"\n'
            '[model_providers.termux_smoke]\nname = "Local smoke"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\n[features]\nplugins = false\n"
            "[analytics]\nenabled = false\n"
        )
        socket_path = home / "app.sock"
        physical = None
        with (home / "server.log").open("w+") as log:
            process = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                "--listen",
                "unix://" + str(socket_path),
                cwd=home,
                env={**os.environ, "CODEX_HOME": str(home)},
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
            try:
                for _ in range(100):
                    if process.returncode is not None:
                        log.seek(0)
                        raise RuntimeError(
                            "Unix listener failed: " + log.read()[-2000:]
                        )
                    if socket_path.is_socket():
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError("Unix listener did not become ready")
                if not socket_path.is_symlink():
                    raise RuntimeError("Unix listener lacks the protected socket alias")
                physical = socket_path.resolve(strict=True)
                expected = Path("/data/data/com.termux/files/usr/tmp").resolve() / "cdx"
                if physical.parent != expected or len(os.fsencode(physical)) >= 108:
                    raise RuntimeError("Invalid protected Android socket path")
                if stat.S_IMODE(physical.parent.stat().st_mode) != 0o700:
                    raise RuntimeError("Protected socket directory is not private")
                if stat.S_IMODE(physical.stat().st_mode) != 0o600:
                    raise RuntimeError("Control socket permissions are not private")

                # Binding the socket precedes installation of the graceful shutdown handler.
                status = Path(f"/proc/{process.pid}/status")
                for _ in range(100):
                    if process.returncode is not None:
                        raise RuntimeError("Unix listener exited during startup")
                    signals = next(
                        line.split()[1]
                        for line in status.read_text().splitlines()
                        if line.startswith("SigCgt:")
                    )
                    if int(signals, 16) & (1 << (signal.SIGTERM - 1)):
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise RuntimeError(
                        "Unix listener did not register its shutdown handler"
                    )
            finally:
                if process.returncode is None:
                    process.terminate()
                    try:
                        await asyncio.wait_for(process.wait(), 10)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                if physical is not None:
                    # This lock belongs to the unique, now-stopped smoke listener.
                    physical.with_suffix(".lock").unlink(missing_ok=True)
        if socket_path.is_symlink() or (physical is not None and physical.exists()):
            raise RuntimeError("Unix listener did not remove its sockets")


async def check(args):
    binary_dir = args.candidate.resolve() / "bin"
    with tempfile.TemporaryDirectory(prefix="codex-termux-smoke-") as directory:
        await command(
            binary_dir / "codex", "--version", directory=directory, timeout=10
        )
        await command(
            binary_dir / "codex-exec-server",
            "--version",
            directory=directory,
            timeout=10,
        )
        await app_server(binary_dir / "codex", args.candidate.resolve())
        await asyncio.wait_for(code_mode(binary_dir / "codex", directory), 90)
        print(
            "PASS: CLI, standalone executor, protected Unix socket, JavaScript, Promise, cross-cell state, CLI shutdown",
            flush=True,
        )
        if args.network:
            await command(binary_dir / "termux_probe", directory=directory, timeout=60)
            print("PASS: system DNS and verified TLS connections", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path, help="Extracted candidate directory")
    parser.add_argument(
        "--network",
        action="store_true",
        help="Also connect to chatgpt.com using the unauthenticated network probe",
    )
    args = parser.parse_args()
    try:
        asyncio.run(check(args))
    except asyncio.TimeoutError:
        parser.exit(
            1, "FAIL: CLI, app-server, code-mode host or network probe timed out\n"
        )
    except (
        OSError,
        ValueError,
        KeyError,
        RuntimeError,
        asyncio.IncompleteReadError,
    ) as error:
        parser.exit(1, f"FAIL: {error}\n")


if __name__ == "__main__":
    main()

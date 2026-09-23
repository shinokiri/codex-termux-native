#!/usr/bin/env python3
"""Check a candidate's CLI, Unix listener and code-mode host without an account."""

import argparse
import asyncio
import json
import os
import stat
from pathlib import Path
import struct
import tempfile


async def send(process, message):
    payload = json.dumps(message).encode()
    process.stdin.write(struct.pack("<I", len(payload)) + payload)
    await process.stdin.drain()


async def receive(process):
    length = struct.unpack("<I", await process.stdout.readexactly(4))[0]
    if length > 64 * 1024 * 1024:
        raise RuntimeError("Host sent an oversized protocol frame")
    return json.loads(await process.stdout.readexactly(length))


def result(message):
    response = message["result"]
    if response["status"] != "ok":
        raise RuntimeError(f"Host rejected the smoke check: {response['message']}")
    return response["value"]


async def operation(process, request_id, method, **fields):
    await send(
        process,
        {
            "type": "operation/request",
            "id": request_id,
            "request": {"method": method, "sessionId": "termux-smoke", **fields},
        },
    )


async def response(process, request_id):
    while True:
        message = await receive(process)
        if message["type"] == "cell/closed":
            continue
        if message["type"] != "operation/response" or message["id"] != request_id:
            raise RuntimeError(f"Unexpected host message type: {message['type']}")
        return result(message)


async def execute(process, request_id, source):
    await operation(
        process,
        request_id,
        "session/execute",
        request={
            "tool_call_id": f"smoke-{request_id}",
            "enabled_tools": [],
            "source": source,
            "yield_time_ms": 10000,
            "max_output_tokens": 100,
        },
    )
    started = None
    completed = None
    while started is None or completed is None:
        message = await receive(process)
        kind = message["type"]
        if kind == "cell/closed":
            continue
        if message.get("id") != request_id:
            raise RuntimeError(f"Unexpected host message type: {kind}")
        if kind == "operation/response" and started is None:
            started = result(message)
            if started["type"] != "execution/started":
                raise RuntimeError("Host did not start the JavaScript cell")
        elif kind == "execute/initialResponse" and completed is None:
            completed = result(message).get("Result")
            if completed is None:
                raise RuntimeError(
                    "Small JavaScript cell did not finish within 10 seconds"
                )
        else:
            raise RuntimeError(f"Unexpected host message type: {kind}")
    if completed["cell_id"] != started["cellId"]:
        raise RuntimeError("Execution response refers to a different cell")
    if completed["error_text"]:
        raise RuntimeError(f"JavaScript failed: {completed['error_text']}")
    if completed["content_items"] != [
        {"type": "input_text", "text": "termux-native:42"}
    ]:
        raise RuntimeError("JavaScript returned an unexpected value")


async def code_mode(host, directory):
    process = await asyncio.create_subprocess_exec(
        host,
        "--listen",
        "stdio",
        cwd=directory,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    try:
        await send(
            process,
            {
                "type": "connection/hello",
                "supportedVersions": [1],
                "requiredCapabilities": [],
                "optionalCapabilities": [],
            },
        )
        hello = await receive(process)
        if hello["type"] != "connection/ready" or hello["selectedVersion"] != 1:
            raise RuntimeError("Host did not accept protocol version 1")
        await operation(process, 1, "session/open")
        if await response(process, 1) != {
            "type": "session/ready",
            "sessionId": "termux-smoke",
        }:
            raise RuntimeError("Host did not open the temporary session")
        await execute(
            process,
            2,
            "const termuxAnswer = await Promise.resolve(6 * 7); "
            "store('termux-smoke-answer', termuxAnswer); "
            "text('termux-native:' + termuxAnswer);",
        )
        await execute(
            process, 3, "text('termux-native:' + load('termux-smoke-answer'));"
        )
        await operation(process, 4, "session/shutdown")
        if await response(process, 4) != {
            "type": "session/closed",
            "sessionId": "termux-smoke",
        }:
            raise RuntimeError("Host did not shut down the temporary session")
        process.stdin.close()
        if await asyncio.wait_for(process.wait(), 5) != 0:
            raise RuntimeError("Host exited unsuccessfully")
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


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
        await app_server(binary_dir / "codex", args.candidate.resolve())
        await asyncio.wait_for(
            code_mode(binary_dir / "codex-code-mode-host", directory), 45
        )
        print(
            "PASS: CLI, protected Unix socket, JavaScript, Promise, cross-cell state, host shutdown",
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

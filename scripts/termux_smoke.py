#!/usr/bin/env python3
"""Check a candidate's CLI and real code-mode host without a Codex account."""

import argparse
import asyncio
import json
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


async def check(args):
    binary_dir = args.candidate.resolve() / "bin"
    with tempfile.TemporaryDirectory(prefix="codex-termux-smoke-") as directory:
        await command(
            binary_dir / "codex", "--version", directory=directory, timeout=10
        )
        await asyncio.wait_for(
            code_mode(binary_dir / "codex-code-mode-host", directory), 45
        )
        print(
            "PASS: CLI, JavaScript, Promise, cross-cell state, host shutdown",
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
        parser.exit(1, "FAIL: CLI, code-mode host or network probe timed out\n")
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

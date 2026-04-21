#!/usr/bin/env python3
import argparse
import asyncio
import statistics
import time
from dataclasses import asdict, dataclass


@dataclass
class RequestResult:
    index: int
    prompt: str
    expected: str
    returncode: int | None
    elapsed_s: float
    ok: bool
    stdout: str
    stderr: str
    error: str | None


async def run_one(
    index: int,
    model: str,
    prompt_prefix: str,
    timeout_s: float,
    start_gate: asyncio.Event,
    semaphore: asyncio.Semaphore,
) -> RequestResult:
    expected = f"{prompt_prefix}-{index}"
    prompt = f"Reply with exactly: {expected}"

    await start_gate.wait()

    async with semaphore:
        started = time.perf_counter()
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                "claude",
                "-p",
                "--model",
                model,
                prompt,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_b, stderr_b = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout_s,
            )
            elapsed_s = time.perf_counter() - started
            stdout = stdout_b.decode("utf-8", errors="replace").strip()
            stderr = stderr_b.decode("utf-8", errors="replace").strip()
            ok = process.returncode == 0 and stdout == expected
            return RequestResult(
                index=index,
                prompt=prompt,
                expected=expected,
                returncode=process.returncode,
                elapsed_s=elapsed_s,
                ok=ok,
                stdout=stdout,
                stderr=stderr,
                error=None,
            )
        except asyncio.TimeoutError:
            if process is not None and process.returncode is None:
                process.kill()
                await process.communicate()
            return RequestResult(
                index=index,
                prompt=prompt,
                expected=expected,
                returncode=None,
                elapsed_s=time.perf_counter() - started,
                ok=False,
                stdout="",
                stderr="",
                error=f"timeout after {timeout_s:.1f}s",
            )
        except Exception as exc:
            return RequestResult(
                index=index,
                prompt=prompt,
                expected=expected,
                returncode=None,
                elapsed_s=time.perf_counter() - started,
                ok=False,
                stdout="",
                stderr="",
                error=str(exc),
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run concurrent Claude CLI requests using the current local Claude configuration.",
    )
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--total", type=int, default=5)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--prompt-prefix", default="pong")
    parser.add_argument("--timeout", type=float, default=180.0)
    return parser


async def amain() -> int:
    args = build_parser().parse_args()
    if args.total < 1:
        raise SystemExit("--total must be >= 1")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be >= 1")

    start_gate = asyncio.Event()
    semaphore = asyncio.Semaphore(args.concurrency)

    tasks = [
        asyncio.create_task(
            run_one(
                index=i,
                model=args.model,
                prompt_prefix=args.prompt_prefix,
                timeout_s=args.timeout,
                start_gate=start_gate,
                semaphore=semaphore,
            )
        )
        for i in range(1, args.total + 1)
    ]

    suite_started = time.perf_counter()
    start_gate.set()
    results = await asyncio.gather(*tasks)
    suite_elapsed_s = time.perf_counter() - suite_started

    successes = [r for r in results if r.ok]
    elapsed_values = [r.elapsed_s for r in results]

    print(
        f"summary total={len(results)} success={len(successes)} "
        f"failure={len(results) - len(successes)} concurrency={args.concurrency} "
        f"suite_elapsed_s={suite_elapsed_s:.2f} "
        f"min_s={min(elapsed_values):.2f} avg_s={statistics.mean(elapsed_values):.2f} "
        f"max_s={max(elapsed_values):.2f}"
    )

    for result in results:
        status = "ok" if result.ok else "fail"
        print(
            f"request index={result.index} status={status} returncode={result.returncode} "
            f"elapsed_s={result.elapsed_s:.2f} stdout={result.stdout!r}"
        )
        if result.error:
            print(f"error index={result.index} detail={result.error}")
        if result.stderr:
            print(f"stderr index={result.index} detail={result.stderr!r}")

    return 0 if len(successes) == len(results) else 1


def main() -> None:
    raise SystemExit(asyncio.run(amain()))


if __name__ == "__main__":
    main()

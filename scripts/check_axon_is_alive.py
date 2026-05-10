#!/usr/bin/env python3
"""
Check whether a miner's axon endpoint is reachable.

**Default (`--probe tcp`)** — TCP connect to the axon IP:port from chain (or overrides).
No wallet, no hotkey files. This matches what validators need first: a routable,
listening port. It does *not* run the full signed `IsAlive` synapse.

**Optional (`--probe dendrite`)** — Same call as validators
``neurons/validators/validator.py::check_uid``: ``await dendrite(axon, IsAlive(), ...)``.
Runs a **TCP preflight** to the same host:port first so logs can separate “port open”
from “signed IsAlive finished in time”. On failure, **DIAGNOSIS** lines explain what
Bittensor set on the synapse (timeout vs axon error), without assuming you are a validator.

- With **`--ephemeral`**: signs using a throwaway in-memory key (no wallet files).
  Production miners usually **blacklist** callers whose hotkey is not on the subnet
  metagraph, so `is_success` may be false even when the axon is up. Use this only
  for debugging transport vs blacklist.

- Without **`--ephemeral`**: uses ``--wallet-name`` / ``--wallet-hotkey`` as **file
  names** under ``~/.bittensor/wallets/``; that hotkey must be registered on the
  subnet for the miner to accept the call.

Examples:

  # No wallet — TCP only (default)
  poetry run python scripts/check_axon_is_alive.py --netuid 22 --uid 67

  # Full IsAlive with on-disk wallet (hotkey name = file name, not ss58)
  poetry run python scripts/check_axon_is_alive.py --netuid 22 --uid 67 \\
      --probe dendrite --wallet-name miner --wallet-hotkey default

  # IsAlive with ephemeral signer (may be blacklisted)
  poetry run python scripts/check_axon_is_alive.py --netuid 22 --uid 67 \\
      --probe dendrite --ephemeral
"""

from __future__ import annotations

import argparse
import asyncio
import os
import socket
import sys
from pathlib import Path
from typing import Any, Tuple


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _bootstrap_imports() -> None:
    root = _repo_root()
    source = root / "source"
    for p in (root, source):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def _load_dotenv_miners() -> None:
    env_path = _repo_root() / "source" / "neurons" / "miners" / ".env"
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            if (v.startswith('"') and v.endswith('"')) or (
                v.startswith("'") and v.endswith("'")
            ):
                v = v[1:-1]
            os.environ[k] = v


def _axon_info_class():
    import bittensor as bt

    try:
        from bittensor.core.chain_data import AxonInfo

        return AxonInfo
    except ImportError:
        return bt.AxonInfo


def _axon_with_overrides(base_axon: Any, ip: str | None, port: int | None) -> Any:
    AxonInfo = _axon_info_class()
    if hasattr(base_axon, "model_dump"):
        data = base_axon.model_dump()
    else:
        data = base_axon.dict()
    if ip is not None:
        data["ip"] = ip
    if port is not None:
        data["port"] = int(port)
    return AxonInfo(**data)


_TERMINAL_DEBUG_FIELDS = (
    "status_code",
    "status_message",
    "process_time",
    "ip",
    "port",
    "version",
)


def _print_synapse_terminal(synapse: Any, side: str) -> None:
    """Log Bittensor TerminalInfo (dendrite vs axon) for debugging failed calls."""
    term = getattr(synapse, side, None)
    if term is None:
        print(f"[check_axon_is_alive] synapse.{side}=None")
        return
    for field in _TERMINAL_DEBUG_FIELDS:
        if not hasattr(term, field):
            continue
        print(f"[check_axon_is_alive] synapse.{side}.{field}={getattr(term, field)!r}")


def _isalive_failure_summary(synapse: Any) -> str:
    """Single line from synapse.dendrite / synapse.axon (what Bittensor actually set)."""
    parts: list[str] = []
    for side in ("dendrite", "axon"):
        term = getattr(synapse, side, None)
        if term is None:
            parts.append(f"{side}=None")
            continue
        code = getattr(term, "status_code", None)
        msg = getattr(term, "status_message", None)
        parts.append(f"{side} status_code={code!r} status_message={msg!r}")
    return " | ".join(parts)


def _status_code_as_str(code: Any) -> str | None:
    if code is None:
        return None
    return str(code).strip()


def _axon_terminal_missing(synapse: Any) -> bool:
    term = getattr(synapse, "axon", None)
    if term is None:
        return True
    return (
        getattr(term, "status_code", None) is None
        and getattr(term, "status_message", None) is None
    )


def _print_isalive_diagnosis(
    synapse: Any,
    *,
    timeout: float,
    tcp_preflight_ok: bool | None,
    endpoint: str,
) -> None:
    """
    Explain the failure in transport/protocol terms (what Bittensor set on the synapse).
    This script is not a subnet validator; diagnosis stays on client/HTTP facts, not stake policy.
    """
    d = getattr(synapse, "dendrite", None)
    a = getattr(synapse, "axon", None)
    d_code = _status_code_as_str(getattr(d, "status_code", None)) if d else None
    d_msg = getattr(d, "status_message", None) if d else None
    a_code = _status_code_as_str(getattr(a, "status_code", None)) if a else None
    a_msg = getattr(a, "status_message", None) if a else None
    axon_empty = _axon_terminal_missing(synapse)

    lines: list[str] = []

    if d_code == "408" and axon_empty:
        lines.append(
            f"Dendrite client timeout: Bittensor set dendrite.status_code=408 and did not fill "
            f"synapse.axon terminal fields — the signed HTTP/synapse exchange did not complete "
            f"within {timeout}s (no parseable axon response in that window)."
        )
        if tcp_preflight_ok is True:
            lines.append(
                f"TCP connect to {endpoint} succeeded immediately before this call: the port accepts "
                f"connections, but this IsAlive request still did not finish in time (application "
                f"layer stall, slow handler, non-Bittensor listener, proxy, or client stack)."
            )
        elif tcp_preflight_ok is False:
            lines.append(
                f"TCP preflight to {endpoint} also failed — fix listening address, firewall, or "
                f"routing before interpreting dendrite output."
            )
    elif a_msg or a_code:
        lines.append(
            "Axon returned terminal fields — treat axon.status_code / axon.status_message as the "
            "server-visible outcome."
        )
    elif d_msg:
        lines.append(
            f"Dendrite terminal: status_code={d_code!r}, status_message={d_msg!r}."
        )
    else:
        lines.append(
            "No axon terminal and incomplete dendrite info — see raw synapse.* lines above."
        )

    for line in lines:
        print(f"[check_axon_is_alive] DIAGNOSIS: {line}")


def _axon_info_for_args(args, metagraph_axon: Any | None) -> Any:
    AxonInfo = _axon_info_class()

    if metagraph_axon is not None:
        if args.ip is not None or args.port is not None:
            return _axon_with_overrides(metagraph_axon, args.ip, args.port)
        return metagraph_axon

    if not args.miner_hotkey:
        raise SystemExit(
            "With no --uid, you must pass --miner-hotkey (miner ss58) for AxonInfo."
        )

    coldkey = args.miner_coldkey or args.miner_hotkey
    return AxonInfo(
        version=args.axon_version,
        ip=args.ip,
        port=int(args.port),
        ip_type=4,
        hotkey=args.miner_hotkey,
        coldkey=coldkey,
    )


def _axon_host_port(axon: Any) -> Tuple[str, int]:
    """Resolve axon.ip (may be uint32) and port for socket.connect."""
    from bittensor.utils import networking

    raw_ip = axon.ip
    port = int(axon.port)
    if isinstance(raw_ip, int):
        return networking.int_to_ip(raw_ip), port
    return str(raw_ip).strip(), port


def _tcp_probe_sync(host: str, port: int, timeout: float) -> None:
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.close()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Check miner axon reachability (TCP by default) or full IsAlive (dendrite).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--probe",
        choices=("tcp", "dendrite"),
        default="tcp",
        help="tcp: TCP connect only, no wallet (default). dendrite: signed IsAlive like validators.",
    )
    p.add_argument(
        "--ephemeral",
        action="store_true",
        help="With --probe dendrite: sign with in-memory key (no wallet files; often blacklisted).",
    )
    p.add_argument("--netuid", type=int, default=22, help="Subnet uid (default: 22).")
    p.add_argument(
        "--subtensor.network",
        dest="subtensor_network",
        default=os.environ.get("SUBTENSOR_NETWORK", "finney"),
        help="Bittensor network (default: finney or SUBTENSOR_NETWORK).",
    )
    p.add_argument(
        "--subtensor.chain_endpoint",
        dest="chain_endpoint",
        default=os.environ.get(
            "SUBTENSOR_CHAIN_ENDPOINT",
            "wss://entrypoint-finney.opentensor.ai:443",
        ),
        help="WebSocket RPC endpoint.",
    )
    p.add_argument(
        "--uid",
        type=int,
        default=None,
        help="Miner UID; loads axon from metagraph (recommended).",
    )
    p.add_argument(
        "--ip",
        type=str,
        default=None,
        help="Override axon IP (with --uid) or set IP for manual mode.",
    )
    p.add_argument(
        "--port",
        type=int,
        default=None,
        help="Override axon port (with --uid) or set port for manual mode.",
    )
    p.add_argument(
        "--miner-hotkey",
        dest="miner_hotkey",
        default=None,
        help="Miner hotkey ss58 (required for manual mode without --uid).",
    )
    p.add_argument(
        "--miner-coldkey",
        dest="miner_coldkey",
        default=None,
        help="Optional coldkey ss58 for AxonInfo (defaults to miner-hotkey).",
    )
    p.add_argument(
        "--axon-version",
        dest="axon_version",
        type=int,
        default=9009000,
        help="AxonInfo version field for manual mode (default: 9009000).",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=10.0,
        help="TCP or dendrite timeout seconds (validator IsAlive uses 10).",
    )
    p.add_argument(
        "--wallet-name",
        dest="wallet_name",
        default=os.environ.get("WALLET_NAME", "default"),
        help="With dendrite (no --ephemeral): coldkey wallet name on disk.",
    )
    p.add_argument(
        "--wallet-hotkey",
        dest="wallet_hotkey",
        default=os.environ.get("WALLET_HOTKEY", "default"),
        help="With dendrite (no --ephemeral): hotkey **file name** under that wallet.",
    )
    return p


async def _load_axon_and_subtensor(args: argparse.Namespace) -> Tuple[Any, Any]:
    import bittensor as bt

    metagraph_axon = None
    subtensor = None
    if args.uid is not None:
        try:
            subtensor = bt.AsyncSubtensor(
                network=args.subtensor_network,
                chain_endpoint=args.chain_endpoint,
            )
        except TypeError:
            subtensor = bt.AsyncSubtensor(network=args.subtensor_network)
        await subtensor.initialize()
        metagraph = await subtensor.metagraph(args.netuid)
        uids = metagraph.uids
        n = int(uids.shape[0]) if hasattr(uids, "shape") else len(uids)
        if args.uid < 0 or args.uid >= n:
            raise SystemExit(f"UID {args.uid} out of range (metagraph size {n}).")
        metagraph_axon = metagraph.axons[args.uid]
        print(
            f"[check_axon_is_alive] netuid={args.netuid} uid={args.uid} "
            f"chain_axon_ip={getattr(metagraph_axon, 'ip', '?')} "
            f"port={getattr(metagraph_axon, 'port', '?')} "
            f"hotkey={str(getattr(metagraph_axon, 'hotkey', ''))[:20]}…"
        )

    axon = _axon_info_for_args(args, metagraph_axon)
    return axon, subtensor


async def _run(args: argparse.Namespace) -> int:
    _bootstrap_imports()
    _load_dotenv_miners()

    import bittensor as bt

    from desearch.protocol import IsAlive

    if args.uid is None:
        if args.ip is None or args.port is None:
            raise SystemExit("Manual mode requires --ip and --port (and --miner-hotkey).")

    subtensor = None
    try:
        axon, subtensor = await _load_axon_and_subtensor(args)
        host, port = _axon_host_port(axon)
        print(
            f"[check_axon_is_alive] probe={args.probe} endpoint={host}:{port} "
            f"(axon hotkey={str(axon.hotkey)[:16]}…)"
        )

        if args.probe == "tcp":
            try:
                await asyncio.to_thread(_tcp_probe_sync, host, port, args.timeout)
            except OSError as e:
                print(
                    f"[check_axon_is_alive] TCP connect failed: {e}\n"
                    "[check_axon_is_alive] RESULT: NOT REACHABLE (fix IP/port/firewall / axon not listening)"
                )
                return 1
            print(
                "[check_axon_is_alive] RESULT: TCP OK (port accepts connections). "
                "For signed IsAlive like validators: --probe dendrite + wallet or --ephemeral"
            )
            return 0

        # dendrite / IsAlive — TCP preflight so we can separate L4 vs L7 in DIAGNOSIS
        tcp_preflight_ok: bool | None = None
        ep = f"{host}:{port}"
        try:
            await asyncio.to_thread(_tcp_probe_sync, host, port, args.timeout)
            tcp_preflight_ok = True
            print(
                f"[check_axon_is_alive] TCP preflight OK ({ep}) — proceeding with dendrite IsAlive"
            )
        except OSError as e:
            tcp_preflight_ok = False
            print(
                f"[check_axon_is_alive] TCP preflight failed ({ep}): {e}\n"
                "[check_axon_is_alive] Still attempting dendrite (may duplicate the failure mode)."
            )

        # dendrite / IsAlive
        if args.ephemeral:
            from bittensor_wallet import Keypair

            wallet = bt.Wallet()
            wallet.set_hotkey(
                Keypair.create_from_mnemonic(Keypair.generate_mnemonic()),
                encrypt=False,
                overwrite=True,
            )
            print(
                "[check_axon_is_alive] dendrite signer (ephemeral) "
                f"{wallet.hotkey.ss58_address}"
            )
        else:
            try:
                wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
            except Exception as e:
                print(
                    f"[check_axon_is_alive] wallet load failed: {e}\n"
                    "Use --probe tcp for no wallet, or --probe dendrite --ephemeral, "
                    "or fix --wallet-name / --wallet-hotkey (hotkey is a file name, not ss58)."
                )
                return 1
            print(
                f"[check_axon_is_alive] dendrite signer={wallet.hotkey.ss58_address} "
                "(caller hotkey for this signed request)"
            )

        dendrite = bt.Dendrite(wallet=wallet)
        synapse = IsAlive()
        response = await dendrite(
            axon,
            synapse,
            deserialize=False,
            timeout=args.timeout,
        )

        ok = bool(getattr(response, "is_success", False))
        manifest = getattr(response, "manifest", None)

        print(f"[check_axon_is_alive] is_success={ok}")
        _print_synapse_terminal(response, "dendrite")
        _print_synapse_terminal(response, "axon")
        for attr in ("answer", "completion"):
            val = getattr(response, attr, None)
            if val not in (None, "", []):
                print(f"[check_axon_is_alive] synapse.{attr}={val!r}")
        if manifest:
            print(f"[check_axon_is_alive] manifest_keys={list(manifest.keys())}")
        else:
            print("[check_axon_is_alive] manifest=None")

        if ok:
            print(
                "[check_axon_is_alive] RESULT: IsAlive OK (validator would add this UID to available_uids)"
            )
            return 0
        print(
            "[check_axon_is_alive] RESULT: IsAlive not successful — "
            + _isalive_failure_summary(response)
        )
        _print_isalive_diagnosis(
            response,
            timeout=args.timeout,
            tcp_preflight_ok=tcp_preflight_ok,
            endpoint=ep,
        )
        return 2
    except Exception as e:
        print(f"[check_axon_is_alive] ERROR: {type(e).__name__}: {e}")
        return 1
    finally:
        if subtensor is not None:
            try:
                await subtensor.close()
            except Exception:
                pass


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())

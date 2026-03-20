#!/usr/bin/env python3
"""
Unified CLI for Claude API Proxy services.

Usage:
    python cli.py proxy [--port PORT]       # Launch the API proxy
    python cli.py orchestrator [--port PORT] # Launch the orchestrator service
    python cli.py install                    # Install dependencies only

This script auto-installs missing dependencies before importing them.
"""
import subprocess
import sys
from pathlib import Path

# Ensure sibling modules (main.py, orchestrator.py) are importable from anywhere
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

# ---------------------------------------------------------------------------
# Dependency bootstrap (runs before any third-party imports)
# ---------------------------------------------------------------------------
REQUIRED_PACKAGES = {
    "fastapi": "fastapi",
    "uvicorn": "uvicorn",
    "httpx": "httpx",
    "pydantic": "pydantic",
    "dotenv": "python-dotenv",
}


def check_and_install_deps(verbose: bool = True) -> bool:
    """
    Check for missing dependencies and install them.
    Returns True if all deps are available (installed or already present).
    """
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)

    if not missing:
        return True

    if verbose:
        print(f"Installing missing dependencies: {', '.join(missing)}", file=sys.stderr)

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", *missing],
            stdout=subprocess.DEVNULL if not verbose else None,
        )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Failed to install dependencies: {e}", file=sys.stderr)
        return False


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Claude API Proxy CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python cli.py proxy                  # Start proxy on default port 8080
    python cli.py proxy --port 9000      # Start proxy on port 9000
    python cli.py orchestrator           # Start orchestrator on default port 8080
    python cli.py install                # Install dependencies only
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # proxy subcommand
    proxy_parser = subparsers.add_parser("proxy", help="Launch the API request proxy")
    proxy_parser.add_argument("--port", "-p", type=int, default=8080, help="Port to listen on (default: 8080)")
    proxy_parser.add_argument("--log-file", "-l", type=str, default="requests.jsonl", help="Log file path")

    # orchestrator subcommand
    orch_parser = subparsers.add_parser("orchestrator", help="Launch the orchestrator service")
    orch_parser.add_argument("--port", "-p", type=int, default=8080, help="Port to listen on (default: 8080)")
    orch_parser.add_argument("--base-dir", "-b", type=str, default=None, help="Base directory for work_dir resolution")
    orch_parser.add_argument("--keep-logs", action="store_true", help="Keep per-job log files after completion")

    # install subcommand
    subparsers.add_parser("install", help="Install dependencies only")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # Handle install command before dependency check
    if args.command == "install":
        if check_and_install_deps(verbose=True):
            print("All dependencies installed successfully.")
            sys.exit(0)
        else:
            sys.exit(1)

    # Check/install dependencies for other commands
    if not check_and_install_deps(verbose=True):
        sys.exit(1)

    # Now safe to import third-party modules
    import os
    import uvicorn

    if args.command == "proxy":
        os.environ["PORT"] = str(args.port)
        os.environ["LOG_FILE"] = args.log_file

        # Import proxy app
        from main import app

        # Define port-reporting server inline (so main.py stays unchanged)
        class PortReportingServer(uvicorn.Server):
            async def startup(self, sockets=None):
                await super().startup(sockets=sockets)
                if not self.started:
                    return  # lifespan failed
                bound_port = self.servers[0].sockets[0].getsockname()[1]
                print(f"PROXY_PORT={bound_port}", flush=True)

        config = uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="warning")
        server = PortReportingServer(config=config)

        import asyncio
        asyncio.run(server.serve())

    elif args.command == "orchestrator":
        if args.base_dir:
            os.environ["BASE_DIR"] = args.base_dir
        if args.keep_logs:
            os.environ["KEEP_LOGS"] = "true"

        # Import and run orchestrator
        from orchestrator import app

        uvicorn.run(app, host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()

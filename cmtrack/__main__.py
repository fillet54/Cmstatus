"""Run the dev server: python -m cmtrack [--port 5000]"""
import argparse

from . import create_app

parser = argparse.ArgumentParser(description="cmtrack dev server")
parser.add_argument("--host", default="127.0.0.1")
parser.add_argument("--port", type=int, default=5000)
parser.add_argument("--debug", action="store_true")
args = parser.parse_args()
create_app().run(host=args.host, port=args.port, debug=args.debug)

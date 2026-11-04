"""Read remote coordinator status without blocking the UI."""
import argparse
import json
import os
from cluster_worker import request

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', required=True)
    args = parser.parse_args()
    print(json.dumps(request(args.server, os.environ.get('HUBBLE_QUEUE_TOKEN', ''), '/status', {}), indent=2))

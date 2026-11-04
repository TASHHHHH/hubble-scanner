"""Internet worker endpoint: direct TLS, per-PC revocable wiki-only credentials."""
import argparse
import os
import ssl
from coordinator import Queue,make_server

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--db',required=True)
    sub=p.add_subparsers(dest='action',required=True)
    a=sub.add_parser('issue');a.add_argument('name');a.add_argument('--role',choices=['wiki','adaptive'],default='adaptive')
    a=sub.add_parser('revoke');a.add_argument('name')
    a=sub.add_parser('serve');a.add_argument('--cert',required=True);a.add_argument('--key',required=True)
    a.add_argument('--host',default='0.0.0.0');a.add_argument('--port',type=int,default=8766)
    args=p.parse_args();q=Queue(args.db,wiki_slots=10,mast_slots=1)
    if args.action=='issue':print(q.worker_token(args.name,args.role));return
    if args.action=='revoke':q.revoke_worker(args.name);return
    q.configure_levels()
    server=make_server(q,args.host,args.port,os.environ.get('HUBBLE_QUEUE_TOKEN',''),restricted=True)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.minimum_version=ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(args.cert,args.key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    from notification_service import start
    notification_stop=start(q)
    try:server.serve_forever()
    finally:notification_stop.set();server.server_close()

if __name__=='__main__':main()

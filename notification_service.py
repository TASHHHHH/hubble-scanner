"""Coordinator-only durable notification outbox. No remote send endpoint."""
import threading


def deliver_one(queue):
    from secret_store import load
    from investigator import notify
    credentials=load()
    if not credentials:return False
    with queue.db() as c:
        c.execute('BEGIN IMMEDIATE')
        row=c.execute("SELECT * FROM notifications WHERE state='pending' LIMIT 1").fetchone()
        if not row:return False
        c.execute("UPDATE notifications SET state='sending' WHERE job_key=?",(row['job_key'],))
    # Do not retry an uncertain delivery: a crash leaves 'sending' for review.
    status=notify(row['message'],credentials)
    with queue.db() as c:
        c.execute('UPDATE notifications SET state=? WHERE job_key=?',(status,row['job_key']))
    return True


def start(queue):
    stop=threading.Event()
    def loop():
        while not stop.wait(2):
            try:deliver_one(queue)
            except Exception:pass  # Never log credential-related exception contents.
    thread=threading.Thread(target=loop,name='telegram-notification-outbox',daemon=True);thread.start()
    from telegram_control import poll
    control=threading.Thread(target=poll,args=(queue,stop),name='telegram-control-poll',daemon=True)
    control.start()
    return stop

"""Hubble Scout desktop application. Run with Python 3.11: python app.py."""
import json
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

ROOT = Path(__file__).resolve().parent


class Runner:
    """One foreground pipeline; subprocess output never touches Tk from a thread."""
    def __init__(self):
        self.events = queue.Queue()
        self.busy = False
        self.cancelled = threading.Event()
        self.process = None
        self.lock = threading.Lock()

    def start(self, commands, cwd, env=None):
        if self.busy:
            raise RuntimeError('A task is already running. Wait or stop it first.')
        self.busy = True
        self.cancelled.clear()
        def work():
            code = 0
            try:
                for command in commands:
                    with self.lock:
                        if self.cancelled.is_set():
                            code = 130
                            break
                        self.events.put(('log', '\n> ' + ' '.join(map(str, command)) + '\n'))
                        self.process = subprocess.Popen(command, cwd=cwd, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding='utf-8', errors='replace',
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                    for line in self.process.stdout:
                        self.events.put(('log', line))
                    code = self.process.wait()
                    self.process.stdout.close()
                    if code or self.cancelled.is_set():
                        break
            except Exception as exc:
                code = 1
                self.events.put(('log', f'Error: {exc}\n'))
            finally:
                with self.lock:
                    self.process = None
                self.busy = False
                self.events.put(('done', 130 if self.cancelled.is_set() else code))
        threading.Thread(target=work, daemon=True).start()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            p = self.process
            if p and p.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(p.pid), '/T', '/F'],
                                   capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    p.terminate()


def command(module, *args):
    return [sys.executable, '-u', str(ROOT / (module + '.py')), *map(str, args)]


class App(tk.Tk):
    def __init__(self, workspace=None):
        super().__init__()
        self.title('Hubble Scanner')
        self.geometry(f'{min(1180, self.winfo_screenwidth() - 80)}x{min(760, self.winfo_screenheight() - 100)}')
        self.minsize(900, 650)
        self.configure(bg='#ffffff')
        self.runner = Runner()
        self.server = None
        self.server_thread = None
        self.lan_server = None
        self.notification_stop = None
        self.workspace = tk.StringVar(value=str(Path(workspace or Path.home() / 'HubbleScout').resolve()))
        self.host = tk.StringVar(value='127.0.0.1')
        self.endpoint = tk.StringVar(value='http://127.0.0.1:8765')
        self.token = tk.StringVar(value=secrets.token_urlsafe(32))
        self.state = tk.StringVar(value='Ready')
        self.metrics = tk.StringVar(value='No queue loaded')
        self.search = tk.StringVar()
        self.rows = []
        self.alerted = set()
        self.live_tick = 0
        self._theme()
        top = ttk.Frame(self, padding=(22, 14)); top.pack(fill='x')
        ttk.Label(top, text='Hubble Scanner', style='Title.TLabel').pack(side='left')
        ttk.Button(top, text='View Logs', command=self.show_logs).pack(side='right', padx=(8, 0))
        ttk.Button(top, text='About', command=self.about).pack(side='right', padx=(8, 0))
        ttk.Button(top, text='Open Folder', command=self.open_folder).pack(side='right', padx=(8, 0))
        bar = ttk.Frame(self, padding=(22, 4)); bar.pack(fill='x')
        ttk.Label(bar, text='Workspace').pack(side='left', padx=(0, 12))
        self.path_entry = ttk.Entry(bar, textvariable=self.workspace); self.path_entry.pack(side='left', fill='x', expand=True)
        ttk.Button(bar, text='Choose…', command=self.choose_workspace).pack(side='left', padx=8)
        statusbar = ttk.Frame(self, padding=(18, 5), style='Statusbar.TFrame'); statusbar.pack(side='bottom', fill='x')
        ttk.Label(statusbar, textvariable=self.state, style='Statusbar.TLabel').pack(side='left')
        ttk.Label(statusbar, text='Ctrl+L opens logs', style='Statusbar.TLabel').pack(side='right')
        bottom = ttk.Frame(self, padding=(22, 8)); bottom.pack(side='bottom', fill='x')
        ttk.Button(bottom, text='Stop', command=self.stop_task).pack(side='right', padx=(12, 0))
        self.progress = ttk.Progressbar(bottom, mode='indeterminate'); self.progress.pack(side='left', fill='x', expand=True)
        self.tabs = ttk.Notebook(self); self.tabs.pack(fill='both', expand=True, padx=18, pady=(8, 10))
        self.scan_tab(); self.results_tab(); self.quality_tab(); self.cluster_tab()
        self.live_tab()
        self.log_window = tk.Toplevel(self); self.log_window.title('Hubble Scanner — Logs'); self.log_window.geometry('900x460'); self.log_window.withdraw()
        self.log_window.protocol('WM_DELETE_WINDOW', self.log_window.withdraw)
        self.log = tk.Text(self.log_window, height=5, bg='#ffffff', fg='#3b413c', insertbackground='#3b413c', relief='flat', font=('Consolas', 9), state='disabled')
        self.log.pack(fill='both', expand=True)
        self.bind('<Control-l>', lambda _event: self.show_logs())
        self.protocol('WM_DELETE_WINDOW', self.close)
        self.after(150, self.poll)

    def _theme(self):
        from app_theme import apply_theme
        apply_theme(self)

    def show_logs(self):
        self.log_window.deiconify(); self.log_window.lift()

    def about(self):
        messagebox.showinfo('Hubble Scanner', 'Hubble archive research and FITS quality review.\n\nWikipedia attribution gaps and ranking scores are candidate-selection evidence, not proof of novelty. Publication status remains not assessed.')

    def tab(self, name, heading, description):
        f = ttk.Frame(self.tabs, padding=16); self.tabs.add(f, text=name)
        ttk.Label(f, text=heading, style='Heading.TLabel').pack(anchor='w')
        ttk.Label(f, text=description, wraplength=1000, style='Muted.TLabel').pack(anchor='w', pady=(6, 16))
        return f

    def scan_tab(self):
        f = self.tab('Scan', 'Catalog scan', 'Load a catalog, run the queue, then collect candidates for review.')
        for title, description, callback in [
            ('Try offline demo', 'Synthetic catalog, three FITS bands and sample results. No API calls.', self.demo),
            ('Build catalog', 'Download OpenNGC / Messier / IC; optional Arp and Sharpless via VizieR.', self.catalog),
            ('Import catalog & seed queue', 'Choose JSONL from a previous run. Known aliases and identical queries are deduplicated.', self.seed),
            ('Join local pool', 'Scan Wikipedia or investigate candidates according to the backlog level.', self.local_scan),
            ('Collect results', 'Snapshot the queue, compute overlapping filter triples and write CSV files.', self.collect)]:
            row = ttk.Frame(f); row.pack(fill='x', pady=5)
            ttk.Button(row, text=title, width=28, command=callback).pack(side='left')
            ttk.Label(row, text=description, wraplength=650).pack(side='left', padx=18)
        self.vizier = tk.BooleanVar(value=False)
        ttk.Checkbutton(f, text='Include Arp and Sharpless (remote VizieR service may be slow)', variable=self.vizier).pack(anchor='w', pady=8)

    def results_tab(self):
        f = self.tab('Candidates', 'Candidates', 'Double-click a target to view its evidence. Publication status is not assessed.')
        row = ttk.Frame(f); row.pack(fill='x', pady=(0, 10))
        ttk.Entry(row, textvariable=self.search).pack(side='left', fill='x', expand=True)
        self.search.trace_add('write', lambda *_: self.render_rows())
        for label, callback in [('Load results', self.load_results), ('TSDP CSV', self.tsdp), ('DINOv2 rank', self.rank)]:
            ttk.Button(row, text=label, command=callback).pack(side='left', padx=5)
        cols = ('id', 'wiki_status', 'observation_count', 'rgb_group_count', 'priority_score')
        self.table = ttk.Treeview(f, columns=cols, show='headings', height=8)
        for col, title in zip(cols, ('Target', 'Wikipedia evidence', 'Exposures', 'RGB groups', 'Priority')):
            self.table.heading(col, text=title); self.table.column(col, width=150)
        scroll = ttk.Scrollbar(f, orient='vertical', command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set); scroll.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True); self.table.bind('<Double-1>', self.detail)

    def quality_tab(self):
        f = self.tab('FITS Quality', 'FITS quality', 'Inspect WCS overlap and DQ / ERR / WHT masks. Files are read-only; common coverage is sampled.')
        ttk.Button(f, text='Choose FITS files & assess', command=self.quality).pack(anchor='w')
        self.quality_text = tk.Text(f, height=10, bg='#ffffff', fg='#3b413c', relief='flat', wrap='word', state='disabled')
        self.quality_text.pack(fill='both', expand=True, pady=12)

    def cluster_tab(self):
        f = self.tab('Workers', 'Worker connections', 'For PCs on the same network, create an invite on the coordinator and open it on the worker.')
        row = ttk.Frame(f); row.pack(fill='x', pady=10)
        ttk.Button(row, text='Create worker invite…', command=self.create_invite).pack(side='left')
        ttk.Button(row, text='Join with invite…', command=self.join_invite).pack(side='left', padx=8)
        ttk.Label(f, textvariable=self.metrics).pack(anchor='w', pady=4)
        manual = ttk.Frame(f)
        def toggle_manual():
            if manual.winfo_manager(): manual.pack_forget()
            else: manual.pack(fill='x')
        ttk.Button(f, text='Manual connection settings', command=toggle_manual).pack(anchor='w', pady=4)
        self.server_fields = []
        for label, var, hidden in [('Local coordinator address', self.host, False), ('Worker HTTPS server URL', self.endpoint, False), ('Credential (worker token on remote PCs)', self.token, True)]:
            row = ttk.Frame(manual); row.pack(fill='x', pady=4)
            ttk.Label(row, text=label, width=43).pack(side='left')
            entry = ttk.Entry(row, textvariable=var, show='•' if hidden else '')
            entry.pack(side='left', fill='x', expand=True)
            if var is self.host or var is self.token: self.server_fields.append(entry)
        row = ttk.Frame(manual); row.pack(fill='x', pady=12)
        for label, callback in [('Start coordinator', self.start_server), ('Issue worker token', self.copy_token), ('Join adaptive pool', self.worker), ('Refresh status', self.status)]:
            ttk.Button(row, text=label, command=callback).pack(side='left', padx=(0, 8))
        ttk.Button(f,text='Encrypted Telegram settings',command=self.telegram_settings).pack(anchor='w')

    def telegram_settings(self):
        token=simpledialog.askstring('Coordinator notifications','Bot token (saved only on this PC with Windows encryption):',show='*',parent=self)
        if not token:return
        chat=simpledialog.askstring('Coordinator notifications','Destination chat ID:',parent=self)
        if not chat:return
        try:
            from secret_store import save
            save(token,chat)
            messagebox.showinfo('Saved','Encrypted for this Windows user. Workers never receive the Telegram token.')
        except Exception:messagebox.showerror('Not saved','Could not save encrypted notification settings.')

    def live_tab(self):
        f=self.tab('Live List','No Hubble attribution detected','Live coordinator results. Matches require two bands with footprints covering the target center. No science FITS are downloaded.')
        cols=('id','mast_state','match','processing','notification')
        self.live_table=ttk.Treeview(f,columns=cols,show='headings')
        for c,label in zip(cols,('Object','MAST queue','Bands','Preview / DINOv2','Notification')):
            self.live_table.heading(c,text=label);self.live_table.column(c,width=160)
        self.live_table.pack(fill='both',expand=True)
        ttk.Button(f,text='Join local adaptive pool',command=self.local_scan,style='Accent.TButton').pack(anchor='w',pady=10)

    def refresh_live(self):
        path=self.workspace_path()/'queue.sqlite'
        if not path.exists():return
        from coordinator import Queue
        rows=Queue(path).candidates()
        level=Queue(path).status()['level']
        self.metrics.set(f"Backlog: {level['backlog']} | Investigator capacity: {level['investigators']} | Other workers scan Wikipedia")
        self.live_table.delete(*self.live_table.get_children())
        for row in rows:
            inv=row['investigation'];match=inv.get('match') or {}
            self.live_table.insert('','end',values=(row['id'],row['mast_state'],', '.join(match.get('filters',[])),inv.get('status',''),inv.get('notification','')))
            if match and row['id'] not in self.alerted:
                self.alerted.add(row['id']);self.bell()
                self.state.set('Match: '+row['id']+' — '+', '.join(match.get('filters',[])))

    def workspace_path(self):
        path = Path(self.workspace.get()).expanduser().resolve(); path.mkdir(parents=True, exist_ok=True)
        return path

    def choose_workspace(self):
        if self.runner.busy or self.server:
            messagebox.showinfo('Workspace in use', 'Finish the active task and restart the app to change a serving workspace.'); return
        path = filedialog.askdirectory()
        if path: self.workspace.set(path)

    def open_folder(self):
        path = self.workspace_path()
        if os.name == 'nt': os.startfile(path)
        else: messagebox.showinfo('Workspace', str(path))

    def run(self, commands, investigator=False, *, worker_token=None, ca_bundle=None):
        try:
            path = self.workspace_path()
            env = os.environ.copy(); env['HUBBLE_CACHE_DIR'] = str(path / '.cache'); env['HUBBLE_QUEUE_TOKEN'] = worker_token or self.token.get()
            if ca_bundle: env['HUBBLE_CA_BUNDLE'] = str(ca_bundle)
            env.pop('HUBBLE_INVESTIGATE_DIR',None)
            if investigator:env['HUBBLE_INVESTIGATE_DIR']=str(path/'investigations')
            env.pop('HUBBLE_TELEGRAM_TOKEN',None);env.pop('HUBBLE_TELEGRAM_CHAT_ID',None)
            self.runner.start(commands, path, env)
            self.path_entry.configure(state='disabled'); self.progress.start(12); self.state.set('Working • details below')
        except Exception as exc: messagebox.showerror('Cannot start', str(exc))

    def demo(self): self.run([command('demo', '--out', self.workspace_path() / 'demo')])
    def catalog(self): self.run([command('scanner', 'catalog', '--out', self.workspace_path() / 'catalog.jsonl', *(['--with-vizier'] if self.vizier.get() else []))])
    def seed(self):
        path = filedialog.askopenfilename(title='Select catalog JSONL', filetypes=[('JSON Lines', '*.jsonl')])
        if path: self.run([command('coordinator', '--db', self.workspace_path() / 'queue.sqlite', 'seed', '--catalog', path)])

    def start_server(self):
        if self.server: return True
        try:
            from coordinator import Queue, make_server
            if self.host.get().strip() not in ('127.0.0.1','localhost'):
                raise ValueError('GUI coordinator is local-only. Use secure_coordinator.py with a TLS certificate for remote PCs.')
            coordinator_queue=Queue(self.workspace_path() / 'queue.sqlite',wiki_slots=10,mast_slots=1)
            coordinator_queue.configure_levels()
            self.server = make_server(coordinator_queue, self.host.get().strip(), 8765, self.token.get(),restricted=True)
            from notification_service import start
            self.notification_stop=start(coordinator_queue)
            self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.server_thread.start()
            self.path_entry.configure(state='disabled')
            for entry in self.server_fields: entry.configure(state='disabled')
            self.state.set('Coordinator listening on port 8765 • leave this app open for workers')
            return True
        except Exception as exc:
            self.server = None; messagebox.showerror('Coordinator error', str(exc)); return False

    def create_invite(self):
        from lan_pairing import lan_addresses, start_listener
        from coordinator import Queue
        from runtime import atomic_json
        try:
            if not self.lan_server:
                addresses = lan_addresses()
                if not addresses:
                    raise ValueError('Connect this PC to the worker’s network and try again.')
                address = addresses[0]
                if len(addresses) > 1:
                    address = simpledialog.askstring('Coordinator network',
                        'Choose the address reachable from the worker: ' + ', '.join(addresses),
                        initialvalue=address, parent=self)
                    if address is None: return
                    if address not in addresses: raise ValueError('Choose one of the listed addresses.')
            name = simpledialog.askstring('Create worker invite',
                'Worker PC name (using the same name replaces its credential):', parent=self)
            if not name or not name.strip(): return
            path = filedialog.asksaveasfilename(title='Save private worker invite',
                initialfile='worker.hubble-invite', defaultextension='.hubble-invite',
                filetypes=[('Hubble worker invite', '*.hubble-invite')])
            if not path or not self.start_server(): return
            q = Queue(self.workspace_path() / 'queue.sqlite', wiki_slots=10, mast_slots=1)
            if not self.lan_server:
                self.lan_server, self.lan_cert = start_listener(q, address, 8766, secrets.token_urlsafe(32))
            atomic_json(Path(path), {'format': 'hubble-lan-1', 'address': self.lan_server.server_address[0],
                'token': q.worker_token(name.strip(), 'adaptive'), 'certificate': self.lan_cert})
            self.state.set('Accepting workers on ' + self.lan_server.server_address[0] + ':8766')
            messagebox.showinfo('Invite ready', 'Send this file privately to the worker PC. On that PC, choose Workers → Join with invite. Keep this coordinator open.')
        except Exception as exc:
            messagebox.showerror('Cannot create invite', str(exc))

    def join_invite(self):
        from lan_pairing import read_invite
        path = filedialog.askopenfilename(title='Open invite from your coordinator',
            filetypes=[('Hubble worker invite', '*.hubble-invite')])
        if not path: return
        try:
            url, token, ca = read_invite(path, self.workspace_path())
            self.run([command('cluster_worker', '--server', url, '--stages', 'mast', 'wiki',
                              '--work-dir', self.workspace_path() / 'worker')],
                     worker_token=token, ca_bundle=ca)
        except Exception as exc:
            messagebox.showerror('Cannot open worker invite', str(exc))

    def copy_token(self):
        if not self.server:
            messagebox.showinfo('Coordinator required','Start the local coordinator on your trusted PC first.');return
        name=simpledialog.askstring('Worker credential','Unique PC name (reissuing revokes its previous token):',parent=self)
        if not name or not name.strip():return
        from coordinator import Queue
        token=Queue(self.workspace_path()/'queue.sqlite').worker_token(name.strip(),'adaptive')
        self.clipboard_clear();self.clipboard_append(token)
        messagebox.showinfo('Worker token copied','Paste this adaptive worker credential on that PC. Install AI dependencies there. Telegram and coordinator credentials are not included.')
    def worker(self):
        from urllib.parse import urlparse
        url=self.endpoint.get().strip();parsed=urlparse(url)
        if parsed.scheme!='https' and parsed.hostname not in ('localhost','127.0.0.1'):
            messagebox.showerror('HTTPS required','Remote workers must connect over HTTPS.');return
        self.run([command('cluster_worker', '--server', url, '--stages','mast','wiki','--work-dir', self.workspace_path() / 'worker')])
    def local_scan(self):
        if self.start_server():
            self.run([command('cluster_worker', '--server', 'http://127.0.0.1:8765','--stages','mast','wiki', '--work-dir', self.workspace_path() / 'investigator-worker')],investigator=True)
    def status(self):
        self.run([command('app_status', '--server', self.endpoint.get().strip())])
    def collect(self):
        p = self.workspace_path() / 'results'
        self.run([command('coordinator', '--db', self.workspace_path() / 'queue.sqlite', 'export', '--out', p),
                  command('scanner', 'merge', '--catalog', p / 'catalog.jsonl', '--wiki-glob', p / 'wiki_coordinator.jsonl', '--mast-glob', p / 'mast_coordinator.jsonl', '--out', p / 'candidates.jsonl'),
                  command('footprint_groups', '--results', p / 'candidates.jsonl', '--out', p / 'filter_groups.jsonl')])
    def load_results(self):
        path = filedialog.askopenfilename(title='Load candidates.jsonl', initialdir=self.workspace_path(), filetypes=[('JSON Lines', '*.jsonl')])
        if path:
            try:
                from runtime import read_rows
                self.rows = list(read_rows(path)); self.results_path = Path(path); self.render_rows()
                self.state.set(f'{len(self.rows)} candidates loaded from {path}')
            except Exception as exc: messagebox.showerror('Invalid results', str(exc))
    def render_rows(self):
        self.table.delete(*self.table.get_children()); term = self.search.get().lower()
        for i, row in enumerate(self.rows):
            if term in str(row.get('id', '')).lower():
                self.table.insert('', 'end', iid=str(i), values=[row.get(k, '') for k in self.table['columns']])
    def detail(self, _event=None):
        if not self.table.selection(): return
        row = self.rows[int(self.table.selection()[0])]
        window = tk.Toplevel(self); window.title(str(row.get('id')) + ' • Evidence'); window.geometry('850x600')
        text = tk.Text(window, wrap='word', bg='#ffffff', fg='#3b413c'); text.pack(fill='both', expand=True)
        text.insert('1.0', json.dumps(row, indent=2, ensure_ascii=False)); text.configure(state='disabled')
    def tsdp(self):
        if not hasattr(self, 'results_path'):
            messagebox.showinfo('Load results', 'Load a candidates.jsonl file first.'); return
        p = self.results_path.parent
        if any(row.get('fixture') or str(row.get('id', '')).startswith('SYNTHETIC') for row in self.rows):
            messagebox.showinfo('Synthetic data', 'Demo products cannot be exported as real download requests.'); return
        self.run([command('footprint_groups', '--results', self.results_path, '--out', p / 'filter_groups.jsonl'), command('mast_support', '--results', self.results_path, '--groups', p / 'filter_groups.jsonl', '--out', p / 'tsdp_products.csv')])
    def rank(self):
        if not hasattr(self, 'results_path'):
            messagebox.showinfo('Load results', 'Load real candidates first; install optional AI dependencies with setup.ps1 -AI.'); return
        p = self.results_path.parent
        self.run([command('scanner', 'previews', '--results', self.results_path, '--out', p / 'previews'), command('scanner', 'ai-rank', '--results', self.results_path, '--previews', p / 'previews', '--out', p / 'ai_rank.csv')])
    def quality(self):
        paths = filedialog.askopenfilenames(title='Science FITS files', filetypes=[('FITS', '*.fits *.fit *.fits.gz')])
        if paths: self.run([command('fits_quality', *paths, '--out', self.workspace_path() / 'fits_quality.json')])
    def stop_task(self):
        if self.runner.busy:
            self.state.set('Stopping • interrupted leases remain held until reviewed')
            threading.Thread(target=self.runner.stop, daemon=True).start()
    def poll(self):
        try:
            while True:
                kind, value = self.runner.events.get_nowait()
                if kind == 'log':
                    self.log.configure(state='normal'); self.log.insert('end', value); self.log.see('end')
                    if int(self.log.index('end-1c').split('.')[0]) > 3000: self.log.delete('1.0', '1000.0')
                    self.log.configure(state='disabled')
                else:
                    self.progress.stop(); self.state.set('Completed • output saved in workspace' if value == 0 else f'Task stopped / failed (exit {value}) • inspect log')
                    if not self.server: self.path_entry.configure(state='normal')
                    report = self.workspace_path() / 'fits_quality.json'
                    if report.exists():
                        self.quality_text.configure(state='normal'); self.quality_text.delete('1.0', 'end')
                        self.quality_text.insert('end', report.read_text(encoding='utf-8')); self.quality_text.configure(state='disabled')
                    if self.server:
                        from coordinator import Queue
                        s = Queue(self.workspace_path() / 'queue.sqlite').status()
                        self.metrics.set(f"{s['targets']} targets • " + ' | '.join(f"{x['stage']} {x['state']}: {x['count']}" for x in s['counts']) + f" • {len(s['attention'])} need review")
        except queue.Empty: pass
        self.live_tick+=1
        if self.live_tick%20==0:
            try:self.refresh_live()
            except Exception as exc:self.state.set('Live list unavailable: '+type(exc).__name__)
        self.after(150, self.poll)
    def close(self):
        if (self.runner.busy or self.server) and not messagebox.askyesno('Close active workspace?', 'Close the coordinator and stop this PC’s task? Remote workers will stop after a failed heartbeat. Interrupted jobs remain held for review.'):
            return
        self.runner.stop()
        if self.notification_stop:self.notification_stop.set()
        if self.lan_server:
            self.lan_server.shutdown(); self.lan_server.server_close()
        if self.server: self.server.shutdown(); self.server.server_close()
        self.destroy()


if __name__ == '__main__':
    App(sys.argv[1] if len(sys.argv) > 1 else None).mainloop()

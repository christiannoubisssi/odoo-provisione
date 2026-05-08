#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Odoo Multi-Site Provisioner v1.0.0
Cree automatiquement un site Odoo avec domaine et SSL
"""

import customtkinter as ctk
import paramiko
import json, re, threading, io
from pathlib import Path
from datetime import datetime
import tkinter.messagebox as messagebox
import tkinter.filedialog as filedialog

VERSION      = "1.0.0"
CONFIG_FILE  = "config.json"
HISTORY_FILE = "history.json"

DEFAULT_CONFIG = {
    "server_ip":        "195.35.24.204",
    "ssh_user":         "root",
    "ssh_port":         "22",
    "ssh_password":     "",
    "ssh_key_path":     "",
    "odoo_container":   "marche_dallygroupe_odoo",
    "db_container":     "marche_dallygroupe_db",
    "model_db":         "modele",
    "db_user":          "odoo",
    "certbot_email":    "",
}

LOG_COLORS = {
    "success": "#2ECC71",
    "error":   "#E74C3C",
    "warning": "#F0A500",
    "info":    "#4FC3F7",
    "step":    "#CE93D8",
}

# ═══ CONFIGURATION ═══════════════════════════════════════════

class ConfigManager:
    def __init__(self):
        self.path = Path(CONFIG_FILE)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    return {**DEFAULT_CONFIG, **json.load(f)}
            except Exception:
                pass
        return DEFAULT_CONFIG.copy()

    def save(self, data):
        self.data = {**DEFAULT_CONFIG, **data}
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2, ensure_ascii=False)

    def get(self):
        return self.data.copy()

# ═══ MOTEUR SSH / PROVISIONNEMENT ════════════════════════════

class OdooProvisioner:
    def __init__(self, config, log_fn, progress_fn):
        self.cfg      = config
        self._log     = log_fn
        self._setprog = progress_fn
        self._ssh     = None
        self._sftp    = None

    def _connect(self):
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs = {
            "hostname": self.cfg["server_ip"],
            "username": self.cfg["ssh_user"],
            "port":     int(self.cfg.get("ssh_port", 22)),
            "timeout":  30,
        }
        key_path = self.cfg.get("ssh_key_path", "").strip()
        password  = self.cfg.get("ssh_password", "").strip()
        if key_path and Path(key_path).exists():
            kwargs["key_filename"] = key_path
        elif password:
            kwargs["password"] = password
        else:
            raise ValueError(
                "Aucune methode d'authentification configuree.\n"
                "Renseignez un mot de passe SSH dans les Parametres."
            )
        client.connect(**kwargs)
        self._ssh  = client
        self._sftp = client.open_sftp()

    def _disconnect(self):
        for obj in (self._sftp, self._ssh):
            if obj:
                try: obj.close()
                except: pass

    def _run(self, cmd, desc=""):
        if desc:
            self._log(f"  -> {desc}", "info")
        _, stdout, stderr = self._ssh.exec_command(cmd, timeout=360)
        code = stdout.channel.recv_exit_status()
        out  = stdout.read().decode("utf-8", errors="replace").strip()
        err  = stderr.read().decode("utf-8", errors="replace").strip()
        if code != 0:
            raise RuntimeError(err or out or f"Code {code}")
        return out

    def _write_remote(self, path, content):
        buf = io.BytesIO(content.encode("utf-8"))
        self._sftp.putfo(buf, path)

    @staticmethod
    def domain_to_dbname(domain):
        name = re.sub(r"[^a-z0-9]+", "_", domain.lower().strip()).strip("_")
        if name and name[0].isdigit():
            name = "db_" + name
        return name or "new_site"

    # ── Etapes ──────────────────────────────────────────────

    def _check_model(self):
        m   = self.cfg["model_db"]
        out = self._run(
            f'docker exec {self.cfg["db_container"]} psql -U {self.cfg["db_user"]} '
            f'-tAc "SELECT 1 FROM pg_database WHERE datname=\'{m}\'"'
        )
        if out.strip() != "1":
            raise RuntimeError(f"Base modele \"{m}\" introuvable. Verifiez les Parametres.")
        self._log(f"  Base modele \"{m}\" trouvee OK", "success")

    def _check_free(self, db):
        out = self._run(
            f'docker exec {self.cfg["db_container"]} psql -U {self.cfg["db_user"]} '
            f'-tAc "SELECT 1 FROM pg_database WHERE datname=\'{db}\'"'
        )
        if out.strip() == "1":
            raise RuntimeError(f"La base \"{db}\" existe deja. Utilisez un autre domaine.")
        self._log(f"  Nom \"{db}\" disponible OK", "success")

    def _duplicate(self, db):
        m, dc, du = self.cfg["model_db"], self.cfg["db_container"], self.cfg["db_user"]
        self._log("  Resiliation des connexions sur la base source...", "info")
        self._run(
            f'docker exec {dc} psql -U {du} postgres -c '
            f'"SELECT pg_terminate_backend(pid) FROM pg_stat_activity '
            f'WHERE datname=\'{m}\' AND pid <> pg_backend_pid();"'
        )
        self._log(f"  Copie \"{m}\" -> \"{db}\" (patience 1-2 min)...", "info")
        self._run(f'docker exec {dc} createdb -U {du} -T {m} {db}',
                  "Creation de la base de donnees")
        self._log(f"  Base \"{db}\" creee OK", "success")

    def _update_url(self, db, domain):
        url = f"https://{domain}"
        self._run(
            f"docker exec {self.cfg['db_container']} psql "
            f"-U {self.cfg['db_user']} -d {db} -c "
            f"\"UPDATE ir_config_parameter SET value='{url}' WHERE key='web.base.url';\"",
            "Mise a jour URL Odoo"
        )
        self._log(f"  URL -> {url} OK", "success")

    def _nginx(self, domain, db):
        conf = f"/etc/nginx/sites-available/{domain}.conf"
        link = f"/etc/nginx/sites-enabled/{domain}.conf"
        content = "\n".join([
            "server {",
            f"    listen 80;",
            f"    server_name {domain};",
            "    # Redirection auto vers la bonne base de donnees",
            "    location = / {",
            f"        return 302 /web/login?db={db};",
            "    }",
            "    location / {",
            "        proxy_pass            http://127.0.0.1:8069;",
            "        proxy_set_header      Host              $host;",
            "        proxy_set_header      X-Real-IP         $remote_addr;",
            "        proxy_set_header      X-Forwarded-For   $proxy_add_x_forwarded_for;",
            "        proxy_set_header      X-Forwarded-Proto $scheme;",
            "        proxy_redirect        off;",
            "        proxy_buffering       off;",
            "        proxy_read_timeout    720s;",
            "        client_max_body_size  100m;",
            "    }",
            "    location /longpolling {",
            "        proxy_pass       http://127.0.0.1:8072;",
            "        proxy_set_header Host $host;",
            "        proxy_set_header X-Real-IP $remote_addr;",
            "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;",
            "        proxy_set_header X-Forwarded-Proto $scheme;",
            "    }",
            "    gzip on;",
            "    gzip_types text/plain text/css application/json application/javascript text/xml;",
            "}",
            "",
        ])
        self._log("  Ecriture config Nginx...", "info")
        self._write_remote(conf, content)
        self._run(f"ln -sf {conf} {link}", "Activation vhost")
        self._run("nginx -t",               "Verification syntaxe Nginx")
        self._run("systemctl reload nginx",  "Rechargement Nginx")
        self._log("  Configuration Nginx activee OK", "success")

    def _certbot(self, domain):
        email = self.cfg.get("certbot_email", "").strip() or f"admin@{domain}"
        self._log(f"  Demande certificat Let's Encrypt (email: {email})...", "info")
        self._log("  Cette etape prend 30-90 sec, patience...", "warning")
        cmd = (f"certbot --nginx -d {domain} --non-interactive --agree-tos "
               f"--email {email} --redirect --no-eff-email")
        try:
            self._run(cmd)
            self._log("  Certificat SSL obtenu, HTTPS active OK", "success")
        except RuntimeError as e:
            if any(k in str(e).lower() for k in
                   ("dns","resolve","connection refused","timeout","challenge","unable")):
                raise RuntimeError(
                    f"Certificat SSL impossible pour \"{domain}\".\n\n"
                    f"Le domaine ne pointe probablement pas encore vers ce serveur.\n\n"
                    f"Solution : creez un enregistrement DNS :\n"
                    f"  {domain}  =>  {self.cfg['server_ip']}\n\n"
                    f"Puis relancez. Detail : {e}"
                )
            raise

    # ── Workflow principal ───────────────────────────────────

    def run(self, domain):
        db  = self.domain_to_dbname(domain)
        sep = "-" * 52
        self._log(sep, "step")
        self._log(f"  NOUVEAU SITE    : {domain}", "step")
        self._log(f"  BASE DE DONNEES : {db}", "step")
        self._log(sep, "step")

        steps = [
            (0.05, "Connexion au serveur",           self._connect),
            (0.15, "Verification base modele",        self._check_model),
            (0.22, "Verification nom disponible",     lambda: self._check_free(db)),
            (0.35, "Duplication base de donnees",     lambda: self._duplicate(db)),
            (0.55, "Mise a jour URL Odoo",            lambda: self._update_url(db, domain)),
            (0.70, "Configuration Nginx",             lambda: self._nginx(domain, db)),
            (0.85, "Certificat SSL Let's Encrypt",    lambda: self._certbot(domain)),
        ]
        try:
            for i, (p, label, fn) in enumerate(steps):
                self._setprog(p)
                self._log(f"\n[{i+1}/{len(steps)}] {label}", "step")
                fn()
            self._setprog(1.0)
            self._log(f"\n{sep}", "step")
            self._log("  PROVISIONNEMENT TERMINE AVEC SUCCES !", "success")
            self._log(f"  URL : https://{domain}", "success")
            self._log(sep, "step")
            return True, domain, db
        except Exception as e:
            self._setprog(0.0)
            self._log(f"\n{sep}", "error")
            self._log(f"  ERREUR : {e}", "error")
            self._log(sep, "error")
            return False, str(e), db
        finally:
            self._disconnect()

# ═══ INTERFACE GRAPHIQUE ═════════════════════════════════════

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self._running = False
        self._cfg     = ConfigManager()
        self.title(f"Odoo Provisioner  -  v{VERSION}")
        self.geometry("740x660")
        self.resizable(False, False)
        self._build_header()
        self._build_tabs()
        self._load_cfg_to_ui()
        self._refresh_history()

    def _build_header(self):
        h = ctk.CTkFrame(self, fg_color="#0D1B2A", corner_radius=0, height=64)
        h.pack(fill="x"); h.pack_propagate(False)
        ctk.CTkLabel(h, text="  Odoo Multi-Site Provisioner",
                     font=ctk.CTkFont(size=19, weight="bold"),
                     text_color="#4FC3F7").pack(side="left", padx=20)
        ctk.CTkLabel(h, text=f"v{VERSION}  ",
                     font=ctk.CTkFont(size=11),
                     text_color="#455A64").pack(side="right")

    def _build_tabs(self):
        self._tabs = ctk.CTkTabview(self, height=570)
        self._tabs.pack(fill="both", expand=True, padx=14, pady=(10, 8))
        for n in ("Nouveau Site", "Parametres", "Historique"):
            self._tabs.add(n)
        self._build_new()
        self._build_settings()
        self._build_history()

    # ── Onglet Nouveau Site ──────────────────────────────────

    def _build_new(self):
        tab = self._tabs.tab("Nouveau Site")
        frm = ctk.CTkFrame(tab, fg_color="transparent")
        frm.pack(fill="x", padx=12, pady=(18, 0))

        ctk.CTkLabel(frm, text="Nom de domaine du nouveau site :",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(frm, text="Exemple :  client1.marche-dallygroupe.com   ou   monprojet.fr",
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="w", pady=(2, 6))

        self._dv = ctk.StringVar()
        self._de = ctk.CTkEntry(frm, textvariable=self._dv,
                                placeholder_text="nouveauclient.com",
                                font=ctk.CTkFont(size=15), height=46)
        self._de.pack(fill="x")
        self._de.bind("<Return>", lambda _: self._start())

        self._dbpv = ctk.StringVar(value="Base de donnees generee : -")
        ctk.CTkLabel(frm, textvariable=self._dbpv,
                     font=ctk.CTkFont(size=11), text_color="#78909C").pack(anchor="w", pady=(4,0))
        self._dv.trace_add("write", self._preview_db)

        self._btn = ctk.CTkButton(frm, text="Creer le site",
                                  font=ctk.CTkFont(size=14, weight="bold"),
                                  height=48, fg_color="#1565C0", hover_color="#0D47A1",
                                  command=self._start)
        self._btn.pack(fill="x", pady=(14, 0))

        pf = ctk.CTkFrame(tab, fg_color="transparent")
        pf.pack(fill="x", padx=12, pady=(10, 0))
        self._pb = ctk.CTkProgressBar(pf, height=10, corner_radius=5)
        self._pb.pack(fill="x"); self._pb.set(0)
        self._plv = ctk.StringVar(value="")
        ctk.CTkLabel(pf, textvariable=self._plv,
                     font=ctk.CTkFont(size=11), text_color="gray").pack(anchor="e", pady=(2,0))

        lf = ctk.CTkFrame(tab)
        lf.pack(fill="both", expand=True, padx=12, pady=(8, 8))
        ctk.CTkLabel(lf, text="Journal des operations",
                     font=ctk.CTkFont(size=11, weight="bold"),
                     text_color="#546E7A").pack(anchor="w", padx=8, pady=(5, 2))
        self._lb = ctk.CTkTextbox(lf, font=ctk.CTkFont(family="Courier New", size=11),
                                  wrap="word", state="disabled")
        self._lb.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        for tag, color in LOG_COLORS.items():
            self._lb._textbox.tag_configure(tag, foreground=color)

    # ── Onglet Parametres ────────────────────────────────────

    def _build_settings(self):
        tab = self._tabs.tab("Parametres")
        sc  = ctk.CTkScrollableFrame(tab, fg_color="transparent")
        sc.pack(fill="both", expand=True, padx=4, pady=4)

        def sec(t):
            ctk.CTkLabel(sc, text=t, font=ctk.CTkFont(size=12, weight="bold"),
                         text_color="#4FC3F7").pack(anchor="w", pady=(14, 2), padx=4)

        def row(label, sv, ph="", pw=False):
            f = ctk.CTkFrame(sc, fg_color="transparent"); f.pack(fill="x", pady=3)
            ctk.CTkLabel(f, text=label, width=210, anchor="w",
                         font=ctk.CTkFont(size=12)).pack(side="left")
            ctk.CTkEntry(f, textvariable=sv, placeholder_text=ph,
                         show="*" if pw else "", height=34
                         ).pack(side="left", fill="x", expand=True)

        sec("Serveur SSH")
        self._iv  = ctk.StringVar(); row("Adresse IP du serveur",   self._iv,  "195.35.24.204")
        self._uv  = ctk.StringVar(); row("Utilisateur SSH",          self._uv,  "root")
        self._ptv = ctk.StringVar(); row("Port SSH",                 self._ptv, "22")
        self._pw  = ctk.StringVar(); row("Mot de passe SSH",         self._pw,  "(vide si cle)", True)

        kf = ctk.CTkFrame(sc, fg_color="transparent"); kf.pack(fill="x", pady=3)
        ctk.CTkLabel(kf, text="Cle SSH privee (optionnel)", width=210,
                     anchor="w", font=ctk.CTkFont(size=12)).pack(side="left")
        self._kv = ctk.StringVar()
        ctk.CTkEntry(kf, textvariable=self._kv,
                     placeholder_text="C:/Users/.../.ssh/id_rsa",
                     height=34).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(kf, text="...", width=40, height=34,
                      command=self._pick_key).pack(side="left", padx=(4,0))

        sec("Conteneurs Docker")
        self._ov  = ctk.StringVar(); row("Conteneur Odoo",           self._ov,  "marche_dallygroupe_odoo")
        self._dbv = ctk.StringVar(); row("Conteneur PostgreSQL",      self._dbv, "marche_dallygroupe_db")
        self._mv  = ctk.StringVar(); row("Base de donnees modele",    self._mv,  "modele")
        self._duv = ctk.StringVar(); row("Utilisateur PostgreSQL",    self._duv, "odoo")

        sec("Certificat SSL")
        self._ev = ctk.StringVar(); row("Email pour Let's Encrypt", self._ev, "admin@mondomaine.com")
        ctk.CTkLabel(sc, text="(Obligatoire pour le certificat SSL)",
                     font=ctk.CTkFont(size=11), text_color="gray"
                     ).pack(anchor="w", padx=216, pady=(0, 4))

        ctk.CTkButton(sc, text="Enregistrer les parametres", height=42,
                      font=ctk.CTkFont(size=13, weight="bold"),
                      fg_color="#2E7D32", hover_color="#1B5E20",
                      command=self._save_cfg).pack(fill="x", pady=(16, 4))

        ctk.CTkButton(sc, text="Tester la connexion SSH", height=38,
                      font=ctk.CTkFont(size=12),
                      fg_color="#00695C", hover_color="#004D40",
                      command=self._test_ssh).pack(fill="x", pady=4)

    # ── Onglet Historique ────────────────────────────────────

    def _build_history(self):
        tab = self._tabs.tab("Historique")
        tp  = ctk.CTkFrame(tab, fg_color="transparent"); tp.pack(fill="x", padx=8, pady=(8,0))
        ctk.CTkLabel(tp, text="Sites crees avec cet outil",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkButton(tp, text="Actualiser", width=100, height=30,
                      command=self._refresh_history).pack(side="right")
        self._hb = ctk.CTkTextbox(tab, font=ctk.CTkFont(family="Courier New", size=12),
                                  state="disabled", wrap="word")
        self._hb.pack(fill="both", expand=True, padx=8, pady=8)

    # ── Helpers ──────────────────────────────────────────────

    def _preview_db(self, *_):
        d = self._dv.get().strip()
        self._dbpv.set(f"Base de donnees generee : {OdooProvisioner.domain_to_dbname(d)}"
                       if d else "Base de donnees generee : -")

    def _load_cfg_to_ui(self):
        c = self._cfg.get()
        self._iv.set(c.get("server_ip",""));   self._uv.set(c.get("ssh_user",""))
        self._ptv.set(c.get("ssh_port","22")); self._pw.set(c.get("ssh_password",""))
        self._kv.set(c.get("ssh_key_path","")); self._ov.set(c.get("odoo_container",""))
        self._dbv.set(c.get("db_container","")); self._mv.set(c.get("model_db",""))
        self._duv.set(c.get("db_user","")); self._ev.set(c.get("certbot_email",""))

    def _ui_to_cfg(self):
        return {
            "server_ip": self._iv.get().strip(), "ssh_user": self._uv.get().strip(),
            "ssh_port": self._ptv.get().strip() or "22", "ssh_password": self._pw.get(),
            "ssh_key_path": self._kv.get().strip(), "odoo_container": self._ov.get().strip(),
            "db_container": self._dbv.get().strip(), "model_db": self._mv.get().strip(),
            "db_user": self._duv.get().strip(), "certbot_email": self._ev.get().strip(),
        }

    def _save_cfg(self):
        self._cfg.save(self._ui_to_cfg())
        messagebox.showinfo("Parametres", "Parametres enregistres avec succes !")

    def _pick_key(self):
        p = filedialog.askopenfilename(title="Cle SSH privee",
                                       filetypes=[("Tous", "*.*"), ("PEM/PPK", "*.pem *.ppk")])
        if p: self._kv.set(p)

    def _test_ssh(self):
        self._save_cfg(); cfg = self._cfg.get()
        def _w():
            p = OdooProvisioner(cfg, lambda m,t="": None, lambda v: None)
            try:
                p._connect(); p._run("echo ok"); p._disconnect()
                self.after(0, lambda: messagebox.showinfo("SSH", "Connexion reussie !"))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("SSH", f"Echec :\n\n{e}"))
        threading.Thread(target=_w, daemon=True).start()

    def _log(self, msg, tag=""):
        def _d():
            self._lb.configure(state="normal")
            self._lb.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n", tag or "")
            self._lb.see("end"); self._lb.configure(state="disabled")
        self.after(0, _d)

    def _setprog(self, v):
        def _d():
            self._pb.set(v)
            if v <= 0:   self._plv.set(""); self._pb.configure(progress_color="#1565C0")
            elif v >= 1: self._plv.set("100% - Termine !"); self._pb.configure(progress_color="#2ECC71")
            else:        self._plv.set(f"{int(v*100)}%..."); self._pb.configure(progress_color="#1565C0")
        self.after(0, _d)

    def _start(self):
        if self._running: return
        domain = self._dv.get().strip().lower()
        if not domain:
            messagebox.showwarning("Domaine manquant", "Veuillez saisir un nom de domaine."); return
        if not re.match(r"^[a-z0-9][a-z0-9\-\.]+\.[a-z]{2,}$", domain):
            messagebox.showwarning("Domaine invalide",
                                   f"\"{domain}\" n'est pas valide.\nExemple : client.monserveur.com"); return
        db = OdooProvisioner.domain_to_dbname(domain)
        if not messagebox.askyesno("Confirmer",
            f"Creer un site Odoo pour :\n\n  Domaine : {domain}\n  Base    : {db}\n\n"
            f"IMPORTANT : Le DNS doit deja pointer\nvers ce serveur. Continuer ?"):
            return
        self._save_cfg(); cfg = self._cfg.get()
        self._running = True
        self._btn.configure(state="disabled", text="Provisionnement en cours...")
        self._de.configure(state="disabled")
        self._pb.set(0)
        self._lb.configure(state="normal"); self._lb.delete("1.0","end"); self._lb.configure(state="disabled")

        def _w():
            ok, res, db2 = OdooProvisioner(cfg, self._log, self._setprog).run(domain)
            self.after(0, lambda: self._done(ok, domain, db2, res))
        threading.Thread(target=_w, daemon=True).start()

    def _done(self, ok, domain, db, result):
        self._running = False
        self._btn.configure(state="normal", text="Creer le site")
        self._de.configure(state="normal")
        if ok:
            self._add_history(domain, db)
            messagebox.showinfo("Site cree !",
                f"Site accessible :\n\n  URL  : https://{domain}\n  Base : {db}\n\nAcces immediat.")
            self._dv.set(""); self._setprog(0)
        else:
            messagebox.showerror("Erreur", f"Erreur lors du provisionnement :\n\n{result}")

    def _add_history(self, domain, db):
        records = []
        p = Path(HISTORY_FILE)
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f: records = json.load(f)
            except: pass
        records.insert(0, {"domain": domain, "db_name": db,
                            "created_at": datetime.now().isoformat()})
        with open(p, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, ensure_ascii=False)
        self._refresh_history()

    def _refresh_history(self):
        self._hb.configure(state="normal"); self._hb.delete("1.0","end")
        p = Path(HISTORY_FILE)
        if p.exists():
            try:
                with open(p, encoding="utf-8") as f: recs = json.load(f)
                if recs:
                    self._hb.insert("end", f"{'─'*48}\n  Sites provisiones\n{'─'*48}\n\n")
                    for r in recs:
                        dt = r.get("created_at","")[:16].replace("T"," a ")
                        self._hb.insert("end",
                            f"  URL  : https://{r['domain']}\n"
                            f"  Base : {r['db_name']}\n"
                            f"  Date : {dt}\n\n")
                else:
                    self._hb.insert("end","  Aucun site cree pour le moment.")
            except:
                self._hb.insert("end","  Impossible de lire l'historique.")
        else:
            self._hb.insert("end","  Aucun site cree pour le moment.")
        self._hb.configure(state="disabled")

# ═══ LANCEMENT ═══════════════════════════════════════════════

if __name__ == "__main__":
    app = App()
    app.mainloop()